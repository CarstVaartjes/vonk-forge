import base64
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from vonk_control.api import AdminServices, SpaFiles, create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.database_authority import AuthorityProposalPreview
from vonk_control.operation_api import (
    OperationDetailResponse,
    ProposalPreviewResponse,
)


class Jobs:
    def enqueue(self, *args, **kwargs): raise AssertionError
    def get(self, job_id): raise KeyError


class Repository:
    def head(self): return "a" * 64
    def inspect(self, revision):
        return type("Snapshot", (), {"revision": revision, "documents": {"inventory/topology.json": "blob"}, "dependencies": {}})()
    def read_document(self, revision, path):
        return type("Document", (), {"revision": revision, "path": path, "sha256": "hash", "parsed": {"schema_version": 2}})()


class Proposals:
    def preview(self, actor, base, changes):
        assert actor == "admin"
        return AuthorityProposalPreview(actor, base, b"canonical diff", tuple(change.path for change in changes), ("passed",), "d" * 64)


def test_admin_proposal_returns_canonical_patch_and_digest() -> None:
    codec = TokenCodec(b"k" * 32)
    app = create_app(
        jobs=Jobs(), tokens=codec, audits=MemoryAuditStore(), fleet=lambda: {"nodes": []}, now=lambda: 10,
        admin=AdminServices(authority=Repository(), proposals=Proposals(), changes=None),
    )
    client = TestClient(app)
    token = codec.issue(Actor("admin", "administrator"), ttl_seconds=100, now=0)
    response = client.post("/api/v1/proposals", headers={"Authorization": f"Bearer {token}"}, json={
        "base_revision": "a" * 64,
        "changes": [{"path": "inventory/topology.json", "document": {"schema_version": 1}}],
    })
    assert response.status_code == 200
    assert response.json() == {
        "affected_documents": ["inventory/topology.json"],
        "base_revision": "a" * 64,
        "digest": "d" * 64,
        "patch": base64.b64encode(b"canonical diff").decode(),
        "validation_results": ["passed"],
    }


def test_admin_json_requests_and_responses_reject_malformed_contract_values() -> None:
    codec = TokenCodec(b"k" * 32)
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        audits=MemoryAuditStore(),
        fleet=lambda: {"nodes": []},
        now=lambda: 10,
        admin=AdminServices(authority=Repository(), proposals=Proposals(), changes=None),
    )
    client = TestClient(app)
    token = codec.issue(Actor("admin", "administrator"), ttl_seconds=100, now=0)
    headers = {"Authorization": f"Bearer {token}"}
    document = {
        "base_revision": "a" * 64,
        "changes": [
            {"path": "inventory/topology.json", "document": {"schema_version": 2}}
        ],
    }

    assert (
        client.post(
            "/api/v1/proposals",
            headers=headers,
            json={**document, "unexpected": True},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/proposals",
            headers=headers,
            json={**document, "base_revision": 7},
        ).status_code
        == 422
    )
    with pytest.raises(ValueError):
        ProposalPreviewResponse(
            base_revision="a" * 64,
            digest="d" * 64,
            patch="eA==",
            affected_documents=["inventory/topology.json"],
            validation_results=["passed"],
            unexpected=True,
        )
    with pytest.raises(ValueError):
        ProposalPreviewResponse(
            base_revision="a" * 64,
            digest="d" * 64,
            patch="not-base64",
            affected_documents=["inventory/topology.json"],
            validation_results=["passed"],
        )
    with pytest.raises(ValueError):
        OperationDetailResponse(
            id="operation",
            node_ids=["spk_" + "a" * 32],
            kind="probe",
            state="queued",
            attempt="1",
            created_at=datetime.now(UTC).isoformat(),
        )


def test_authority_document_reads_require_authentication() -> None:
    codec = TokenCodec(b"k" * 32)
    app = create_app(jobs=Jobs(), tokens=codec, audits=MemoryAuditStore(), fleet=dict, admin=AdminServices(Repository(), Proposals(), None))
    client = TestClient(app)
    assert client.get("/api/v1/authority", params={"revision": "a" * 64}).status_code == 401


def test_spa_falls_back_to_index_for_client_routes_but_not_assets(tmp_path) -> None:
    from fastapi import FastAPI
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<h1>Admin</h1>")
    app = FastAPI()
    app.mount("/", SpaFiles(directory=web, html=True))
    client = TestClient(app)
    assert client.get("/profiles").text == "<h1>Admin</h1>"
    assert client.get("/missing.js").status_code == 404
