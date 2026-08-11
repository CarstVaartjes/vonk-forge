from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from threading import Lock

import pytest
from fastapi.testclient import TestClient
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.operation_api import admin_openapi_schema
from vonk_control.package_api import PackageApiServices

CANDIDATE = "a" * 64
PREVIEW = "sha256:" + "b" * 64
PLAN = "sha256:" + "c" * 64
RELEASE = "sha256:" + "d" * 64
ROLLOUT = "10000000-0000-4000-8000-000000000001"
REQUEST_ID = "20000000-0000-4000-8000-000000000002"


class Jobs:
    def get(self, job_id: str):
        raise KeyError(job_id)

    def list(self, *, limit: int = 100):
        del limit
        return []

    def list_page(self, **_kwargs):
        return [], None, 0


@dataclass
class Packages:
    calls: list[tuple[object, ...]] = field(default_factory=list)
    promoted: dict[str, object] | None = None
    _idempotency: dict[tuple[str, str], tuple[tuple[object, ...], dict[str, object]]] = field(default_factory=dict)
    _idempotency_lock: Lock = field(default_factory=Lock)

    def idempotency(self, actor: str, request_id: str, fingerprint: tuple[object, ...], operation) -> dict[str, object]:
        with self._idempotency_lock:
            prior = self._idempotency.get((actor, request_id))
            if prior is not None:
                if prior[0] != fingerprint:
                    raise ValueError("conflicting request")
                return prior[1], True
            result = dict(operation())
            self._idempotency[(actor, request_id)] = (fingerprint, result)
            return result, False

    def families(self, cursor: str | None, limit: int) -> dict[str, object]:
        self.calls.append(("families", cursor, limit))
        return {
            "families": [
                {
                    "id": "synthetic-stack",
                    "promotion_mode": "manual",
                    "channels": ["stable"],
                }
            ],
            "next_cursor": None,
            "total": 1,
        }

    def candidates(
        self, family_id: str | None, cursor: str | None, limit: int
    ) -> dict[str, object]:
        self.calls.append(("candidates", family_id, cursor, limit))
        return {
            "candidates": [
                {
                    "id": CANDIDATE,
                    "family_id": "synthetic-stack",
                    "release_key": "v1",
                    "upstream_version": "1.0.0",
                    "state": "promotion-ready",
                    "reason_code": None,
                    "metadata": {"token": "never-returned", "source": "index"},
                    "release": {
                        "release_digest": RELEASE,
                        "lock_digest": "sha256:" + "f" * 64,
                        "components": [{"name": "runtime", "digest": RELEASE, "kind": "oci"}],
                        "dependencies": ["cuda-runtime"],
                        "provenance": [{"kind": "build", "digest": "sha256:" + "1" * 64}],
                    },
                }
            ],
            "next_cursor": None,
            "total": 1,
        }

    def candidate(self, candidate_id: str) -> dict[str, object]:
        if candidate_id != CANDIDATE:
            raise KeyError(candidate_id)
        return self.candidates(None, None, 20)["candidates"][0]

    def resolution(self, candidate_id: str) -> dict[str, object]:
        self.candidate(candidate_id)
        return {"candidate_id": candidate_id, "release_digest": RELEASE, "state": "resolved"}

    def compatibility(self, candidate_id: str) -> dict[str, object]:
        self.candidate(candidate_id)
        return {"candidate_id": candidate_id, "release_digest": RELEASE, "digest": PREVIEW, "compatible_node_ids": []}

    def validation_preview(self, candidate_id: str) -> dict[str, object]:
        self.candidate(candidate_id)
        return {"candidate_id": candidate_id, "release_digest": RELEASE, "digest": PREVIEW, "state": "planned"}

    def validate(self, candidate_id: str, digest: str, actor: str, request_id: str) -> dict[str, object]:
        self.calls.append(("validate", candidate_id, digest, actor, request_id))
        if digest != PREVIEW:
            raise KeyError(digest)
        return {"id": "30000000-0000-4000-8000-000000000003", "state": "planned", "plan_digest": digest, "progress": {"completed": 0, "failed": 0, "running": 0, "total": 2}}

    def validation_status(self, validation_id: str) -> dict[str, object]:
        return {"id": validation_id, "state": "failed", "plan_digest": PREVIEW, "failure": "Bearer validation-secret", "progress": {"completed": 0, "failed": 1, "running": 0, "total": 2}}

    def promotion_preview(self, candidate_id: str) -> dict[str, object]:
        self.candidate(candidate_id)
        return {"candidate_id": candidate_id, "release_digest": RELEASE, "digest": PREVIEW, "state": "ready"}

    def promote(self, candidate_id: str, digest: str, actor: str, request_id: str) -> dict[str, object]:
        self.calls.append(("promote", candidate_id, digest, actor, request_id))
        if digest != PREVIEW:
            raise KeyError(digest)
        result = {"candidate_id": candidate_id, "release_digest": RELEASE, "digest": digest, "state": "promoted"}
        self.promoted = result
        return result

    def deployments(self, cursor: str | None, limit: int) -> dict[str, object]:
        return {"deployments": [{"id": "synthetic-canary", "family_id": "synthetic-stack", "release_digest": RELEASE, "previous_release_digest": "sha256:" + "e" * 64, "state": "approved"}], "next_cursor": None, "total": 1}

    def deployment(self, deployment_id: str) -> dict[str, object]:
        return self.deployments(None, 20)["deployments"][0]

    def rollout_preview(self, deployment_id: str) -> dict[str, object]:
        return {"deployment_id": deployment_id, "release_digest": RELEASE, "digest": PLAN, "state": "ready", "batches": [["spk_" + "1" * 32]], "canary_node": "spk_" + "1" * 32, "offline_pending": [], "storage_bytes": 10, "download_bytes": 20}

    def rollout(self, deployment_id: str, digest: str, actor: str, request_id: str) -> dict[str, object]:
        self.calls.append(("rollout", deployment_id, digest, actor, request_id))
        if self.promoted is None or digest != PLAN:
            raise KeyError(digest)
        return {"id": ROLLOUT, "state": "planned", "plan_digest": digest, "job_id": "job-package-1", "audit_request_id": request_id, "progress": {"completed": 0, "failed": 0, "running": 0, "total": 1}, "nodes": [{"node_id": "spk_" + "1" * 32, "state": "pending", "batch_index": 0, "completed": 0, "total": 1}], "rollback_rollout_id": None, "rollback_selector": "retained"}

    def rollout_status(self, deployment_id: str, rollout_id: str, cursor: str | None, limit: int) -> dict[str, object]:
        return {"id": rollout_id, "state": "failed", "plan_digest": PLAN, "job_id": "job-package-1", "audit_request_id": REQUEST_ID, "failure": "token=rollout-secret", "progress": {"completed": 0, "failed": 1, "running": 0, "total": 1}, "nodes": [{"node_id": "spk_" + "1" * 32, "state": "pending", "batch_index": 0, "completed": 0, "total": 1}], "rollback_rollout_id": None, "rollback_selector": "retained"}

    def rollback_preview(self, deployment_id: str, rollout_id: str) -> dict[str, object]:
        return {"deployment_id": deployment_id, "release_digest": RELEASE, "digest": PLAN, "state": "ready"}

    def rollback(self, deployment_id: str, rollout_id: str, digest: str, actor: str, request_id: str) -> dict[str, object]:
        return self.rollout(deployment_id, digest, actor, request_id)

    def repair_preview(self, deployment_id: str) -> dict[str, object]:
        return self.rollout_preview(deployment_id)

    def repair(self, deployment_id: str, digest: str, actor: str, request_id: str) -> dict[str, object]:
        return self.rollout(deployment_id, digest, actor, request_id)

    def gc_preview(self) -> dict[str, object]:
        return {"digest": PLAN, "state": "ready", "reclaim_bytes": 0}

    def gc(self, digest: str, actor: str, request_id: str) -> dict[str, object]:
        if digest != PLAN:
            raise KeyError(digest)
        return {"id": "40000000-0000-4000-8000-000000000004", "state": "planned", "plan_digest": digest, "progress": {"completed": 0, "failed": 0, "running": 0, "total": 0}}

    def inventory(
        self,
        node_id: str | None,
        deployment_id: str | None,
        cursor: str | None,
        limit: int,
    ) -> dict[str, object]:
        del cursor, limit
        node = "spk_" + "1" * 32
        if node_id is not None and node_id != node:
            return {"nodes": [], "next_cursor": None, "total": 0}
        if deployment_id is not None and deployment_id != "synthetic-canary":
            return {"nodes": [], "next_cursor": None, "total": 0}
        return {
            "nodes": [
                {
                    "node_id": node,
                    "online": True,
                    "observed_at": "2026-08-06T12:00:00Z",
                    "storage": {
                        "total_bytes": 1000,
                        "used_bytes": 400,
                        "free_bytes": 600,
                        "reserved_bytes": 100,
                        "reclaimable_bytes": 50,
                    },
                    "resources": {
                        "host_memory_total_bytes": 1000,
                        "host_memory_free_bytes": 700,
                        "gpu_memory_total_bytes": 800,
                        "gpu_memory_free_bytes": 500,
                        "gpu_count": 1,
                    },
                    "current_generation": RELEASE,
                    "packages": [
                        {
                            "deployment_id": "synthetic-canary",
                            "family_id": "synthetic-stack",
                            "release_digest": RELEASE,
                            "content_group": "weights",
                            "state": "available",
                            "bytes_total": 200,
                            "bytes_complete": 200,
                            "bytes_remaining": 0,
                            "installed_bytes": 200,
                            "reclaimable_bytes": 200,
                            "reserved_bytes": 0,
                            "active": False,
                            "retained": False,
                            "leased": False,
                            "operation_id": None,
                            "last_operation_state": "completed",
                            "last_operation_error": None,
                            "resources": {
                                "download_bytes": 200,
                                "installed_bytes": 200,
                                "transient_bytes": 20,
                                "output_bytes": 0,
                                "host_memory_bytes": 100,
                                "resident_memory_bytes": 100,
                                "auxiliary_memory_bytes": 0,
                                "activation_memory_bytes": 0,
                                "workspace_memory_bytes": 0,
                                "gpu_memory_bytes": 300,
                                "gpu_count": 1,
                                "cpu_millicores": 1,
                                "kv_cache_base_bytes": 40,
                                "kv_cache_per_token_bytes": 1,
                                "required_nodes": 1,
                                "topology": "single",
                                "world_size": 1,
                                "ranks": [{"rank": 0, "role": "primary"}],
                                "fabric": {"kind": "none", "min_bandwidth_mbps": 0},
                            },
                        }
                    ],
                }
            ],
            "next_cursor": None,
            "total": 1,
        }

    def removal_preview(
        self, deployment_id: str, release_digest: str, node_ids: tuple[str, ...]
    ) -> dict[str, object]:
        assert deployment_id == "synthetic-canary"
        assert release_digest == RELEASE
        return {
            "digest": PLAN,
            "state": "ready",
            "deployment_id": deployment_id,
            "release_digest": release_digest,
            "nodes": [
                {
                    "node_id": node_ids[0],
                    "state": "removable",
                    "active": False,
                    "retained": False,
                    "leased": False,
                    "reclaimable_bytes": 200,
                    "dependencies": [],
                    "blocked_reason": None,
                }
            ],
            "reclaimable_bytes": 200,
            "blocked_nodes": [],
        }

    def remove(self, digest: str, actor: str, request_id: str) -> dict[str, object]:
        self.calls.append(("remove", digest, actor, request_id))
        if digest != PLAN:
            raise KeyError(digest)
        return {
            "id": "50000000-0000-4000-8000-000000000005",
            "state": "planned",
            "plan_digest": digest,
            "progress": {"completed": 0, "failed": 0, "running": 0, "total": 1},
        }


def _client(role: str, service: Packages | None = None):
    codec = TokenCodec(b"x" * 32)
    packages = service or Packages()
    audits = MemoryAuditStore()
    app = create_app(jobs=Jobs(), tokens=codec, audits=audits, fleet=lambda: {"nodes": []}, now=lambda: 10, packages=PackageApiServices.from_object(packages))
    token = codec.issue(Actor(role, role), ttl_seconds=100, now=0)
    return TestClient(app), {"Authorization": f"Bearer {token}"}, packages, audits


def test_package_api_enforces_roles_redacts_metadata_and_requires_exact_digests() -> None:
    # Break caught: a viewer can mutate, manual promotion accepts an unreviewed
    # preview, or a proxy leaks discovery credentials in routine projections.
    viewer, viewer_headers, _, _ = _client("viewer")
    operator, operator_headers, packages, _ = _client("operator")
    admin, admin_headers, _, audits = _client("administrator", packages)

    listed = viewer.get("/api/v1/packages/candidates?family_id=synthetic-stack&limit=1", headers=viewer_headers)
    assert listed.status_code == 200
    assert listed.json()["candidates"][0]["metadata"] == {"source": "index"}
    release = listed.json()["candidates"][0]["release"]
    assert release["release_digest"] == RELEASE
    assert release["components"] == [{"name": "runtime", "digest": RELEASE, "kind": "oci"}]
    assert viewer.post(f"/api/v1/packages/candidates/{CANDIDATE}/promote", headers=viewer_headers, json={"preview_digest": PREVIEW}).status_code == 403
    assert operator.post(f"/api/v1/packages/candidates/{CANDIDATE}/promote", headers=operator_headers, json={"preview_digest": PREVIEW}).status_code == 403

    preview = admin.post(f"/api/v1/packages/candidates/{CANDIDATE}/promotion-preview", headers=admin_headers).json()
    stale = admin.post(f"/api/v1/packages/candidates/{CANDIDATE}/promote", headers={**admin_headers, "x-request-id": REQUEST_ID}, json={"preview_digest": "sha256:" + "e" * 64})
    first = admin.post(f"/api/v1/packages/candidates/{CANDIDATE}/promote", headers={**admin_headers, "x-request-id": REQUEST_ID}, json={"preview_digest": preview["digest"]})
    duplicate = admin.post(f"/api/v1/packages/candidates/{CANDIDATE}/promote", headers={**admin_headers, "x-request-id": REQUEST_ID}, json={"preview_digest": preview["digest"]})

    assert stale.status_code == 409
    assert first.status_code == 202
    assert duplicate.status_code == 202
    assert first.json() == duplicate.json()
    assert packages.calls.count(("promote", CANDIDATE, PREVIEW, "administrator", REQUEST_ID)) == 1
    assert (audits.for_request(REQUEST_ID).action, audits.for_request(REQUEST_ID).actor) == ("package.promote", "administrator")
    assert audits.for_request(REQUEST_ID).targets == (CANDIDATE,)


def test_package_mutations_bound_validation_errors_and_replay_to_the_same_request() -> None:
    # Break caught: validation reflects a supplied secret, or an ID reused for a
    # different immutable command silently replays the earlier result.
    client, headers, packages, _ = _client("administrator")
    secret = "Bearer " + "s" * 2_000

    invalid = client.post(
        f"/api/v1/packages/candidates/{CANDIDATE}/promote",
        headers=headers,
        json={"preview_digest": PREVIEW, "authorization": secret},
    )
    first = client.post(
        f"/api/v1/packages/candidates/{CANDIDATE}/promote",
        headers={**headers, "x-request-id": REQUEST_ID},
        json={"preview_digest": PREVIEW},
    )
    exact_replay = client.post(
        f"/api/v1/packages/candidates/{CANDIDATE}/promote",
        headers={**headers, "x-request-id": REQUEST_ID},
        json={"preview_digest": PREVIEW},
    )
    conflict = client.post(
        f"/api/v1/packages/candidates/{CANDIDATE}/promote",
        headers={**headers, "x-request-id": REQUEST_ID},
        json={"preview_digest": "sha256:" + "e" * 64},
    )

    assert invalid.status_code == 422
    assert invalid.json() == {"detail": "package request is invalid"}
    assert len(invalid.json()["detail"]) <= 256
    assert "authorization" not in invalid.text.lower()
    assert secret not in invalid.text
    assert first.status_code == exact_replay.status_code == 202
    assert first.json() == exact_replay.json()
    assert conflict.status_code == 409
    assert packages.calls.count(("promote", CANDIDATE, PREVIEW, "administrator", REQUEST_ID)) == 1
    documented = client.get("/openapi.json").json()
    assert documented["paths"]["/api/v1/packages/candidates/{candidate_id}/promote"]["post"]["responses"]["422"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/BoundedErrorResponse"}


def test_package_operation_ids_are_registered_when_the_app_is_mounted() -> None:
    # Break caught: a process that constructs the API outside the generator
    # cannot derive the authenticated schema due to missing package operation IDs.
    client, _headers, _packages, _audits = _client("viewer")

    schema = admin_openapi_schema(client.app)

    assert schema["paths"]["/api/v1/packages/candidates"]["get"]["operationId"] == "listPackageCandidates"


def test_inventory_projects_node_storage_resource_and_package_lifecycle() -> None:
    client, headers, _packages, _audits = _client("viewer")

    response = client.get(
        "/api/v1/packages/inventory?node_id=spk_11111111111111111111111111111111",
        headers=headers,
    )

    assert response.status_code == 200
    node = response.json()["nodes"][0]
    assert node["storage"] == {
        "total_bytes": 1000,
        "used_bytes": 400,
        "free_bytes": 600,
        "reserved_bytes": 100,
        "reclaimable_bytes": 50,
    }
    package = node["packages"][0]
    assert package["state"] == "available"
    assert package["bytes_remaining"] == 0
    assert package["resources"]["kv_cache_per_token_bytes"] == 1


def test_removal_requires_operator_and_digest_bound_preview() -> None:
    viewer, viewer_headers, _packages, _audits = _client("viewer")
    operator, operator_headers, packages, audits = _client("operator")
    node = "spk_" + "1" * 32
    body = {"deployment_id": "synthetic-canary", "release_digest": RELEASE, "node_ids": [node]}

    denied = viewer.post("/api/v1/packages/inventory/remove-preview", headers=viewer_headers, json=body)
    assert denied.status_code == 403

    preview = operator.post(
        "/api/v1/packages/inventory/remove-preview", headers=operator_headers, json=body
    )
    assert preview.status_code == 200
    assert preview.json()["reclaimable_bytes"] == 200
    assert preview.json()["nodes"][0]["state"] == "removable"

    stale = operator.post(
        "/api/v1/packages/inventory/remove",
        headers=operator_headers,
        json={"plan_digest": "sha256:" + "e" * 64},
    )
    applied = operator.post(
        "/api/v1/packages/inventory/remove",
        headers={**operator_headers, "x-request-id": REQUEST_ID},
        json={"plan_digest": preview.json()["digest"]},
    )
    assert stale.status_code == 409
    assert applied.status_code == 202
    assert packages.calls.count(("remove", PLAN, "operator", REQUEST_ID)) == 1
    assert audits.for_request(REQUEST_ID).action == "package.remove"


def test_package_progress_reads_redact_service_failure_details() -> None:
    # Break caught: a backend failure containing credentials is exposed by a
    # routine validation or rollout status read.
    client, headers, _packages, _audits = _client("viewer")

    validation = client.get("/api/v1/packages/validations/30000000-0000-4000-8000-000000000003", headers=headers)
    rollout = client.get(f"/api/v1/deployments/synthetic-canary/rollouts/{ROLLOUT}", headers=headers)

    assert validation.json()["failure"] == rollout.json()["failure"] == "package operation failed"
    assert "secret" not in validation.text + rollout.text


def test_atomic_idempotency_boundary_replays_concurrent_exact_mutations_once() -> None:
    # Break caught: concurrent retries dispatch the same immutable promotion
    # twice, or a same-ID request with a changed digest is accepted.
    packages = Packages()
    fingerprint = ("administrator", "package.promote", (CANDIDATE,), PREVIEW)
    calls = 0

    def operation() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"candidate_id": CANDIDATE, "release_digest": RELEASE, "digest": PREVIEW, "state": "promoted"}

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: packages.idempotency("administrator", REQUEST_ID, fingerprint, operation), range(2)))

    assert calls == 1
    assert results[0][0] == results[1][0]
    with pytest.raises(ValueError):
        packages.idempotency("administrator", REQUEST_ID, ("administrator", "package.promote", (CANDIDATE,), PLAN), operation)


def test_operator_rolls_out_only_an_approved_exact_plan_and_routes_are_bounded() -> None:
    # Break caught: rollout bypasses promotion, changes the server plan digest,
    # or accepts unbounded pagination values.
    client, headers, packages, _ = _client("administrator")
    promoted = client.post(f"/api/v1/packages/candidates/{CANDIDATE}/promote", headers=headers, json={"preview_digest": PREVIEW})
    assert promoted.status_code == 202
    operator, operator_headers, _, _ = _client("operator", packages)
    preview = operator.post("/api/v1/deployments/synthetic-canary/rollout-preview", headers=operator_headers)
    applied = operator.post("/api/v1/deployments/synthetic-canary/rollouts", headers={**operator_headers, "x-request-id": REQUEST_ID}, json={"plan_digest": preview.json()["digest"]})

    assert applied.status_code == 202
    assert applied.json()["plan_digest"] == PLAN
    assert preview.json()["batches"] == [["spk_" + "1" * 32]]
    assert applied.json()["nodes"][0]["state"] == "pending"
    assert operator.get("/api/v1/packages/families?limit=101", headers=operator_headers).status_code == 422
    assert operator.get("/api/v1/packages/candidates?cursor=" + "x" * 513, headers=operator_headers).status_code == 422
    schema = operator.get("/openapi.json").json()
    assert all("payload" not in path and "upload" not in path for path in schema["paths"])
