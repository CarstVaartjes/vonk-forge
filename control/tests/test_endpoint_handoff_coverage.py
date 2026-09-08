"""Additional successful API handoffs for the current schema-2 routes.

The tests in this module exercise production ``create_app`` routes and then
consume the returned bytes with the same typed response models used by API
clients.  The small stateful doubles below stand in for a durable service
boundary only for route families that otherwise have no successful client
handoff witness.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.model_cache_progress import cache_progress
from vonk_control.recipe_image_availability import RecipeImageAvailabilityView
from vonk_control.recipe_image_availability_api import RecipeImageAvailabilityResponse
from vonk_control.run_switch_contract import RunSwitchApplyRequest


class Jobs:
    def list(self, **_kwargs):
        return []

    def get(self, _job_id):
        raise KeyError


def _headers(codec: TokenCodec, role: str = "administrator") -> dict[str, str]:
    token = codec.issue(Actor(role, role), ttl_seconds=3600, now=0)
    return {"Authorization": f"Bearer {token}"}


def test_profile_handoff_covers_persisted_view_update_apply_and_delete() -> None:
    from .test_fleet_profile_api import _body, _setup

    client, headers, _audits = _setup()
    created = client.post("/api/v1/fleet-profiles", headers=headers(), json=_body())
    assert created.status_code == 201, created.text
    profile_id = created.json()["id"]

    fetched = client.get(f"/api/v1/fleet-profiles/{profile_id}", headers=headers("viewer"))
    assert fetched.status_code == 200
    assert fetched.json()["id"] == profile_id

    updated = client.put(
        f"/api/v1/fleet-profiles/{profile_id}",
        headers=headers(),
        json={**_body(), "name": "Updated studio profile"},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Updated studio profile"

    preview = client.post(
        f"/api/v1/fleet-profiles/{profile_id}/preview",
        headers=headers(),
        json={},
    )
    assert preview.status_code == 200, preview.text
    applied = client.post(
        f"/api/v1/fleet-profiles/{profile_id}/apply",
        headers=headers(),
        json={
            "plan_digest": preview.json()["plan_digest"],
            "request_key": str(uuid4()),
        },
    )
    assert applied.status_code == 202, applied.text
    application = client.get(
        f"/api/v1/fleet-profile-applications/{applied.json()['id']}",
        headers=headers("viewer"),
    )
    assert application.status_code == 200
    assert application.json()["profile_id"] == profile_id

    # Capture-current has no active assignment in this seeded fixture, which
    # gives the delete route an independently valid, inactive profile target.
    captured = client.post(
        "/api/v1/fleet-profiles/capture-current",
        headers=headers(),
        json={"name": "Disposable current state"},
    )
    assert captured.status_code == 201
    deleted = client.delete(
        f"/api/v1/fleet-profiles/{captured.json()['id']}", headers=headers()
    )
    assert deleted.status_code == 204


def test_run_switch_handoff_covers_preview_apply_read_and_cancel(tmp_path) -> None:
    from .test_recipe_operations import NOW, setup_services
    from .test_run_switch_operations import RecordingArtifactExecutor, _request, _service

    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(tmp_path)
    service = _service(sessions, NOW, lifecycle, RecordingArtifactExecutor())
    preview_request = _request(sessions, nodes[0])
    codec = TokenCodec(b"run-switch-response-handoff-signing-key")
    audits = MemoryAuditStore()

    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        audits=audits,
        now=lambda: 10,
        run_switch_operations=service,
    )
    client = TestClient(app)
    preview = client.post(
        "/api/v1/recipes/run-switch-plans/preview",
        headers=_headers(codec),
        json=preview_request.model_dump(mode="json"),
    )
    assert preview.status_code == 200, preview.text
    apply_body = RunSwitchApplyRequest(
        **preview_request.model_dump(),
        plan_digest=preview.json()["plan_digest"],
        request_key=str(uuid4()),
    )
    applied = client.post(
        "/api/v1/recipes/run-switches",
        headers=_headers(codec),
        json=apply_body.model_dump(mode="json"),
    )
    assert applied.status_code == 202, applied.text
    operation_id = applied.json()["operation_id"]
    fetched = client.get(
        f"/api/v1/recipes/run-switches/{operation_id}", headers=_headers(codec, "viewer")
    )
    assert fetched.status_code == 200
    assert fetched.json()["operation_id"] == operation_id

    cancelled = client.post(
        f"/api/v1/recipes/run-switches/{operation_id}/cancel",
        headers=_headers(codec),
        json={"request_key": str(uuid4()), "reason": "handoff coverage"},
    )
    assert cancelled.status_code == 202, cancelled.text
    assert cancelled.json()["operation_id"] == operation_id


class _StoredAvailability:
    """Tiny durable-service seam for exercising all availability consumers."""

    def __init__(self) -> None:
        self._rows: dict[str, RecipeImageAvailabilityView] = {}

    def _view(self, *, operation_id: str, request_id: str, attempt: int = 1):
        return RecipeImageAvailabilityView(
            id=operation_id,
            request_id=request_id,
            kind="recipe.image.availability.v2",
            state="queued",
            attempt=attempt,
            recipe_revision_id="recipe-revision",
            recipe_content_sha256="a" * 64,
            model_digest=None,
            build_input_sha256=None,
            progress={"phase": "prepare", "total_bytes_known": False},
            image_progress=None,
            result=None,
            failure=None,
            supported_actions=("retry",),
            created_at="2026-09-08T10:00:00+00:00",
            updated_at="2026-09-08T10:00:00+00:00",
        )

    def start(self, recipe_revision_id, *, actor, request_id, force=False):
        del actor, force
        assert recipe_revision_id == "recipe-revision"
        value = self._view(operation_id=str(uuid4()), request_id=request_id)
        self._rows[value.id] = value
        return value

    def get(self, operation_id):
        return self._rows[operation_id]

    def list_page(self, *, recipe_revision_id=None, state=None, limit=50, boundary=None):
        del recipe_revision_id, state, limit, boundary
        rows = list(self._rows.values())
        return rows, len(rows), None

    def retry(self, operation_id, *, actor, request_id):
        del actor
        previous = self._rows[operation_id]
        value = self._view(
            operation_id=str(uuid4()), request_id=request_id, attempt=previous.attempt + 1
        )
        self._rows[value.id] = value
        return value


def test_recipe_availability_handoff_covers_start_list_read_and_retry() -> None:
    codec = TokenCodec(b"recipe-availability-response-handoff-key")
    availability = _StoredAvailability()
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        audits=MemoryAuditStore(),
        now=lambda: 10,
        recipe_image_availability=availability,
    )
    client = TestClient(app)
    started = client.post(
        "/api/v1/library/recipe-image-availability",
        headers=_headers(codec),
        json={"request_key": str(uuid4()), "recipe_revision_id": "recipe-revision"},
    )
    assert started.status_code == 202, started.text
    RecipeImageAvailabilityResponse.model_validate_json(started.content)
    operation_id = started.json()["id"]

    listed = client.get(
        "/api/v1/library/recipe-image-availability", headers=_headers(codec, "viewer")
    )
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    fetched = client.get(
        f"/api/v1/library/recipe-image-availability/{operation_id}",
        headers=_headers(codec, "viewer"),
    )
    assert fetched.status_code == 200
    retried = client.post(
        f"/api/v1/library/recipe-image-availability/{operation_id}/retry",
        headers=_headers(codec),
        json={"request_key": str(uuid4())},
    )
    assert retried.status_code == 202, retried.text
    assert retried.json()["attempt"] == 2


class _StoredModelCache:
    """Stateful model-cache service boundary for typed route consumers."""

    def __init__(self) -> None:
        self.operation = self._operation()

    @staticmethod
    def _operation():
        now = datetime(2026, 9, 8, 10, tzinfo=UTC)
        progress = cache_progress(
            {
                "phase": "queued",
                "completed_artifacts": 0,
                "total_artifacts": 0,
                "downloaded_bytes": 0,
                "expected_bytes": None,
                "current_artifact_key": None,
            },
            previous=None,
            now=now,
        )
        return SimpleNamespace(
            id=str(uuid4()),
            request_key=str(uuid4()),
            kind="download",
            state="queued",
            attempt=1,
            artifact_set_sha256="a" * 64,
            plan_digest="b" * 64,
            progress=progress,
            result=None,
            failure=None,
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
            completed_at=None,
        )

    def inventory(self, *, limit=100, boundary=None):
        del limit, boundary
        return {
            "entries": [],
            "storage": {
                "schema_version": 2,
                "total_bytes": 1000,
                "free_bytes": 1000,
                "reserve_bytes": 0,
                "available_bytes": 1000,
                "unique_used_bytes": 0,
                "in_flight_bytes": 0,
                "protected_bytes": 0,
                "reclaimable_bytes": 0,
            },
            "total": 0,
            "_next_boundary": None,
        }

    def operations_page(self, *, limit=100, boundary=None):
        del limit, boundary
        return {"operations": [self.operation], "total": 1, "_next_boundary": None}

    def discover_updates(self, *, artifact_set_sha256=None, limit=100, check_upstream=False, boundary=None):
        del artifact_set_sha256, limit, check_upstream, boundary
        return {"updates": [], "total": 0, "_next_boundary": None}

    def download_preview(self, **_kwargs):
        return {
            "artifact_set_sha256": "a" * 64,
            "plan_digest": "b" * 64,
            "artifact_count": 0,
            "expected_bytes": 0,
            "already_cached_bytes": 0,
            "new_bytes": 0,
            "blockers": [],
            "warnings": [],
        }

    def start_download(self, **_kwargs):
        return self.operation

    def get_operation(self, operation_id):
        if operation_id != self.operation.id:
            raise KeyError(operation_id)
        return self.operation


def test_model_cache_handoff_covers_inventory_preview_operations_and_client_read() -> None:
    codec = TokenCodec(b"model-cache-response-handoff-signing-key")
    cache = _StoredModelCache()
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        audits=MemoryAuditStore(),
        now=lambda: 10,
        model_cache=cache,
    )
    client = TestClient(app)
    headers = _headers(codec)
    assert client.get("/api/v1/model-cache", headers=headers).status_code == 200
    assert client.get("/api/v1/model-cache/operations", headers=headers).status_code == 200
    assert client.get("/api/v1/model-cache/updates", headers=headers).status_code == 200
    preview = client.post(
        "/api/v1/model-cache/download-preview",
        headers=headers,
        json={"artifact_set_sha256": "a" * 64},
    )
    assert preview.status_code == 200, preview.text
    downloaded = client.post(
        "/api/v1/model-cache/download",
        headers=headers,
        json={
            "request_key": cache.operation.request_key,
            "plan_digest": "b" * 64,
            "artifact_set_sha256": "a" * 64,
        },
    )
    assert downloaded.status_code == 202, downloaded.text
    fetched = client.get(
        f"/api/v1/model-cache/operations/{cache.operation.id}", headers=headers
    )
    assert fetched.status_code == 200
    assert fetched.json()["id"] == cache.operation.id
