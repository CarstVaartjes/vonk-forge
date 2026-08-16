import base64

from fastapi.testclient import TestClient
from vonk_control.api import AdminServices, SpaFiles, create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.proposals import ProposalPreview


class Jobs:
    def enqueue(self, *args, **kwargs): raise AssertionError
    def get(self, job_id): raise KeyError


class Repository:
    def inspect(self, commit):
        return type("Snapshot", (), {"commit": commit, "documents": {"inventory/fleet.toml": "blob"}, "dependencies": {}})()
    def read_document(self, commit, path):
        return type("Document", (), {"commit": commit, "path": path, "sha256": "hash", "parsed": {"schema_version": 2}})()


class Proposals:
    def preview(self, actor, base, changes):
        assert actor == "admin"
        return ProposalPreview(actor, base, b"canonical diff", tuple(change.path for change in changes), ("passed",), "d" * 64)


def test_admin_proposal_returns_canonical_patch_and_digest() -> None:
    codec = TokenCodec(b"k" * 32)
    app = create_app(
        jobs=Jobs(), tokens=codec, audits=MemoryAuditStore(), fleet=lambda: {"nodes": []}, now=lambda: 10,
        admin=AdminServices(repository=Repository(), proposals=Proposals(), changes=None),
    )
    client = TestClient(app)
    token = codec.issue(Actor("admin", "administrator"), ttl_seconds=100, now=0)
    response = client.post("/api/v1/proposals", headers={"Authorization": f"Bearer {token}"}, json={
        "base_commit": "a" * 40,
        "changes": [{"path": "inventory/fleet.toml", "document": {"schema_version": 2}}],
    })
    assert response.status_code == 200
    assert response.json() == {
        "affected_documents": ["inventory/fleet.toml"],
        "base_commit": "a" * 40,
        "digest": "d" * 64,
        "patch": base64.b64encode(b"canonical diff").decode(),
        "validation_results": ["passed"],
    }


def test_repository_document_reads_require_authentication() -> None:
    codec = TokenCodec(b"k" * 32)
    app = create_app(jobs=Jobs(), tokens=codec, audits=MemoryAuditStore(), fleet=dict, admin=AdminServices(Repository(), Proposals(), None))
    client = TestClient(app)
    assert client.get("/api/v1/repository", params={"commit": "a" * 40}).status_code == 401


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
