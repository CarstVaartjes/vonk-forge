from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from vonk_control import operation_api
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.library_placement_contract import (
    LibraryPlacementApplication,
    LibraryPlacementLocations,
    LibraryPlacementNode,
    LibraryPlacementPreview,
    LibraryPlacementStep,
)

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)
RECIPE = "00000000-0000-4000-8000-000000000001"
REVISION = "00000000-0000-4000-8000-000000000002"
APPLICATION = "00000000-0000-4000-8000-000000000003"
NODE = "spk_" + "1" * 32
DIGEST = "a" * 64
REQUEST_KEY = "00000000-0000-4000-8000-000000000004"


class Jobs:
    def list_page(self, **_kwargs):
        return [], None, 0


class Placements:
    def preview(self, value, *, actor):
        assert actor == "administrator"
        return LibraryPlacementPreview(
            generated_at=NOW,
            recipe_id=value.recipe_id,
            recipe_revision_id=REVISION,
            recipe_title="Tiny model",
            topology_name="solo",
            desired_state=value.desired_state,
            alias=value.alias,
            invocation=value.invocation,
            selected_node_ids=value.node_ids,
            selected_nodes=[
                LibraryPlacementNode(
                    node_id=NODE,
                    rank=0,
                    role="entrypoint",
                    endpoint_owner=True,
                    disk_free_bytes=1000,
                    disk_required_bytes=100,
                    disk_free_after_bytes=900,
                    memory_available_bytes=800,
                    memory_required_bytes=200,
                    memory_free_after_bytes=600,
                )
            ],
            allowed=True,
            steps=[
                LibraryPlacementStep(
                    index=0,
                    kind="install",
                    label="Install Tiny model",
                    node_ids=[NODE],
                )
            ],
            blockers=[],
            warnings=[],
            locations=LibraryPlacementLocations(
                installation_ids=[], run_ids=[], installed=False, running=False
            ),
            plan_digest=DIGEST,
        )

    def apply(self, value, *, actor):
        assert actor == "administrator"
        return self._application(value.plan_digest)

    def application(self, application_id):
        if application_id != APPLICATION:
            raise KeyError(application_id)
        return self._application(DIGEST)

    @staticmethod
    def _application(plan_digest: str):
        return LibraryPlacementApplication(
            id=APPLICATION,
            state="queued",
            recipe_id=RECIPE,
            recipe_revision_id=REVISION,
            selected_node_ids=[NODE],
            desired_state="installed",
            alias=None,
            plan_digest=plan_digest,
            current_step=0,
            total_steps=1,
            current_operation_id=None,
            status_reason=None,
            progress={"completed_steps": 0, "total_steps": 1},
            locations=LibraryPlacementLocations(
                installation_ids=[], run_ids=[], installed=False, running=False
            ),
            created_at=NOW,
            updated_at=NOW,
        )


def setup():
    codec = TokenCodec(b"z" * 32)
    audits = MemoryAuditStore()
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        audits=audits,
        fleet=lambda: {"nodes": []},
        library_placements=Placements(),
        now=lambda: 10,
    )

    def headers(role="administrator"):
        token = codec.issue(Actor(role, role), ttl_seconds=100, now=0)
        return {"Authorization": f"Bearer {token}"}

    return TestClient(app), headers, audits


def body(invocation="keyboard"):
    return {
        "recipe_id": RECIPE,
        "node_ids": [NODE],
        "desired_state": "installed",
        "alias": None,
        "invocation": invocation,
    }


def test_preview_and_apply_share_transport_neutral_contract_and_are_audited() -> None:
    client, headers, audits = setup()

    denied = client.post(
        "/api/v1/library/placements/preview",
        headers=headers("operator"),
        json=body(),
    )
    preview = client.post(
        "/api/v1/library/placements/preview", headers=headers(), json=body()
    )
    request_id = "00000000-0000-4000-8000-000000000005"
    applied = client.post(
        "/api/v1/library/placements",
        headers={**headers(), "x-request-id": request_id},
        json={**body("drag-drop"), "plan_digest": DIGEST, "request_key": REQUEST_KEY},
    )
    progress = client.get(
        f"/api/v1/library/placements/{APPLICATION}", headers=headers("viewer")
    )

    assert denied.status_code == 403
    assert preview.status_code == 200
    assert preview.json()["invocation"] == "keyboard"
    assert applied.status_code == 202
    assert progress.status_code == 200
    assert progress.json()["progress"] == {"completed_steps": 0, "total_steps": 1}
    audit = audits.for_request(request_id)
    assert audit.action == "library.placement.apply"
    assert audit.targets == (APPLICATION, DIGEST, NODE)


def test_openapi_exposes_typed_stable_placement_operations() -> None:
    client, _headers, _audits = setup()
    schema = operation_api.admin_openapi_schema(client.app)
    paths = schema["paths"]

    assert (
        paths["/api/v1/library/placements/preview"]["post"]["operationId"]
        == "previewLibraryPlacement"
    )
    assert (
        paths["/api/v1/library/placements"]["post"]["operationId"]
        == "applyLibraryPlacement"
    )
    assert (
        paths["/api/v1/library/placements/{placement_id}"]["get"]["operationId"]
        == "getLibraryPlacement"
    )
    assert "LibraryPlacementPreview" in schema["components"]["schemas"]
    assert "LibraryPlacementApplication" in schema["components"]["schemas"]
