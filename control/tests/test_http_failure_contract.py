"""Catch storage-as-refusal bugs and accidental retries of client errors."""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.testclient import TestClient
from vonk_agent_protocol.http_failure import HttpTransient, TransientReason
from vonk_control.agent_api import common
from vonk_control.api import application, create_app
from vonk_control.auth import AgentIdentity, TokenCodec
from vonk_control.http_errors import temporary_http_answer


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # Routes exercise the production exception handler and middleware, without
    # starting workers or requiring a database for these HTTP-only boundaries.
    monkeypatch.setattr(application, "active_agent_identity", lambda *_: True)
    app = create_app(
        jobs=cast(Any, object()),
        tokens=TokenCodec(b"t" * 32),
        agent=cast(common.AgentApiServices, object()),
        trusted_agent_proxy_auth=b"test",
    )
    with TestClient(
        app,
        headers={
            "x-vonk-agent-proxy-auth": "test",
            "x-vonk-agent-node": "spk_" + "a" * 32,
            "x-vonk-agent-serial": "serial",
            "x-vonk-agent-fingerprint": "fingerprint",
            "x-vonk-agent-verified": "1",
            "x-vonk-agent-source": "192.0.2.1",
        },
    ) as value:
        yield value


@pytest.mark.parametrize("reason", list(TransientReason))
@pytest.mark.parametrize("path", ["/agent/temporary", "/api/temporary"])
def test_temporary_answer(
    client: TestClient, reason: TransientReason, path: str
) -> None:
    """Catch nested envelopes, lost reasons and incorrect rate-limit status."""

    def temporary() -> None:
        temporary_http_answer(reason, retry_after=17)

    cast(FastAPI, client.app).add_api_route(path, temporary, methods=["POST"])
    response = client.post(path)
    assert response.status_code == (
        429 if reason == TransientReason.RATE_LIMITED else 503
    )
    assert response.headers["retry-after"] == "17"
    answer = HttpTransient.model_validate_json(response.content)
    assert answer.reason == reason
    assert answer.retry_after == 17


@pytest.mark.parametrize("condition", ["symlink", "fifo", "permission", "publication"])
def test_storage_is_temporary(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, condition: str
) -> None:
    """Catch non-security storage failures incorrectly reported as HTTP 403."""
    target = tmp_path / ".upload.upload"
    destination = tmp_path / "image"
    if condition == "symlink":
        target.symlink_to(tmp_path / "absent")
    elif condition == "fifo":
        os.mkfifo(target)
    elif condition == "publication":
        destination.mkdir()
    else:

        def denied(*args: object, **kwargs: object) -> int:
            raise PermissionError("storage unavailable")

        monkeypatch.setattr(common.os, "open", denied)

    def temporary() -> None:
        if condition == "publication":
            common._commit_recipe_image_upload(target, destination, expected_bytes=1)
        else:
            common._prepare_recipe_image_upload(tmp_path, "upload")

    cast(FastAPI, client.app).add_api_route(
        "/agent/storage", temporary, methods=["POST"]
    )
    response = client.post("/agent/storage")
    assert response.status_code == 503
    answer = HttpTransient.model_validate_json(response.content)
    assert answer.reason == TransientReason.STORAGE_UNAVAILABLE
    assert int(response.headers["retry-after"]) == answer.retry_after


@pytest.mark.parametrize(
    "condition, status", [("identity_missing", 401), ("identity_override", 403)]
)
def test_security_refusal(client: TestClient, condition: str, status: int) -> None:
    """Catch security refusals softened into temporary or retryable responses."""

    def refusal(request: Request) -> None:
        if condition == "identity_missing":
            common._scope_identity(Request({"type": "http"}))
        else:
            identity = AgentIdentity("spk_" + "a" * 32, "serial", "fingerprint", True)
            common._body_node_matches("another-node", identity)

    cast(FastAPI, client.app).add_api_route("/agent/security", refusal)
    response = client.get("/agent/security")
    assert response.status_code == status
    assert "retry-after" not in response.headers


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 413, 422])
@pytest.mark.parametrize("path", ["/agent/client-error", "/plain-client-error"])
def test_unclassified_client_error(client: TestClient, status: int, path: str) -> None:
    """Catch status-based reclassification of unrelated client errors."""

    def client_error() -> None:
        raise HTTPException(status_code=status, detail="unchanged client error")

    cast(FastAPI, client.app).add_api_route(path, client_error)
    response = client.get(path)
    assert response.status_code == status
    assert response.json() == (
        {"detail": "unchanged client error", "issues": []}
        if status == 422 and path.startswith("/agent/")
        else {"detail": "unchanged client error"}
    )
    assert "retry-after" not in response.headers


@pytest.mark.parametrize("status", [429, 503, 500, 403])
@pytest.mark.parametrize("retry_after", [None, "23"])
def test_retry_header_fallback(
    client: TestClient, status: int, retry_after: str | None
) -> None:
    """Catch overwritten Retry-After headers or middleware modifying bodies."""

    def answer() -> Response:
        return Response(
            "unchanged body",
            status_code=status,
            headers={"Retry-After": retry_after} if retry_after is not None else {},
        )

    cast(FastAPI, client.app).add_api_route("/header-fallback", answer)
    response = client.get("/header-fallback")
    assert response.status_code == status
    assert response.text == "unchanged body"
    if retry_after is not None:
        assert response.headers["retry-after"] == retry_after
    elif status in {429, 503}:
        assert int(response.headers["retry-after"]) > 0
    else:
        assert "retry-after" not in response.headers
