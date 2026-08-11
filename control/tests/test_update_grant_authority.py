from __future__ import annotations

import io
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from vonk_control.worker_authority import (
    HttpWorkerAuthority,
    WorkerAuthorityError,
    install_worker_authority_routes,
    worker_document_signature,
)

ROLLOUT_ID = "10000000-0000-4000-8000-000000000001"
NODE_ID = "spk_00000000000000000000000000000001"
TOKEN = b"w" * 32
GRANT = {
    "claims": {
        "action": "agent.update",
        "node_ids": [NODE_ID],
    },
    "signature": {
        "algorithm": "ed25519",
        "key_id": "sha256:" + "1" * 64,
        "value": "2" * 128,
    },
}


class _Grants:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def refresh_update_grant(
        self,
        rollout_id: str,
        batch_index: int,
        node_ids: tuple[str, ...],
        *,
        actor: str,
        request_id: str,
    ) -> dict[str, object]:
        self.calls.append(
            (rollout_id, batch_index, node_ids, actor, request_id)
        )
        return GRANT


def _body() -> dict[str, object]:
    return {
        "schema_version": 1,
        "rollout_id": ROLLOUT_ID,
        "batch_index": 0,
        "node_ids": [NODE_ID],
        "nonce": "0" * 32,
    }


def test_internal_update_grant_refresh_is_authenticated_and_exact() -> None:
    grants = _Grants()
    app = FastAPI()
    install_worker_authority_routes(
        app,
        object(),
        token=TOKEN,
        update_grants=grants,
    )
    client = TestClient(app)
    body = _body()

    assert client.post("/internal/v1/updates/grant", json=body).status_code == 401
    response = client.post(
        "/internal/v1/updates/grant",
        json=body,
        headers={
            "x-vonk-worker-signature": worker_document_signature(
                TOKEN, body, purpose="request"
            ),
            "x-request-id": "20000000-0000-4000-8000-000000000002",
        },
    )

    assert response.status_code == 200
    document = response.json()
    signature = document.pop("signature")
    assert document == {**body, "grant": GRANT}
    assert signature == worker_document_signature(
        TOKEN, document, purpose="response"
    )
    assert grants.calls == [
        (
            ROLLOUT_ID,
            0,
            (NODE_ID,),
            "control-worker",
            "20000000-0000-4000-8000-000000000002",
        )
    ]


def test_http_worker_authority_requests_and_verifies_exact_update_grant() -> None:
    calls: list[str] = []

    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            self.close()

        def geturl(self) -> str:
            return calls[-1]

    def open_request(request, *, timeout):
        calls.append(request.full_url)
        request_document = json.loads(request.data)
        response = {**request_document, "grant": GRANT}
        response["signature"] = worker_document_signature(
            TOKEN, response, purpose="response"
        )
        return Response(json.dumps(response).encode())

    authority = HttpWorkerAuthority(
        "http://control-api:8000",
        TOKEN,
        opener=open_request,
    )

    assert authority.refresh_update_grant(ROLLOUT_ID, 0, (NODE_ID,)) == GRANT
    assert calls == ["http://control-api:8000/internal/v1/updates/grant"]


def test_http_worker_authority_rejects_rebound_update_grant_response() -> None:
    class Response(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            self.close()

        def geturl(self) -> str:
            return "http://control-api:8000/internal/v1/updates/grant"

    def open_request(request, *, timeout):
        request_document = json.loads(request.data)
        response = {
            **request_document,
            "batch_index": 1,
            "grant": GRANT,
        }
        response["signature"] = worker_document_signature(
            TOKEN, response, purpose="response"
        )
        return Response(json.dumps(response).encode())

    authority = HttpWorkerAuthority(
        "http://control-api:8000",
        TOKEN,
        opener=open_request,
    )

    with pytest.raises(WorkerAuthorityError, match="response is invalid"):
        authority.refresh_update_grant(ROLLOUT_ID, 0, (NODE_ID,))
