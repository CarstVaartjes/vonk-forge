"""A faithful fake of GitHub's anonymous Git LFS download surface.

It answers like the real peer, as recorded on 2026-10-27 in
``fixtures/github_lfs_batch_response.json`` (signed query values redacted):

- the batch POST demands the LFS media type (406 otherwise) and a well-formed
  download request; it replies ``application/json`` with only ``objects``,
  per-object errors inside a 200, and the ``x-ratelimit-*`` headers;
- each download link is an S3 pre-signed URL on
  ``github-cloud.githubusercontent.com`` with the recorded query keys, a fresh
  ``token`` and an ``expires_in``/``expires_at`` on the injected clock;
- object storage answers 403 XML for an expired or unknown link, 206 with an
  exact ``Content-Range`` for a range and 200 without one;
- no request may carry the client's credentials or cookies.

Scripted faults: a batch answer (429/403/509/401/...), a per-object error, an
off-allowlist link host, a link redirect, storage clock skew, wrong bytes and a
truncated body.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx2
from vonk_forge_contracts import ModelDefinition

VECTOR = json.loads(
    (Path(__file__).parent / "fixtures/github_lfs_batch_response.json").read_text()
)
OWNER = "NVlabs"
REPO = "GR00T-WholeBodyControl"
REVISION = "45c03facbf848e0c743091b7d9c17952096ff908"
BATCH_URL = f"https://github.com/{OWNER}/{REPO}.git/info/lfs/objects/batch"
OBJECT_HOST = "github-cloud.githubusercontent.com"
LFS_MEDIA_TYPE = "application/vnd.git-lfs+json"
USER_AGENT = "vonk-forge/0.1.1"
LINK_SECONDS = 3600
_RECORDED_HREF = urlsplit(VECTOR["body"]["objects"][0]["actions"]["download"]["href"])
_RECORDED_QUERY_KEYS = [key for key, _ in parse_qsl(_RECORDED_HREF.query)]
_RECORDED_MISSING = VECTOR["body"]["objects"][1]
_ACCESS_DENIED = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b"<Error><Code>AccessDenied</Code><Message>Request has expired</Message></Error>"
)


class GithubLfsFake:
    """One public repository whose LFS store holds ``data``."""

    def __init__(self, data: bytes, clock: Callable[[], datetime]) -> None:
        self.data = data
        self.oid = hashlib.sha256(data).hexdigest()
        self.clock = clock
        self.requests: list[httpx2.Request] = []
        self.batch_calls = 0
        self.remaining = 100
        self.reset_at = clock() + timedelta(hours=1)
        # Scripted faults; None means healthy.
        self.batch_answer: Callable[[], httpx2.Response] | None = None
        self.object_error: int | None = None
        self.link_host = OBJECT_HOST
        self.link_status: int | None = None
        self.storage_skew = timedelta(0)
        self.served: bytes | None = None
        self.truncate_next_to: int | None = None
        self._links: dict[str, datetime] = {}

    # The transport -------------------------------------------------------

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        assert request.headers.get("authorization") is None
        assert request.headers.get("cookie") is None
        assert request.headers.get("user-agent") == USER_AGENT
        if str(request.url) == BATCH_URL:
            return self._batch(request)
        if request.url.host == self.link_host:
            return self._object(request)
        raise AssertionError(f"unexpected provider request: {request.url.host}")

    def revoke_links(self) -> None:
        """Object storage stops honouring every link issued so far."""
        self._links.clear()

    def requests_to(self, host: str) -> list[httpx2.Request]:
        return [request for request in self.requests if request.url.host == host]

    @property
    def link_requests(self) -> list[httpx2.Request]:
        return self.requests_to(self.link_host)

    # The batch API -------------------------------------------------------

    def _rate_headers(self) -> dict[str, str]:
        return {
            "x-ratelimit-limit": "100",
            "x-ratelimit-remaining": str(self.remaining),
            "x-ratelimit-reset": str(int(self.reset_at.timestamp())),
            "x-ratelimit-resource": "lfs",
        }

    def _batch(self, request: httpx2.Request) -> httpx2.Response:
        assert request.method == "POST"
        self.batch_calls += 1
        if request.headers.get("accept") != LFS_MEDIA_TYPE:
            return httpx2.Response(406, request=request)
        assert request.headers.get("content-type") == LFS_MEDIA_TYPE
        document = json.loads(request.content)
        assert document["operation"] == "download"
        assert "basic" in document["transfers"]
        assert document.get("hash_algo", "sha256") == "sha256"
        [wanted] = document["objects"]
        if self.batch_answer is not None:
            reply = self.batch_answer()
            return httpx2.Response(
                reply.status_code,
                request=request,
                headers={**self._rate_headers(), **reply.headers},
                content=reply.content,
            )
        self.remaining = max(0, self.remaining - 1)
        entry = self._entry(wanted["oid"], wanted["size"])
        return httpx2.Response(
            200,
            request=request,
            headers={
                "content-type": VECTOR["headers"]["content-type"],
                **self._rate_headers(),
            },
            content=json.dumps({"objects": [entry]}).encode(),
        )

    def _entry(self, oid: str, size: int) -> dict[str, object]:
        if oid != self.oid or self.object_error is not None:
            missing = copy.deepcopy(_RECORDED_MISSING)
            missing.update(oid=oid, size=size)
            if self.object_error is not None:
                missing["error"]["code"] = self.object_error
            return missing
        entry = copy.deepcopy(VECTOR["body"]["objects"][0])
        token = uuid.uuid4().hex
        now = self.clock()
        expires = now + timedelta(seconds=LINK_SECONDS)
        self._links[token] = expires
        values = {
            "X-Amz-Expires": str(LINK_SECONDS),
            "X-Amz-Signature": hashlib.sha256(token.encode()).hexdigest(),
            "token": token,
        }
        query = urlencode(
            [(key, values.get(key, "fake")) for key in _RECORDED_QUERY_KEYS]
        )
        path = _RECORDED_HREF.path.replace(VECTOR["body"]["objects"][0]["oid"], oid)
        path = re.sub(r"/[0-9a-f]{2}/[0-9a-f]{2}/", f"/{oid[:2]}/{oid[2:4]}/", path)
        entry.update(oid=oid, size=size)
        entry["actions"]["download"].update(
            href=f"https://{self.link_host}{path}?{query}",
            expires_in=LINK_SECONDS,
            expires_at=expires.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        return entry

    # Object storage ------------------------------------------------------

    def _object(self, request: httpx2.Request) -> httpx2.Response:
        assert request.method == "GET"
        if self.link_status is not None:
            return httpx2.Response(
                self.link_status,
                request=request,
                headers={"location": "https://elsewhere.example/object"},
            )
        token = dict(parse_qsl(request.url.query.decode())).get("token")
        expires = self._links.get(token or "")
        if expires is None or self.clock() + self.storage_skew >= expires:
            return httpx2.Response(
                403,
                request=request,
                headers={"content-type": "application/xml", "server": "AmazonS3"},
                content=_ACCESS_DENIED,
            )
        body = self.served if self.served is not None else self.data
        total = len(body)
        requested = request.headers.get("range")
        start, end, status = 0, total - 1, 200
        if requested is not None:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", requested)
            assert match is not None, requested
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else total - 1
            status = 206
        content = body[start : end + 1]
        if self.truncate_next_to is not None:
            content = content[: self.truncate_next_to]
            self.truncate_next_to = None
        headers = {"server": "AmazonS3", "accept-ranges": "bytes"}
        if status == 206:
            headers["content-range"] = f"bytes {start}-{end}/{total}"
        return httpx2.Response(
            status, request=request, headers=headers, content=content
        )


def quota_answer(status: int) -> Callable[[], httpx2.Response]:
    """The repository owner's bandwidth is exhausted (status unverified: 403 or 509)."""

    message = (
        "This repository is over its data quota. Account responsible for LFS "
        "bandwidth should purchase more data packs to restore access."
    )
    return lambda: httpx2.Response(
        status,
        headers={"content-type": "application/json; charset=utf-8"},
        content=json.dumps({"message": message}).encode(),
    )


def rate_limited_until(at: datetime) -> Callable[[], httpx2.Response]:
    """The 100 anonymous LFS calls of this hour are spent."""

    return lambda: httpx2.Response(
        429,
        headers={
            "x-ratelimit-remaining": "0",
            "x-ratelimit-reset": str(int(at.timestamp())),
        },
    )


def repository_gone() -> httpx2.Response:
    """What anonymous GitHub answers for a missing or private repository."""

    return httpx2.Response(
        401,
        headers={
            "content-type": "application/json; charset=utf-8",
            "lfs-authenticate": 'Basic realm="GitHub"',
        },
        content=b'{"message":"Requires authentication"}',
    )


def lfs_model(data: bytes) -> ModelDefinition:
    """The shared contract example, with these bytes as its one LFS object."""

    document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-definition-github-lfs.json")
        .read_text(encoding="utf-8")
    )
    [entry] = document["files"]
    entry["sha256"] = hashlib.sha256(data).hexdigest()
    entry["size_bytes"] = len(data)
    return ModelDefinition.model_validate(document)
