"""Cross-client parity against one composed Controller application.

The acceptance uses the public route providers and the CLI's existing request
seam.  Values are compared from the responses returned by the app; the test
does not rebuild a second client-side DTO.
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("vonk_control.model_cache")

from fastapi.testclient import TestClient
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.operation_api import (
    OperationApiServices,
    OperationListPage,
    OperationProvider,
)
from vonk_control.recipe_operations import RecipeOperationView

from cluster_profiles import cli
from cluster_profiles.control_client import ControlHTTPError, ControlTransportError

NODE = "spk_" + "1" * 32
MODEL = "a" * 64
ARTIFACT_SET = "b" * 64
RECIPE = "00000000-0000-4000-8000-000000000001"
PROFILE = "00000000-0000-4000-8000-000000000002"
MAPPING = "00000000-0000-4000-8000-000000000003"
PLAN = "c" * 64
NOW = "2026-09-05T12:00:00+00:00"
NOW_DT = datetime(2026, 9, 5, 12, tzinfo=UTC)
REQUEST_NAMESPACE = uuid.UUID("00000000-0000-4000-8000-000000000010")


class _Jobs:
    def list_page(self, **_kwargs: object) -> tuple[list[object], None, int]:
        return [], None, 0

    def get(self, _job_id: str) -> object:
        raise KeyError(_job_id)


class _Ledger:
    def __init__(self) -> None:
        self.operations: dict[str, dict[str, object]] = {}
        self.by_request_key: dict[str, str] = {}

    def id_for(self, request_key: str, prefix: str) -> str:
        value = self.by_request_key.get(request_key)
        if value is None:
            value = str(uuid.uuid5(REQUEST_NAMESPACE, f"{prefix}:{request_key}"))
            self.by_request_key[request_key] = value
        return value

    def add(
        self,
        *,
        operation_id: str,
        request_key: str,
        kind: str,
        plan_digest: str,
        progress: dict[str, object],
    ) -> None:
        self.operations[operation_id] = {
            "id": operation_id,
            "parent_id": None,
            "node_ids": [NODE],
            "kind": kind,
            "state": "succeeded",
            "attempt": 1,
            "progress": progress,
            "created_at": NOW,
            "updated_at": NOW,
            "result": {
                "request_key": request_key,
                "plan_digest": plan_digest,
                "model_version_sha256": MODEL,
                "recipe_revision_id": RECIPE,
                "artifact_set_sha256": ARTIFACT_SET,
                "scope_node_ids": [NODE],
            },
        }

    def provider(self) -> OperationProvider:
        def list_operations(query: Any) -> OperationListPage:
            rows = list(self.operations.values())
            if query.state is not None:
                rows = [row for row in rows if row["state"] == query.state]
            if query.node_id is not None:
                rows = [row for row in rows if query.node_id in row["node_ids"]]
            return OperationListPage(rows[: query.limit], None, len(rows))

        def get_operation(operation_id: str) -> dict[str, object]:
            try:
                return self.operations[operation_id]
            except KeyError:
                raise KeyError(operation_id) from None

        return OperationProvider("acceptance", list_operations, get_operation)


def _cache_operation(ledger: _Ledger, operation_id: str, request_key: str, plan_digest: str) -> SimpleNamespace:
    progress = {
        "schema_version": 2,
        "phase": "completed",
        "completed_artifacts": 1,
        "total_artifacts": 1,
        "downloaded_bytes": 128,
        "expected_bytes": 128,
        "current_artifact_key": None,
    }
    ledger.add(
        operation_id=operation_id,
        request_key=request_key,
        kind="model-cache.download",
        plan_digest=plan_digest,
        progress={
            "phase": "completed",
            "completed_bytes": 128,
            "total_bytes": 128,
            "total_bytes_known": True,
            "members": [{"member_id": NODE, "phase": "completed", "state": "succeeded", "completed_bytes": 128, "total_bytes": 128}],
        },
    )
    return SimpleNamespace(
        id=operation_id,
        request_key=request_key,
        kind="download",
        state="succeeded",
        attempt=1,
        artifact_set_sha256=ARTIFACT_SET,
        plan_digest=plan_digest,
        progress=progress,
        result={"model_version_sha256": MODEL, "recipe_revision_id": RECIPE},
        last_error=None,
        created_at=NOW,
        updated_at=NOW,
        completed_at=NOW,
    )


class _Cache:
    def __init__(self, ledger: _Ledger) -> None:
        self.ledger = ledger
        self.requests: dict[str, SimpleNamespace] = {}

    def download_preview(self, **_kwargs: object) -> dict[str, object]:
        return {
            "schema_version": 2,
            "artifact_set_sha256": ARTIFACT_SET,
            "plan_digest": PLAN,
            "source_policy": "nas-first",
            "artifact_count": 1,
            "expected_bytes": 128,
            "already_cached_bytes": 0,
            "new_bytes": 128,
            "blockers": [],
            "warnings": [],
        }

    def start_download(self, *, request_key: str, plan_digest: str, **_kwargs: object) -> SimpleNamespace:
        existing = self.requests.get(request_key)
        if existing is not None:
            return existing
        operation_id = self.ledger.id_for(request_key, "download")
        value = _cache_operation(self.ledger, operation_id, request_key, plan_digest)
        self.requests[request_key] = value
        return value

    def get_operation(self, operation_id: str) -> SimpleNamespace:
        for value in self.requests.values():
            if value.id == operation_id:
                return value
        raise KeyError(operation_id)


def _run_plan() -> dict[str, object]:
    fit_node = {
        "node_id": NODE,
        "rank": 0,
        "role": "entrypoint",
        "allowed": True,
        "disk_required_bytes": 128,
        "disk_free_bytes": 1024,
        "disk_free_after_bytes": 896,
        "memory_required_bytes": 128,
        "memory_available_bytes": 1024,
        "memory_free_after_bytes": 896,
        "blockers": [],
        "warnings": [],
    }
    fit = {"allowed": True, "nodes": [fit_node], "blockers": [], "warnings": []}
    phase = {
        "index": 0,
        "kind": "start",
        "subphase": None,
        "state": "planned",
        "node_ids": [NODE],
        "operation_digest": None,
        "detail": "Start model",
    }
    return {
        "schema_version": 2,
        "generated_at": NOW_DT,
        "action": "run",
        "model_version_sha256": MODEL,
        "recipe_revision_id": RECIPE,
        "recipe_content_sha256": "d" * 64,
        "alias": "parity",
        "run_id": None,
        "spark_group": {"nodes": [{"node_id": NODE, "rank": 0, "role": "entrypoint", "endpoint_owner": True}]},
        "mapping": {
            "mapping_id": MAPPING,
            "mapping_generation": 1,
            "topology_name": "solo",
            "parameters": {},
            "placement_digest": "e" * 64,
            "action": "create",
            "nodes": [{"node_id": NODE, "rank": 0, "role": "entrypoint", "endpoint_owner": True}],
        },
        "installation_id": None,
        "installation_state": None,
        "recipe_build_id": None,
        "image_digest": None,
        "start_plan_digest": None,
        "model_capabilities": [],
        "recipe_capabilities": [],
        "freshness": [],
        "fit_current": fit,
        "fit_after_stop": None,
        "fit": fit,
        "storage": {
            "required_bytes": 128,
            "reused_bytes": 0,
            "copied_bytes": 128,
            "missing_nas_bytes": 128,
            "missing_spark_bytes": 128,
            "reclaimable_bytes": 0,
            "reclaimed_bytes": 0,
            "nas_coverage": "partial",
            "spark_coverage": "partial",
            "retention": "retain-cached",
            "running_coverage": "unknown",
            "artifact_digests": [MODEL],
            "reclaimable_digests": [],
        },
        "runtime_storage": {
            "build_id": None,
            "image_digest": None,
            "image_bytes": None,
            "required_bytes": None,
            "reused_bytes": 0,
            "copied_bytes": 0,
            "missing_nas_bytes": None,
            "missing_spark_bytes": None,
            "missing_image_distribution_bytes": None,
            "nas_coverage": "unknown",
            "spark_coverage": "unknown",
            "running_coverage": "unknown",
            "reclaimable_bytes": 0,
            "reclaimable_digests": [],
        },
        "build": {
            "state": "unknown",
            "build_id": None,
            "build_input_sha256": None,
            "builder_node_id": None,
            "image_digest": None,
            "image_bytes": None,
            "oci_layout_sha256": None,
            "source": {"state": "unknown", "source_bundle_sha256": None, "detail": None},
            "compatibility": {"expected_architecture": "linux-arm64", "observed_architecture": None, "state": "unknown", "evidence_digest": None, "detail": None},
            "runtime": {
                "build_id": None,
                "image_digest": None,
                "image_bytes": None,
                "required_bytes": None,
                "reused_bytes": 0,
                "copied_bytes": 0,
                "missing_nas_bytes": None,
                "missing_spark_bytes": None,
                "missing_image_distribution_bytes": None,
                "nas_coverage": "unknown",
                "spark_coverage": "unknown",
                "running_coverage": "unknown",
                "reclaimable_bytes": 0,
                "reclaimable_digests": [],
            },
        },
        "preparation": None,
        "conflicts": [],
        "stops": [],
        "reclaimed_bytes": 0,
        "phases": [phase],
        "allowed": True,
        "blockers": [],
        "warnings": [],
        "invocation": {"origin": "operator", "correlation_id": None, "reason": None, "context": {}},
        "plan_digest": PLAN,
        "stop_before_prepare": False,
        "stop_before_transfer": False,
    }


class _RunSwitch:
    def __init__(self, ledger: _Ledger) -> None:
        self.ledger = ledger
        self.requests: dict[str, dict[str, object]] = {}

    def preview(self, _body: object, *, actor: str) -> dict[str, object]:
        assert actor == "admin"
        return _run_plan()

    def apply(self, body: Any, *, actor: str) -> dict[str, object]:
        assert actor == "admin"
        request_key = body.request_key
        existing = self.requests.get(request_key)
        if existing is not None:
            return SimpleNamespace(**existing)
        operation_id = self.ledger.id_for(request_key, "run")
        result = {
            "schema_version": 2,
            "operation_id": operation_id,
            "kind": "recipe.run-switch.v2",
            "action": body.action,
            "state": "succeeded",
            "plan_digest": body.plan_digest or PLAN,
            "request_key": request_key,
            "node_ids": [NODE],
            "current_phase": "start",
            "completed_phases": ["start"],
            "progress": {
                "phase_index": 0,
                "phase_count": 1,
                "phase": "start",
                "state": "succeeded",
                "completed_bytes": 128,
                "total_bytes": 128,
                "total_bytes_known": True,
                "subphase": None,
                "members": [{"node_id": NODE, "phase": "start", "state": "succeeded", "completed_bytes": 128, "total_bytes": 128, "error": None}],
            },
            "status_reason": None,
            "result": {"model_version_sha256": MODEL, "recipe_revision_id": RECIPE, "scope_node_ids": [NODE]},
        }
        self.requests[request_key] = result
        self.ledger.add(
            operation_id=operation_id,
            request_key=request_key,
            kind="recipe.run-switch.v2",
            plan_digest=result["plan_digest"],
            progress={
                "phase": "start",
                "completed_bytes": 128,
                "total_bytes": 128,
                "total_bytes_known": True,
                "members": [{"member_id": NODE, "phase": "start", "state": "succeeded", "completed_bytes": 128, "total_bytes": 128}],
            },
        )
        return SimpleNamespace(**result)

    def get(self, operation_id: str) -> dict[str, object]:
        for result in self.requests.values():
            if result["operation_id"] == operation_id:
                return result
        raise KeyError(operation_id)


def _profile() -> dict[str, object]:
    return {
        "schema_version": 2,
        "id": PROFILE,
        "name": "Parity profile",
        "description": "Cross client",
        "installation_policy": "keep-cached",
        "labels": {"purpose": "parity"},
        "favorite": False,
        "scope": {"node_ids": [NODE]},
        "assignments": [],
        "profile_digest": "f" * 64,
        "created_by": "admin",
        "created_at": NOW_DT,
        "updated_at": NOW_DT,
    }


class _Profiles:
    def __init__(self, ledger: _Ledger) -> None:
        self.ledger = ledger

    def list(self) -> dict[str, object]:
        return {"schema_version": 2, "generated_at": NOW_DT, "profiles": [_profile()]}

    def create(self, _body: object, *, actor: str) -> dict[str, object]:
        assert actor == "admin"
        return SimpleNamespace(**_profile())

    def preview(self, _profile_id: str) -> dict[str, object]:
        return {
            "schema_version": 2,
            "profile_id": PROFILE,
            "profile_name": "Parity profile",
            "profile_digest": "f" * 64,
            "generated_at": NOW_DT,
            "allowed": True,
            "scope": {"node_ids": [NODE], "idle_node_ids": [NODE]},
            "summary": {"already_correct": 1, "placements": 0, "builds": 0, "distributions": 0, "installs": 0, "starts": 0, "stops": 0, "uninstalls": 0, "blockers": 0},
            "assignments": [],
            "preparations": [],
            "steps": [],
            "reasons": [],
            "plan_digest": PLAN,
        }

    def switch(self, _profile_id: str, *, plan_digest: str, request_key: str, actor: str) -> dict[str, object]:
        assert actor == "admin"
        operation_id = self.ledger.id_for(request_key, "profile")
        self.ledger.add(operation_id=operation_id, request_key=request_key, kind="fleet-profile.switch", plan_digest=plan_digest, progress={"phase": "start", "completed_bytes": 128, "total_bytes": 128, "total_bytes_known": True, "members": [{"member_id": NODE, "phase": "start", "state": "succeeded", "completed_bytes": 128, "total_bytes": 128}]})
        return SimpleNamespace(schema_version=2, id=operation_id, profile_id=PROFILE, profile_digest="f" * 64, plan_digest=plan_digest, state="succeeded", current_step=1, total_steps=1, current_operation_id=operation_id, status_reason=None, progress={"completed_steps": 1, "total_steps": 1}, result={"scope_node_ids": [NODE]}, created_at=NOW_DT, updated_at=NOW_DT)


class _Recipes:
    def __init__(self, ledger: _Ledger) -> None:
        self.ledger = ledger

    @staticmethod
    def _view(operation_id: str, request_key: str) -> RecipeOperationView:
        return RecipeOperationView(operation_id, "recipe.run", "installation", "failed", PLAN, (NODE,), {"request_key": request_key, "scope_node_ids": [NODE]})

    def replay_start(self, *_args: object, **_kwargs: object) -> None:
        return None

    def preview_run(self, *_args: object, **_kwargs: object) -> object:
        return object()

    def start(self, _plan: object, *, plan_digest: str, actor: str, request_id: str) -> RecipeOperationView:
        value = self._view(str(uuid.uuid5(REQUEST_NAMESPACE, "recipe:" + request_id)), request_id)
        self.ledger.add(operation_id=value.id, request_key=request_id, kind=value.kind, plan_digest=plan_digest, progress={"phase": "start", "completed_bytes": 128, "total_bytes": 128, "total_bytes_known": True, "members": [{"member_id": NODE, "phase": "start", "state": "failed", "completed_bytes": 128, "total_bytes": 128}]})
        return value

    def get(self, operation_id: str) -> RecipeOperationView:
        row = self.ledger.operations[operation_id]
        return self._view(operation_id, str(row["result"]["request_key"]))

    def retry(self, operation_id: str, *, actor: str, request_id: str) -> RecipeOperationView:
        assert actor == "admin"
        value = self._view(str(uuid.uuid5(REQUEST_NAMESPACE, "retry:" + request_id)), request_id)
        self.ledger.add(operation_id=value.id, request_key=request_id, kind=value.kind, plan_digest=PLAN, progress={"phase": "start", "completed_bytes": 128, "total_bytes": 128, "total_bytes_known": True, "members": [{"member_id": NODE, "phase": "start", "state": "succeeded", "completed_bytes": 128, "total_bytes": 128}]})
        return value


class _AppTransport:
    def __init__(self, client: TestClient, headers: dict[str, str]) -> None:
        self.client = client
        self.headers = headers

    def request(self, method: str, path: str, payload: dict[str, object] | None = None, *, extra_headers: dict[str, str] | None = None, query: dict[str, object] | None = None) -> dict[str, object]:
        headers = {**self.headers, **(extra_headers or {})}
        response = self.client.request(method, path, headers=headers, json=payload, params=query)
        if response.status_code >= 400:
            detail = response.json().get("detail", "request failed")
            raise ControlHTTPError(response.status_code, str(detail))
        return response.json() if response.content else {}


class _UncertainTransport(_AppTransport):
    """Commit the request at the Controller, then lose only its response."""

    def __init__(self, client: TestClient, headers: dict[str, str]) -> None:
        super().__init__(client, headers)
        self._uncertain = True

    def request(self, method: str, path: str, payload: dict[str, object] | None = None, *, extra_headers: dict[str, str] | None = None, query: dict[str, object] | None = None) -> dict[str, object]:
        result = super().request(
            method, path, payload, extra_headers=extra_headers, query=query
        )
        if self._uncertain and method == "POST" and path == "/api/v1/model-cache/download":
            self._uncertain = False
            raise ControlTransportError("response uncertain")
        return result


def _invoke(transport: _AppTransport, *argv: str) -> dict[str, object]:
    output = StringIO()
    with redirect_stdout(output):
        assert cli.main(argv, control_client=transport, request_id_factory=lambda: "00000000-0000-4000-8000-000000000099") == 0
    return json.loads(output.getvalue())


def _invoke_uncertain(transport: _AppTransport, *argv: str) -> tuple[int, dict[str, object]]:
    output = StringIO()
    with redirect_stdout(output):
        code = cli.main(
            argv,
            control_client=transport,
            request_id_factory=lambda: "00000000-0000-4000-8000-000000000099",
        )
    return code, json.loads(output.getvalue())


def test_cli_and_api_share_operation_identity_progress_and_replay() -> None:
    ledger = _Ledger()
    cache = _Cache(ledger)
    run_switch = _RunSwitch(ledger)
    profiles = _Profiles(ledger)
    recipes = _Recipes(ledger)
    codec = TokenCodec(b"t" * 32)
    app = create_app(
        jobs=_Jobs(),
        tokens=codec,
        audits=MemoryAuditStore(),
        fleet=lambda: {"authority_revision": "1" * 64, "nodes": []},
        now=lambda: 10,
        model_cache=cache,
        run_switch_operations=run_switch,
        fleet_profiles=profiles,
        recipe_operations=recipes,
        operations=OperationApiServices(
            endpoint=lambda _alias: {},
            agents=list,
            job_operations=lambda *_args: None,
            resume_job=lambda _job_id: None,
            operation_providers=(ledger.provider(),),
            cursor_codec=codec.cursor_codec(),
        ),
    )
    token = codec.issue(Actor("admin", "administrator"), ttl_seconds=100, now=0)
    headers = {"Authorization": f"Bearer {token}"}
    api = TestClient(app)
    transport = _AppTransport(api, headers)

    download_key = "00000000-0000-4000-8000-000000000101"
    download_preview = api.post("/api/v1/model-cache/download-preview", headers=headers, json={"model_version_sha256": MODEL})
    download = api.post("/api/v1/model-cache/download", headers={**headers, "x-request-id": download_key}, json={"model_version_sha256": MODEL, "plan_digest": PLAN, "request_key": download_key})
    assert download_preview.status_code == 200
    assert download.status_code == 202
    api_download = download.json()
    cli_download = _invoke(transport, "--json", "cache", "operations", "show", api_download["id"])
    assert cli_download["id"] == api_download["id"]
    assert cli_download["progress"]["downloaded_bytes"] == 128
    assert cli_download["artifact_set_sha256"] == api_download["artifact_set_sha256"] == ARTIFACT_SET
    assert cli_download["request_key"] == api_download["request_key"] == download_key

    run_key = "00000000-0000-4000-8000-000000000102"
    run_body = {"model_version_sha256": MODEL, "recipe_revision_id": RECIPE, "spark_group": {"nodes": [{"node_id": NODE, "rank": 0, "role": "entrypoint", "endpoint_owner": True}]}, "alias": "parity", "action": "run", "retention": "retain-cached", "invocation": {"origin": "operator", "correlation_id": None, "reason": None, "context": {}}}
    api_run_preview = api.post("/api/v1/recipes/run-switch-plans/preview", headers=headers, json=run_body)
    api_run = api.post("/api/v1/recipes/run-switches", headers=headers, json={**run_body, "plan_digest": api_run_preview.json()["plan_digest"], "request_key": run_key})
    assert api_run_preview.status_code == 200
    assert api_run.status_code == 202
    api_run_body = api_run.json()
    cli_run = _invoke(transport, "--json", "models", "run", "--input", json.dumps(run_body), "--request-key", run_key)
    assert cli_run["result"]["id"] == api_run_body["operation_id"]
    assert cli_run["plan"]["model_version_sha256"] == api_run_preview.json()["model_version_sha256"] == MODEL
    assert cli_run["result"]["progress"]["members"][0]["member_id"] == NODE

    profile_create = api.post("/api/v1/fleet-profiles", headers=headers, json={"name": "Parity profile", "scope": {"node_ids": [NODE]}, "assignments": []})
    assert profile_create.status_code == 201
    profile_id = profile_create.json()["id"]
    profile_preview = api.post(f"/api/v1/fleet-profiles/{profile_id}/preview", headers=headers, json={})
    profile_key = "00000000-0000-4000-8000-000000000103"
    profile_apply = api.post(f"/api/v1/fleet-profiles/{profile_id}/switch", headers=headers, json={"plan_digest": profile_preview.json()["plan_digest"], "request_key": profile_key})
    assert profile_apply.status_code == 202
    api_profile = profile_apply.json()
    cli_profile = _invoke(transport, "--json", "profiles", "switch", profile_id, "--request-key", profile_key)
    assert cli_profile["result"]["current_operation_id"] == api_profile["current_operation_id"]
    assert cli_profile["result"]["plan_digest"] == api_profile["plan_digest"]
    assert cli_profile["result"]["progress"]["completed_steps"] == 1

    # Seed one failed recipe operation in the same composed ledger.  This is
    # the persisted operation an operator would select from Activity before
    # asking vonkctl to retry it.
    original_id = str(uuid.uuid5(REQUEST_NAMESPACE, "recipe:original"))
    recipes.ledger.add(
        operation_id=original_id,
        request_key="00000000-0000-4000-8000-000000000104",
        kind="recipe.run",
        plan_digest=PLAN,
        progress={"phase": "start", "completed_bytes": 128, "total_bytes": 128, "total_bytes_known": True, "members": [{"member_id": NODE, "phase": "start", "state": "failed", "completed_bytes": 128, "total_bytes": 128}]},
    )
    original = {"id": original_id}
    retry = _invoke(transport, "--json", "library", "operation", "retry", original["id"], "--request-key", "00000000-0000-4000-8000-000000000105", "--apply")
    assert retry["id"] != original["id"]
    activity = api.get("/api/v1/operations", headers=headers, params={"state": "succeeded"})
    assert activity.status_code == 200
    activity_ids = {item["id"] for item in activity.json()["operations"]}
    assert {api_download["id"], api_run_body["operation_id"], api_profile["current_operation_id"], retry["id"]} <= activity_ids

    replay = api.post("/api/v1/model-cache/download", headers={**headers, "x-request-id": download_key}, json={"model_version_sha256": MODEL, "plan_digest": PLAN, "request_key": download_key})
    assert replay.status_code == 202
    assert replay.json()["id"] == api_download["id"]


def test_uncertain_cache_submission_reuses_request_key_against_same_app() -> None:
    ledger = _Ledger()
    cache = _Cache(ledger)
    codec = TokenCodec(b"u" * 32)
    app = create_app(
        jobs=_Jobs(),
        tokens=codec,
        audits=MemoryAuditStore(),
        fleet=lambda: {"nodes": []},
        model_cache=cache,
    )
    headers = {
        "Authorization": f"Bearer {codec.issue(Actor('admin', 'administrator'), ttl_seconds=100, now=int(time.time()))}"
    }
    api = TestClient(app)
    request_key = "00000000-0000-4000-8000-000000000201"
    transport = _UncertainTransport(api, headers)
    code, failure = _invoke_uncertain(
        transport,
        "--json",
        "cache",
        "download",
        "--model-version-sha256",
        MODEL,
        "--request-key",
        request_key,
    )
    assert code == 2
    assert failure["request_key"] == request_key
    assert failure["reconcile"]["request_key"] == request_key
    replay_code, replay = _invoke_uncertain(transport, "--json", "cache", "download", "--model-version-sha256", MODEL, "--request-key", request_key)
    assert replay_code == 0, replay
    assert replay["request_key"] == request_key
    assert replay["result"]["id"] == ledger.id_for(request_key, "download")


def test_production_services_share_cache_run_and_profile_state(tmp_path: Any) -> None:
    """Compose the integrated durable services, then cross the CLI seam."""
    # The recipe-operation fixture supplies the same catalog, mapping, node,
    # inventory, and lifecycle rows used by the Controller service tests.  It
    # is imported lazily because this acceptance is run on the integrated
    # branch, while the CLI branch intentionally has no backend dependency.
    from importlib import resources

    from sqlalchemy import select
    from vonk_control.cluster_mappings import ClusterMappingService
    from vonk_control.fleet_profiles import (
        FleetProfileService,
        RunSwitchFleetProfileAdapter,
    )
    from vonk_control.model_cache import ModelCacheService
    from vonk_control.model_cache_api import register_model_cache_operation_provider
    from vonk_control.models import CatalogDocumentRevision
    from vonk_control.run_switch_operations import (
        ArtifactInspection,
        PhaseExecution,
        RunSwitchOperationService,
    )
    from vonk_forge_contracts import ModelDefinition, content_sha256

    from .test_recipe_operations import NOW as fixture_now
    from .test_recipe_operations import setup_services

    def model_transform(document: dict[str, object]) -> None:
        document["source"]["repository"] = (
            "https://huggingface.co/vonk-forge/synthetic-tiny"
        )

    model_document = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text()
    )
    model_transform(model_document)
    model_digest = content_sha256(ModelDefinition.model_validate(model_document))

    def recipe_transform(document: dict[str, object]) -> None:
        document["models"][0]["model"]["content_sha256"] = model_digest

    sessions, lifecycle, _queue, _mapping, _build, node_ids = setup_services(
        tmp_path, recipe_transform=recipe_transform, model_transform=model_transform
    )
    now = fixture_now

    class ProductionPhases:
        def execute(self, _plan: Any, phase: Any, **_kwargs: Any) -> PhaseExecution:
            return PhaseExecution(result={"phase": phase.kind})

        def get(self, _operation_id: str) -> None:
            return None

    class ProductionCoverage:
        def inspect(self, _session: Any, **_kwargs: Any) -> ArtifactInspection:
            return ArtifactInspection(
                required_bytes=1024,
                reused_bytes=1024,
                copied_bytes=0,
                missing_nas_bytes=0,
                missing_spark_bytes=0,
                reclaimable_bytes=0,
                nas_coverage="complete",
                spark_coverage="complete",
                artifact_digests=("c" * 64,),
                artifact_set_sha256="d" * 64,
                artifact_set_bytes=1024,
            )

    cache = ModelCacheService(
        sessions, tmp_path / "model-cache", reserve_bytes=0, fixture_sources=True
    )
    run_switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: now,
        mappings=ClusterMappingService(sessions),
        artifacts=ProductionCoverage(),
        model_cache=cache,
        phase_executor=ProductionPhases(),
        memory_floor_bytes=50,
    )
    profiles = FleetProfileService(
        sessions,
        clock=lambda: now,
        switch_adapter=RunSwitchFleetProfileAdapter(sessions, run_switch),
    )
    codec = TokenCodec(b"p" * 32)
    activity_services = register_model_cache_operation_provider(
        OperationApiServices(
            endpoint=lambda _alias: {},
            agents=list,
            job_operations=lambda *_args: None,
            resume_job=lambda _job_id: None,
            operation_providers=(
                run_switch.activity_provider(),
                profiles.operation_provider(),
            ),
            cursor_codec=codec.cursor_codec(),
        ),
        cache,
    )
    app = create_app(
        jobs=_Jobs(),
        tokens=codec,
        audits=MemoryAuditStore(),
        fleet=lambda: {"nodes": []},
        now=lambda: 10,
        model_cache=cache,
        recipe_operations=lifecycle,
        run_switch_operations=run_switch,
        fleet_profiles=profiles,
        operations=activity_services,
    )
    token = codec.issue(Actor("admin", "administrator"), ttl_seconds=100, now=0)
    headers = {"Authorization": f"Bearer {token}"}
    api = TestClient(app)
    transport = _AppTransport(api, headers)
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
        assert revision is not None
        model_digest = str(revision.document["models"][0]["model"]["content_sha256"])
        revision_id = str(revision.id)
    node_id = str(node_ids[0])

    download_key = "00000000-0000-4000-8000-000000000301"
    preview = api.post(
        "/api/v1/model-cache/download-preview",
        headers=headers,
        json={"model_version_sha256": model_digest},
    )
    assert preview.status_code == 200
    download = api.post(
        "/api/v1/model-cache/download",
        headers={**headers, "x-request-id": download_key},
        json={
            "model_version_sha256": model_digest,
            "plan_digest": preview.json()["plan_digest"],
            "request_key": download_key,
        },
    )
    assert download.status_code == 202
    cli_download = _invoke(transport, "--json", "cache", "operations", "show", download.json()["id"])
    assert cli_download["id"] == download.json()["id"]
    assert cli_download["artifact_set_sha256"] == preview.json()["artifact_set_sha256"]

    run_key = "00000000-0000-4000-8000-000000000302"
    run_input = {
        "model_version_sha256": model_digest,
        "recipe_revision_id": revision_id,
        "spark_group": {
            "nodes": [
                {
                    "node_id": node_id,
                    "rank": 0,
                    "role": "entrypoint",
                    "endpoint_owner": True,
                }
            ]
        },
        "alias": "production-parity",
        "action": "run",
        "retention": "retain-cached",
        "invocation": {
            "origin": "operator",
            "correlation_id": None,
            "reason": None,
            "context": {},
        },
    }
    run_preview = api.post(
        "/api/v1/recipes/run-switch-plans/preview", headers=headers, json=run_input
    )
    assert run_preview.status_code == 200, run_preview.text
    run_apply = api.post(
        "/api/v1/recipes/run-switches",
        headers=headers,
        json={
            **run_input,
            "plan_digest": run_preview.json()["plan_digest"],
            "request_key": run_key,
        },
    )
    assert run_apply.status_code == 202, run_apply.text
    cli_run = _invoke(
        transport,
        "--json",
        "models",
        "run",
        "--input",
        json.dumps(run_input),
        "--request-key",
        run_key,
        "--detach",
    )
    assert cli_run["result"].get("operation_id") == run_apply.json()["operation_id"], cli_run
    assert cli_run["plan"]["model_version_sha256"] == model_digest
    assert cli_run["result"]["progress"]["members"][0]["node_id"] == node_id, cli_run

    profile = api.post(
        "/api/v1/fleet-profiles",
        headers=headers,
        json={"name": "Production parity", "scope": {"node_ids": [node_id]}, "assignments": []},
    )
    assert profile.status_code == 201
    profile_id = profile.json()["id"]
    profile_plan = api.post(
        f"/api/v1/fleet-profiles/{profile_id}/preview", headers=headers, json={}
    )
    profile_key = "00000000-0000-4000-8000-000000000303"
    profile_apply = api.post(
        f"/api/v1/fleet-profiles/{profile_id}/switch",
        headers=headers,
        json={"plan_digest": profile_plan.json()["plan_digest"], "request_key": profile_key},
    )
    assert profile_apply.status_code == 202
    cli_profile = _invoke(
        transport, "--json", "profiles", "switch", profile_id, "--request-key", profile_key
    )
    assert cli_profile["result"]["id"] == profile_apply.json()["id"]
    assert cli_profile["result"]["plan_digest"] == profile_plan.json()["plan_digest"]

    activity = api.get("/api/v1/operations", headers=headers, params={"limit": 100})
    assert activity.status_code == 200, activity.text
    activity_ids = {item["id"] for item in activity.json()["operations"]}
    assert {download.json()["id"], run_apply.json()["operation_id"], profile_apply.json()["id"]} <= activity_ids
