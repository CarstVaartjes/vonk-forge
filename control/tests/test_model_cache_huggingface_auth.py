from __future__ import annotations

from pathlib import Path

import httpx2
import pytest
from sqlalchemy.orm import sessionmaker
from vonk_control.model_cache import (
    ArtifactSpec,
    ModelCacheService,
)

SOURCE = "https://huggingface.co/acme/private/resolve/" + "a" * 40 + "/weights.bin"


def _service(
    tmp_path: Path,
    handler,
    *,
    token: str | None = None,
) -> tuple[ModelCacheService, httpx2.Client]:
    token_path = None
    if token is not None:
        token_path = tmp_path / "hf-token"
        token_path.write_text(token + "\n")
    client = httpx2.Client(
        transport=httpx2.MockTransport(handler),
        follow_redirects=False,
    )
    return (
        ModelCacheService(
            sessionmaker(),
            tmp_path / "cache",
            reserve_bytes=0,
            http_client=client,
            huggingface_token_path=token_path,
        ),
        client,
    )


def test_configured_huggingface_token_is_used_on_canonical_request(
    tmp_path: Path,
) -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, content=b"public model")

    service, client = _service(tmp_path, handler, token="hf_should_not_be_used")
    try:
        response = service._open_http_response(client, SOURCE, {})
        assert response.read() == b"public model"
        response.close()
    finally:
        client.close()

    assert len(requests) == 1
    assert requests[0].headers["authorization"] == "Bearer hf_should_not_be_used"


@pytest.mark.parametrize("optional_secret", ["unset", "missing", "empty", "blank"])
def test_public_huggingface_download_is_anonymous_without_token_file(
    tmp_path: Path, optional_secret: str
) -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, content=b"public model")

    service, client = _service(tmp_path, handler)
    if optional_secret != "unset":
        path = tmp_path / "optional-hf-token"
        service._huggingface_token_path = path
        if optional_secret != "missing":
            path.write_text("" if optional_secret == "empty" else "\n")
    try:
        response = service._open_http_response(client, SOURCE, {})
        assert response.read() == b"public model"
        response.close()
    finally:
        client.close()
    assert len(requests) == 1
    assert "authorization" not in requests[0].headers


@pytest.mark.parametrize("unsafe_secret", ["symlink", "directory", "malformed"])
def test_invalid_huggingface_secret_does_not_downgrade_to_anonymous(
    tmp_path: Path, unsafe_secret: str
) -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, content=b"verified access")

    service, client = _service(tmp_path, handler)
    path = tmp_path / "unsafe-hf-token"
    if unsafe_secret == "symlink":
        target = tmp_path / "target"
        target.write_text("hf_test")
        path.symlink_to(target)
    elif unsafe_secret == "directory":
        path.mkdir()
    else:
        path.write_text("hf_bad token")
    service._huggingface_token_path = path
    try:
        with pytest.raises(Exception):  # noqa: B017 -- no HTTP effect; repaired credentials below
            service._open_http_response(client, SOURCE, {})
        assert not requests
        repaired = tmp_path / "repaired-hf-token"
        repaired.write_text("hf_valid_secret\n")
        service._huggingface_token_path = repaired
        response = service._open_http_response(client, SOURCE, {})
        assert response.read() == b"verified access"
        response.close()
        assert requests[-1].headers["authorization"] == "Bearer hf_valid_secret"
    finally:
        client.close()


def test_streamed_huggingface_response_is_consumed_before_cleanup(
    tmp_path: Path,
) -> None:
    class TrackingStream(httpx2.SyncByteStream):
        closed = False

        def __iter__(self):
            yield b"streamed model"

        def close(self) -> None:
            self.closed = True

    stream = TrackingStream()

    def handler(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, stream=stream)

    service, client = _service(tmp_path, handler)
    spec = ArtifactSpec(
        key="weights",
        artifact_id="weights",
        path="weights.bin",
        kind="huggingface.file",
        repository="acme/private",
        source=SOURCE,
        revision="a" * 40,
        sha256="b" * 64,
        expected_bytes=len(b"streamed model"),
        roles=("model",),
    )
    try:
        body, offset, close = service._open_source(spec, 0)
        assert offset == 0
        assert b"".join(body) == b"streamed model"
        close()
    finally:
        client.close()

    assert stream.closed


def test_gated_huggingface_download_retries_with_bearer_token(tmp_path: Path) -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert request.headers.get("authorization") == "Bearer hf_gated_secret"
        return httpx2.Response(200, content=b"gated model")

    service, client = _service(tmp_path, handler, token="hf_gated_secret")
    try:
        response = service._open_http_response(client, SOURCE, {})
        assert response.read() == b"gated model"
        response.close()
    finally:
        client.close()

    assert len(requests) == 1
    assert requests[0].headers["authorization"] == "Bearer hf_gated_secret"


@pytest.mark.parametrize("token", [None, "hf_rejected_secret"])
def test_denied_huggingface_access_has_no_bytes_and_fresh_access_succeeds(
    tmp_path: Path, token: str | None
) -> None:
    allowed = False

    def handler(_: httpx2.Request) -> httpx2.Response:
        return (
            httpx2.Response(200, content=b"gated model")
            if allowed
            else httpx2.Response(403)
        )

    service, client = _service(tmp_path, handler, token=token)
    accepted = []
    try:
        with pytest.raises(Exception) as caught:
            accepted.append(service._open_http_response(client, SOURCE, {}))
        assert not accepted
        if token is not None:
            assert token not in str(caught.value)
        allowed = True
        response = service._open_http_response(client, SOURCE, {})
        assert response.read() == b"gated model"
        response.close()
    finally:
        client.close()


def test_huggingface_cdn_redirect_is_allowed_and_bearer_is_stripped(
    tmp_path: Path,
) -> None:
    requests: list[httpx2.Request] = []
    cdn = "https://cdn-lfs-us-1.hf.co/signed/weights?x=1"

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if len(requests) == 1:
            assert request.headers["authorization"] == "Bearer hf_cdn_secret"
            return httpx2.Response(302, headers={"location": cdn})
        return httpx2.Response(
            206,
            content=b"gated model",
            headers={"content-range": "bytes 0-11/12"},
        )

    service, client = _service(tmp_path, handler, token="hf_cdn_secret")
    try:
        response = service._open_http_response(client, SOURCE, {})
        assert response.read() == b"gated model"
        response.close()
    finally:
        client.close()

    assert requests[0].headers["authorization"] == "Bearer hf_cdn_secret"
    assert "authorization" not in requests[1].headers
    assert str(requests[1].url) == cdn


def test_huggingface_redirect_to_arbitrary_host_is_rejected(tmp_path: Path) -> None:
    requests: list[httpx2.Request] = []
    redirect = True

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if redirect:
            return httpx2.Response(
                302, headers={"location": "https://attacker.example/steal"}
            )
        return httpx2.Response(200, content=b"model")

    service, client = _service(tmp_path, handler, token="hf_secret")
    try:
        with pytest.raises(Exception):  # noqa: B017 -- no credential sent to attacker; fresh request below
            service._open_http_response(client, SOURCE, {})
        assert requests and all(
            request.url.host == "huggingface.co" for request in requests
        )
        redirect = False
        response = service._open_http_response(client, SOURCE, {})
        assert response.read() == b"model"
        response.close()
    finally:
        client.close()


def test_huggingface_range_resume_preserves_requested_offset(tmp_path: Path) -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            206,
            content=b"payload",
            headers={"content-range": "bytes 7-13/14"},
        )

    service, client = _service(tmp_path, handler)
    try:
        response = service._open_http_response(client, SOURCE, {"Range": "bytes=7-"})
        assert response.read() == b"payload"
        response.close()
    finally:
        client.close()

    assert requests[0].headers["range"] == "bytes=7-"
