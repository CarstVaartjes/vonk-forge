import base64

import pytest
from fastapi.testclient import TestClient
from vonk_control.api import AdminServices, SpaFiles, create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.proposals import ProposalPreview
from vonk_control.reconcile import IneligibleCommit
from vonk_control.repository import RepositoryPolicyError


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


class Reconciler:
    def plan(self, commit, profile_id, **_kwargs):
        return type(
            "Plan",
            (),
            {
                "commit": commit,
                "digest": "d" * 64,
                "targets": ("spk_" + "1" * 32,),
                "placements": {"model": ("spk_" + "1" * 32,)},
                "routes": {},
                "releases": {},
                "input_digests": {},
                "operation_graph": type(
                    "Graph",
                    (),
                    {
                        "document": {
                            "schema_version": 1,
                            "base_commit": commit,
                            "targets": ["spk_" + "1" * 32],
                            "nodes": [],
                        },
                        "reconciliation_id": "reconciliation-1",
                    },
                )(),
                "agent_protocol_range": (1, 1),
            },
        )()


class Cancellations:
    def __init__(self) -> None:
        self.calls = []

    def enqueue_cancel(self, reconciliation_id, reason, *, actor, request_id):
        self.calls.append((reconciliation_id, reason, actor, request_id))
        return type(
            "Cancellation",
            (),
            {"reconciliation_id": reconciliation_id, "state": "requested"},
        )()


def test_admin_proposal_returns_canonical_patch_and_digest() -> None:
    codec = TokenCodec(b"k" * 32)
    app = create_app(
        jobs=Jobs(), tokens=codec, audits=MemoryAuditStore(), fleet=lambda: {"nodes": []}, now=lambda: 10,
        admin=AdminServices(repository=Repository(), proposals=Proposals(), changes=None, reconciler=None),
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
    app = create_app(jobs=Jobs(), tokens=codec, audits=MemoryAuditStore(), fleet=dict, admin=AdminServices(Repository(), Proposals(), None, None))
    client = TestClient(app)
    assert client.get("/api/v1/repository", params={"commit": "a" * 40}).status_code == 401


def test_reconciliation_plan_requires_an_explicit_repository_profile() -> None:
    codec = TokenCodec(b"k" * 32)
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        audits=MemoryAuditStore(),
        fleet=lambda: {"commit": "a" * 40, "nodes": []},
        admin=AdminServices(Repository(), Proposals(), None, Reconciler()),
        now=lambda: 10,
    )
    client = TestClient(app)
    token = codec.issue(Actor("operator", "operator"), ttl_seconds=100, now=0)
    headers = {"Authorization": f"Bearer {token}"}

    response = client.post(
        "/api/v1/reconciliations/plan",
        headers=headers,
        json={"commit": "a" * 40, "profile_id": "inference"},
    )
    assert response.status_code == 200
    assert response.json()["agent_protocol_range"] == [1, 1]
    assert response.json()["reconciliation_id"] == "reconciliation-1"
    assert response.json()["operation_graph"] == {
        "schema_version": 1,
        "base_commit": "a" * 40,
        "targets": ["spk_" + "1" * 32],
        "nodes": [],
    }
    assert client.post(
        "/api/v1/reconciliations/plan", headers=headers, json={"commit": "a" * 40}
    ).status_code == 422


def test_reconciliation_cancellation_is_rbac_guarded_and_audited() -> None:
    codec = TokenCodec(b"k" * 32)
    audits = MemoryAuditStore()
    cancellations = Cancellations()
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        audits=audits,
        fleet=dict,
        admin=AdminServices(
            Repository(), Proposals(), None, Reconciler(), cancellations
        ),
        now=lambda: 10,
    )
    client = TestClient(app)
    reconciliation_id = "11111111-1111-4111-8111-111111111111"
    request_id = "22222222-2222-4222-8222-222222222222"
    operator = codec.issue(Actor("operator", "operator"), ttl_seconds=100, now=0)
    viewer = codec.issue(Actor("viewer", "viewer"), ttl_seconds=100, now=0)

    denied = client.post(
        f"/api/v1/reconciliations/{reconciliation_id}/cancel",
        headers={"Authorization": f"Bearer {viewer}"},
        json={"reason": "operator requested rollback"},
    )
    response = client.post(
        f"/api/v1/reconciliations/{reconciliation_id}/cancel",
        headers={
            "Authorization": f"Bearer {operator}",
            "X-Request-ID": request_id,
        },
        json={"reason": "operator requested rollback"},
    )

    assert denied.status_code == 403
    assert response.status_code == 202
    assert response.json() == {
        "reconciliation_id": reconciliation_id,
        "state": "requested",
    }
    assert cancellations.calls == [
        (
            reconciliation_id,
            "operator requested rollback",
            "operator",
            request_id,
        )
    ]
    assert audits.for_request(request_id).action == "reconciliation.cancel.request"


@pytest.mark.parametrize(
    ("error", "status_code", "detail"),
    [
        (IneligibleCommit("secret detail"), 409, "commit is not eligible"),
        (
            RepositoryPolicyError("secret detail"),
            422,
            "repository desired state is invalid",
        ),
        (ValueError("secret detail"), 422, "desired state cannot be planned"),
    ],
)
def test_expected_planning_rejections_are_stable_bounded_client_errors(
    error, status_code: int, detail: str
) -> None:
    class RejectingReconciler:
        def plan(self, commit, profile_id, **_kwargs):
            raise error

    codec = TokenCodec(b"k" * 32)
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        audits=MemoryAuditStore(),
        fleet=lambda: {"commit": "a" * 40, "nodes": []},
        admin=AdminServices(Repository(), Proposals(), None, RejectingReconciler()),
        now=lambda: 10,
    )
    client = TestClient(app)
    token = codec.issue(Actor("operator", "operator"), ttl_seconds=100, now=0)

    response = client.post(
        "/api/v1/reconciliations/plan",
        headers={"Authorization": f"Bearer {token}"},
        json={"commit": "a" * 40, "profile_id": "inference"},
    )

    assert response.status_code == status_code
    assert response.json() == {"detail": detail}
    assert "secret" not in response.text


def test_unexpected_planning_errors_remain_server_errors() -> None:
    class BrokenReconciler:
        def plan(self, commit, profile_id, **_kwargs):
            raise AssertionError("programming defect")

    codec = TokenCodec(b"k" * 32)
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        audits=MemoryAuditStore(),
        fleet=lambda: {"commit": "a" * 40, "nodes": []},
        admin=AdminServices(Repository(), Proposals(), None, BrokenReconciler()),
        now=lambda: 10,
    )
    client = TestClient(app, raise_server_exceptions=False)
    token = codec.issue(Actor("operator", "operator"), ttl_seconds=100, now=0)

    response = client.post(
        "/api/v1/reconciliations/plan",
        headers={"Authorization": f"Bearer {token}"},
        json={"commit": "a" * 40, "profile_id": "inference"},
    )

    assert response.status_code == 500


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
