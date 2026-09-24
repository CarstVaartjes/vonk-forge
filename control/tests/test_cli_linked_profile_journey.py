"""Installed CLI journey from Profile authoring to its published route owner.

The local deterministic executor consumes real Controller-issued AgentOperations
and submits canonical typed receipts through AgentJobService. It exercises the
owner and identity chain only; no Spark, container runtime, model process, or
inference endpoint is started.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from vonk_agent_protocol import (
    AgentInstallResult,
    AgentOperation,
    AgentResult,
    ArtifactDistributionResult,
    InventoryRequest,
    RecipeRunObservationGrantRequest,
    RecipeRunObservationGrantWire,
    RecipeRunObservationsWire,
    RecipeRunObservationWire,
    RecipeStartResult,
    RecipeStartSingleEvidence,
    canonical_message,
    format_model_identity,
)
from vonk_agent_protocol.runtime_preflight import (
    RuntimePreflightFinding,
    RuntimePreflightRequest,
    RuntimePreflightResult,
)
from vonk_control.models import (
    AgentNode,
    AgentOperationAttempt,
    CatalogDocumentRevision,
    ClusterMapping,
    Job,
    NodeInventorySnapshot,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from vonk_control.models import (
    AgentOperation as StoredAgentOperation,
)
from vonk_control.runtime_image_preparation import PulledImageEvidence
from vonk_control.runtime_preflight import mandatory_capabilities, request_digest

from . import test_cli_operator_walkthrough as walkthrough
from .runtime_identity_support import PACKAGED_RUNTIME_IDENTITY, claim_agent
from .test_recipe_operations import RECEIPT_SIGNER, signed_observation_receipt

pytest_plugins = ("tests.test_profile_load_installed_cli",)

pytestmark = [
    pytest.mark.lane,
    pytest.mark.skipif(
        "VONK_LINKED_PROFILE_JOURNEY_MODE" not in os.environ,
        reason="set VONK_LINKED_PROFILE_JOURNEY_MODE=smoke or interactive to opt in",
    ),
]

_ASSIGNMENT = "linked-answer"
_REQUEST_KEY = "11111111-1111-4111-8111-111111111181"
_INTERACTIVE_INVENTORY_REFRESH = 60
_AGENT_CAPABILITIES = (
    "agent.runtime.rust.v1",
    "runtime.vonk.v1",
    "runtime.preflight.v1",
    "artifact.distribution.v1",
    "recipe.install",
    "recipe.start",
    "recipe.run.inspect.exact.v1",
    "recipe.run.inspect.receipt.v1",
    f"runtime.preflight.fingerprint.{walkthrough._LINKED_PREFLIGHT_FINGERPRINT}",
)


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_message(value)).hexdigest()


def _sealed_model(model: type[Any], body: dict[str, object]):
    return model.model_validate({**body, "evidence_digest": _sha256(body)})


def _result(claim: Any, result: Any) -> AgentResult:
    return AgentResult(
        schema_version=1,
        job_id=claim.job_id,
        operation_id=claim.operation_id,
        attempt=claim.attempt,
        fence=claim.fence,
        node_id=claim.node_id,
        deadline=claim.deadline,
        state="succeeded",
        result=result,
    )


def _execute_distribution(claim: Any, owners: Any) -> ArtifactDistributionResult:
    payload = claim.payload
    assignment = owners.distribution.authorize(
        node_id=claim.node_id, plan_digest=payload.plan_digest
    )
    if assignment is None:
        raise AssertionError("issued distribution operation has no assignment")
    copied = owners.target_root / assignment.assignment_id
    model_digests: list[str] = []
    downloaded_bytes = 0
    inspected_image: PulledImageEvidence | None = None

    for item in assignment.objects:
        exact_assignment, exact_item, opened = owners.distribution.open_object(
            node_id=claim.node_id,
            plan_digest=payload.plan_digest,
            digest=item.sha256,
        )
        assert exact_assignment.assignment_id == assignment.assignment_id
        assert exact_item == item
        try:
            content = opened.stream.read()
        finally:
            opened.stream.close()
        assert len(content) == item.bytes
        assert hashlib.sha256(content).hexdigest() == item.sha256
        destination = copied.joinpath(*item.name.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        observed = destination.read_bytes()
        assert len(observed) == item.bytes
        assert hashlib.sha256(observed).hexdigest() == item.sha256
        downloaded_bytes += len(observed)
        if item.kind == "model":
            model_digests.append(item.sha256)
        elif item.kind == "oci-archive":
            inspected_image = owners.image_inspector.inspect_archive(
                destination,
                expected_architecture=owners.image_evidence.architecture,
                expected_runtime_interface=owners.image_evidence.runtime_interface,
                expected_archive_sha256=item.sha256,
                expected_archive_bytes=item.bytes,
            )

    if inspected_image is None:
        raise AssertionError("issued assignment did not include an OCI archive")
    if inspected_image.manifest_digest != assignment.oci_image_digest:
        raise AssertionError("target copy OCI identity differs from its assignment")
    if inspected_image.archive_sha256 != assignment.oci_archive_sha256:
        raise AssertionError("target copy archive differs from its assignment")

    body: dict[str, object] = {
        "assignment_id": assignment.assignment_id,
        "model_artifact_set_sha256": assignment.model_artifact_set_sha256,
        "verified": True,
        "verified_digests": sorted(model_digests),
        "verified_image_digest": inspected_image.manifest_digest,
        # This receipt is produced at the simulated agent boundary. Skopeo
        # verifies the exact copied archive; no Docker daemon is available to
        # claim a real image import or runtime launch.
        "imported_image_digest": inspected_image.manifest_digest,
        "verified_oci_layout_sha256": inspected_image.archive_sha256,
        "oci_image_digest": inspected_image.manifest_digest,
        "downloaded_bytes": downloaded_bytes,
    }
    return _sealed_model(ArtifactDistributionResult, body)


def _execute_install(claim: Any) -> AgentInstallResult:
    # The typed install acknowledgement is emitted only for a current issued
    # recipe.install operation. It stands in for the Spark-side package/runtime
    # boundary, not an installed host package.
    payload = claim.payload
    return AgentInstallResult(installed_bytes=payload.expected_bytes)


def _execute_start(claim: Any, owners: Any) -> RecipeStartResult:
    payload = claim.payload
    compiled = payload.compiled_execution_plan
    with owners.sessions() as session:
        revision = session.get(CatalogDocumentRevision, payload.recipe_revision_id)
        if revision is None or revision.kind != "recipe":
            raise AssertionError("issued recipe.start lost its catalog revision")
        selections = revision.document.get("models")
        if not isinstance(selections, list) or not selections:
            raise AssertionError("issued recipe.start has no canonical model pin")
        model = selections[0].get("model")
        if not isinstance(model, dict):
            raise TypeError("issued recipe.start model pin is malformed")
        model_identity = format_model_identity(
            model["publisher"], model["slug"], model["content_sha256"]
        )

    argv_digest = hashlib.sha256(canonical_message(compiled.runtime.argv)).hexdigest()
    body: dict[str, object] = {
        "recipe_revision_id": payload.recipe_revision_id,
        "recipe_content_sha256": payload.recipe_content_sha256,
        "image_digest": payload.image_digest,
        "artifact_set_digest": compiled.identity.model_artifact_set_sha256,
        "model_identity": model_identity,
        "rank": payload.rank,
        "world_size": payload.world_size,
        "endpoint": f"http://{payload.endpoint_address}:{payload.port}",
        "memory_reservation_bytes": payload.reserved_memory_bytes,
        "ready": True,
        "run_generation": payload.run_generation,
        "runtime_arguments_sha256": argv_digest,
        "local_address": None,
        "master_address": None,
        "master_port": None,
    }
    evidence = _sealed_model(RecipeStartSingleEvidence, body)
    return RecipeStartResult(
        endpoint=evidence.endpoint,
        evidence=evidence,
        evidence_digest=evidence.evidence_digest,
    )


def _execute_preflight(claim: Any, owners: Any) -> RuntimePreflightResult:
    payload = claim.payload
    request = (
        payload
        if isinstance(payload, RuntimePreflightRequest)
        else RuntimePreflightRequest.model_validate(payload)
    )
    return RuntimePreflightResult(
        schema_version=1,
        fingerprint=walkthrough._LINKED_PREFLIGHT_FINGERPRINT,
        request_sha256=request_digest(request),
        observed_at=int(owners.clock().timestamp()),
        duration_ms=1,
        cached=False,
        findings=[
            RuntimePreflightFinding(
                capability=capability,
                status="passed",
                code="fixture_observed",
            )
            for capability in mandatory_capabilities(request)
        ],
    )


def _agent_headers(node_id: str) -> dict[str, str]:
    return {
        "x-vonk-agent-node": node_id,
        "x-vonk-agent-serial": "serial-0",
        "x-vonk-agent-fingerprint": "fingerprint-0",
        "x-vonk-agent-verified": "1",
        "x-vonk-agent-proxy-auth": "p" * 32,
        "x-vonk-agent-source": "10.0.0.42",
    }


def _refresh_inventory(api: TestClient, owners: Any, node_id: str) -> None:
    """Refresh the same simulated node report through the registered owner API."""

    with owners.sessions() as session:
        snapshot = session.scalar(
            select(NodeInventorySnapshot)
            .where(NodeInventorySnapshot.node_id == node_id)
            .order_by(NodeInventorySnapshot.observed_at.desc())
        )
        if snapshot is None:
            raise AssertionError("linked journey inventory owner has no snapshot")
        body: dict[str, object] = {
            "schema_version": 1,
            "observed_at": owners.clock().isoformat(),
            "disk_total_bytes": snapshot.disk_total_bytes,
            "disk_free_bytes": snapshot.disk_free_bytes,
            "host_memory_total_bytes": snapshot.host_memory_total_bytes,
            "host_memory_free_bytes": snapshot.host_memory_free_bytes,
            "gpu_memory_total_bytes": snapshot.gpu_memory_total_bytes,
            "gpu_memory_free_bytes": snapshot.gpu_memory_free_bytes,
            "gpu_count": snapshot.gpu_count,
            "memory_pool": snapshot.memory_pool,
            "artifact_store_read_only": snapshot.artifact_store_read_only,
            "capabilities": snapshot.capabilities,
            "nvidia_driver_version": snapshot.nvidia_driver_version,
            "container_runtime_version": snapshot.container_runtime_version,
        }
        if snapshot.fabric_address is not None:
            body["fabric_address"] = snapshot.fabric_address
        if snapshot.fabric_bandwidth_mbps is not None:
            body["fabric_bandwidth_mbps"] = snapshot.fabric_bandwidth_mbps
    request = InventoryRequest.model_validate_json(canonical_message(body), strict=True)
    response = api.post(
        "/agent/inventory",
        headers=_agent_headers(node_id),
        json=request.model_dump(mode="json"),
    )
    assert response.status_code == 204, response.text
    owners.events.append(f"refreshed inventory node={node_id}")


def _observe_exact_running_generation(
    api: TestClient,
    owners: Any,
    node_id: str,
) -> bool:
    """Submit one genuine grant-bound observation for the exact current run."""

    with owners.sessions() as session:
        run = session.scalar(
            select(RecipeRun)
            .where(RecipeRun.state == "running", RecipeRun.route_state == "pending")
            .order_by(RecipeRun.created_at, RecipeRun.id)
        )
        if run is None:
            return False
        run_node = session.scalar(
            select(RunNode).where(
                RunNode.run_id == run.id,
                RunNode.node_id == node_id,
            )
        )
        if (
            run_node is None
            or run_node.state != "running"
            or run_node.observed_run_generation == run.run_generation
        ):
            return False
        installation = session.get(RecipeInstallation, run.installation_id)
        revision = (
            session.get(CatalogDocumentRevision, installation.recipe_revision_id)
            if installation is not None
            else None
        )
        mapping = session.get(ClusterMapping, run.mapping_id)
        agent_node = session.get(AgentNode, node_id)
        start_jobs = tuple(
            session.scalars(
                select(Job)
                .where(Job.kind == "recipe.start", Job.state == "succeeded")
                .order_by(Job.updated_at.desc(), Job.id.desc())
            )
        )
        launch: dict[str, object] | None = None
        for job in start_jobs:
            if job.payload.get("owner_id") != run.id or not isinstance(
                job.result, dict
            ):
                continue
            raw = job.result.get("launch_evidence")
            candidate = raw.get(node_id) if isinstance(raw, dict) else None
            if (
                isinstance(candidate, dict)
                and candidate.get("run_generation") == run.run_generation
            ):
                launch = dict(candidate)
                break
        if (
            installation is None
            or revision is None
            or mapping is None
            or agent_node is None
            or launch is None
            or revision.content_digest is None
            or installation.image_digest is None
            or agent_node.observation_receipt_public_key is None
        ):
            raise AssertionError("exact running observation owner data is incomplete")
        identity: dict[str, object] = {
            "schema_version": 1,
            "node_id": node_id,
            "run_id": run.id,
            "installation_id": installation.id,
            "recipe_revision_id": revision.id,
            "recipe_content_sha256": revision.content_digest,
            "mapping_id": mapping.id,
            "mapping_generation": run.mapping_generation,
            "run_generation": run.run_generation,
            "image_digest": installation.image_digest.removeprefix("sha256:"),
            "artifact_set_digest": launch.get("artifact_set_digest"),
            "model_identity": launch.get("model_identity"),
            "rank": run_node.rank,
            "role": run_node.role,
            "world_size": launch.get("world_size"),
            "local_address": launch.get("local_address"),
            "master_address": launch.get("master_address"),
            "master_port": launch.get("master_port"),
            "port": run_node.port,
            "runtime_arguments_sha256": launch.get("runtime_arguments_sha256"),
        }
        receipt_public_key = agent_node.observation_receipt_public_key
        endpoint_owner = mapping.endpoint_owner_node_id == node_id

    observed_at = owners.clock()
    grant_request = RecipeRunObservationGrantRequest.model_validate(
        {
            **identity,
            "job_id": run.id,
            "operation_id": str(uuid.uuid4()),
            "attempt": run.run_generation,
            "fence": str(uuid.uuid4()),
            "request_sha256": "d" * 64,
            "expires_in_seconds": 10,
        },
        strict=True,
    )
    agent_headers = _agent_headers(node_id)
    grant_response = api.post(
        "/agent/recipe-runs/observation-grants",
        headers=agent_headers,
        json=grant_request.model_dump(mode="json"),
    )
    if grant_response.status_code == 425:
        return False
    assert grant_response.status_code == 200, grant_response.text
    grant_document = RecipeRunObservationGrantWire.model_validate_json(
        grant_response.content, strict=True
    )
    signed_grant = grant_document.grant
    helper_receipt = signed_observation_receipt(
        signed_grant,
        grant_document.observation_identity_sha256,
        node_id=node_id,
        observed_at=observed_at,
    )
    observation = RecipeRunObservationWire.model_validate(
        {
            **identity,
            "observed_at": observed_at.isoformat(),
            "endpoint_ready": True if endpoint_owner else None,
            "observation_identity_sha256": grant_document.observation_identity_sha256,
            "grant": signed_grant.model_dump(mode="json"),
            "helper_receipt": helper_receipt.model_dump(mode="json"),
            "observation_receipt_public_key": receipt_public_key,
        },
        strict=True,
    )
    envelope = RecipeRunObservationsWire.model_validate(
        {
            "schema_version": 2,
            "observed_at": observed_at.isoformat(),
            "runs": [observation.model_dump(mode="json")],
        },
        strict=True,
    )
    observation_response = api.post(
        "/agent/recipe-runs/observations",
        headers=agent_headers,
        json=envelope.model_dump(mode="json"),
    )
    assert observation_response.status_code == 204, observation_response.text
    owners.events.append(f"observed run={run.id} generation={run.run_generation}")
    return True


def _advance_once(owners: Any, node_id: str, api: TestClient) -> bool:
    """Tick the real owner graph and answer one exact claimed operation."""

    observed = _observe_exact_running_generation(api, owners, node_id)
    progressed = owners.worker.tick() or observed
    owners.events.append(f"worker.tick progressed={progressed}")
    claim = claim_agent(
        owners.agent_jobs,
        node_id,
        "serial-0",
        120,
        protocol_version=3,
        capabilities=_AGENT_CAPABILITIES,
        runtime_identity={
            **PACKAGED_RUNTIME_IDENTITY,
            "architecture": "linux-arm64",
            "observation_receipt_public_key": RECEIPT_SIGNER.public_key()
            .public_bytes_raw()
            .hex(),
        },
        hostname="spark-one",
    )
    if claim is None:
        owners.events.append("claim_agent returned no operation")
        return progressed
    owners.events.append(f"claimed {claim.operation.value}")
    if claim.operation is AgentOperation.RUNTIME_PREFLIGHT:
        result = _execute_preflight(claim, owners)
    elif claim.operation is AgentOperation.ARTIFACT_DISTRIBUTION:
        result = _execute_distribution(claim, owners)
    elif claim.operation is AgentOperation.RECIPE_INSTALL:
        result = _execute_install(claim)
    elif claim.operation is AgentOperation.RECIPE_START:
        result = _execute_start(claim, owners)
    else:
        raise AssertionError(
            f"linked journey issued unexpected operation {claim.operation.value}"
        )
    owners.agent_jobs.record_result(_result(claim, result))
    owners.events.append(f"recorded {claim.operation.value} success")
    return True


def _owner_loop(
    owners: Any,
    node_id: str,
    api: TestClient,
    stop: threading.Event,
    errors: list[Exception],
) -> None:
    last_inventory_refresh = owners.clock()
    while not stop.is_set():
        try:
            now = owners.clock()
            if owners.interactive_clock and now - last_inventory_refresh >= timedelta(
                seconds=_INTERACTIVE_INVENTORY_REFRESH
            ):
                _refresh_inventory(api, owners, node_id)
                last_inventory_refresh = owners.clock()
            _advance_once(owners, node_id, api)
            owners.advance_clock()
        except Exception as error:  # noqa: BLE001 - surface worker failures to the test thread.
            errors.append(error)
            return
        # Rate-limit the owner poll even when a tick makes progress.
        stop.wait(0.05)


def test_interactive_clock_tracks_elapsed_time_across_inventory_age_boundary() -> None:
    monotonic_time = [10.0]
    clock, advance_clock = walkthrough._walkthrough_clock(
        interactive=True, monotonic=lambda: monotonic_time[0]
    )
    initial = clock()
    for _ in range(320):
        monotonic_time[0] += 0.05
        advance_clock()
    elapsed = (clock() - initial).total_seconds()
    assert elapsed == pytest.approx(16.0)
    assert elapsed < _INTERACTIVE_INVENTORY_REFRESH


def _cli(
    executable: Path,
    environment: dict[str, str],
    cwd: Path,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    return walkthrough._run_cli(executable, tuple(arguments), environment, cwd)


def _assert_cli_success(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert result.returncode == 0, result.stdout + result.stderr
    value = json.loads(result.stdout)
    assert isinstance(value, dict)
    return value


def _author_and_import(
    *, executable: Path, environment: dict[str, str], cwd: Path, node_id: str
) -> tuple[str, dict[str, object]]:
    selector = f"vonk-forge/{walkthrough._READY_RECIPE_SELECTOR}"
    added = _assert_cli_success(
        _cli(
            executable,
            environment,
            cwd,
            "--profile",
            "1",
            "profile",
            "add",
            selector,
            "--spark",
            "Spark One",
            "--as",
            _ASSIGNMENT,
            "--state",
            "installed",
            "--json",
        )
    )
    assert added
    configured = _assert_cli_success(
        _cli(
            executable,
            environment,
            cwd,
            "--profile",
            "1",
            "profile",
            "configure",
            "--description",
            "Linked owner journey rehearsal",
            "--favorite",
            "true",
            "--label",
            "purpose=linked-journey",
            "--json",
        )
    )
    revision = configured.get("revision")
    assert type(revision) is int
    profile_definition = configured.get("definition")
    assert isinstance(profile_definition, dict)
    assert profile_definition["assignments"][0]["desired_state"] == "installed"
    assert profile_definition["assignments"][0]["spark_ids"] == [node_id]

    export_path = cwd / "linked-profile.json"
    _assert_cli_success(
        _cli(
            executable,
            environment,
            cwd,
            "--profile",
            "1",
            "profile",
            "export",
            "--output",
            str(export_path),
            "--json",
        )
    )
    assert stat.S_IMODE(export_path.stat().st_mode) == 0o600
    exported_definition = json.loads(export_path.read_text(encoding="utf-8"))
    assert exported_definition == profile_definition
    imported = _assert_cli_success(
        _cli(
            executable,
            environment,
            cwd,
            "--profile",
            "1",
            "profile",
            "import",
            "--file",
            str(export_path),
            "--expected-revision",
            str(revision),
            "--json",
        )
    )
    assert imported["definition"] == exported_definition
    return selector, exported_definition


def _accept_running_review(
    *, executable: Path, environment: dict[str, str], cwd: Path, node_id: str
) -> tuple[str, str]:
    selector = f"vonk-forge/{walkthrough._READY_RECIPE_SELECTOR}"
    _assert_cli_success(
        _cli(
            executable,
            environment,
            cwd,
            "--profile",
            "1",
            "profile",
            "add",
            selector,
            "--spark",
            "Spark One",
            "--as",
            _ASSIGNMENT,
            "--state",
            "running",
            "--json",
        )
    )
    preview = _assert_cli_success(
        _cli(
            executable,
            environment,
            cwd,
            "--no-input",
            "--profile",
            "1",
            "profile",
            "load",
            "--dry-run",
            "--json",
        )
    )
    assert preview.get("allowed") is True, preview
    digest = preview.get("plan_digest")
    assert isinstance(digest, str) and len(digest) == 64
    accepted = _assert_cli_success(
        _cli(
            executable,
            environment,
            cwd,
            "--no-input",
            "--profile",
            "1",
            "profile",
            "load",
            "--expected-plan",
            digest,
            "--yes",
            "--request-key",
            _REQUEST_KEY,
            "--detach",
            "--json",
        )
    )
    application_id = accepted.get("id")
    assert isinstance(application_id, str) and application_id
    profile_id = accepted.get("profile_id")
    assert isinstance(profile_id, str) and profile_id
    return application_id, profile_id


def _wait_for_published_route(
    api: TestClient,
    headers: dict[str, str],
    application_id: str,
    *,
    owners: Any,
    owner_errors: list[Exception],
    owner_events: list[str],
    timeout_seconds: float = 45,
) -> tuple[dict[str, object], dict[str, object]]:
    deadline = time.monotonic() + timeout_seconds
    progress: dict[str, object] = {}
    endpoint_view: dict[str, object] = {}
    while time.monotonic() < deadline:
        if owner_errors:
            raise AssertionError(
                f"deterministic owner boundary failed: {owner_errors!r}"
            ) from owner_errors[0]
        current = api.get(
            f"/api/profile/applications/{application_id}", headers=headers
        )
        assert current.status_code == 200, current.text
        progress = current.json()
        assert progress.get("id") == application_id
        if progress.get("state") in {"failed", "cancelled"}:
            with owners.sessions() as session:
                debug_jobs = [
                    {
                        "id": row.id,
                        "kind": row.kind,
                        "state": row.state,
                        "status_reason": row.status_reason,
                        "checkpoint": (
                            {
                                key: row.result.get(key)
                                for key in (
                                    "phase",
                                    "subphase",
                                    "phase_index",
                                    "item_index",
                                    "child_operation_id",
                                    "preflight",
                                )
                                if key in row.result
                            }
                            if isinstance(row.result, dict)
                            else None
                        ),
                    }
                    for row in session.scalars(
                        select(Job).order_by(Job.created_at, Job.id)
                    )
                ]
                debug_operations = [
                    {
                        "id": row.id,
                        "job_id": row.parent_job_id,
                        "kind": row.kind,
                        "state": row.state,
                        "reason": row.status_reason,
                        "result": (
                            session.scalar(
                                select(AgentOperationAttempt.result).where(
                                    AgentOperationAttempt.operation_id == row.id,
                                    AgentOperationAttempt.attempt
                                    == row.current_attempt,
                                )
                            )
                            if row.current_attempt > 0
                            else None
                        ),
                    }
                    for row in session.scalars(select(StoredAgentOperation))
                ]
            raise AssertionError(
                f"linked profile application did not complete: {progress}; "
                f"owner events={owner_events[-16:]!r}; jobs={debug_jobs!r}; "
                f"agent_operations={debug_operations!r}"
            )
        endpoints = api.get(
            "/api/profile/1/endpoints",
            params={"alias": _ASSIGNMENT},
            headers=headers,
        )
        assert endpoints.status_code == 200, endpoints.text
        endpoint_view = endpoints.json()
        assignments = endpoint_view.get("assignments")
        if (
            progress.get("state") == "succeeded"
            and endpoint_view.get("application_id") == application_id
            and isinstance(assignments, list)
            and len(assignments) == 1
            and isinstance(assignments[0], dict)
            and assignments[0].get("state") == "published"
        ):
            return progress, endpoint_view
        time.sleep(0.05)
    with owners.sessions() as session:
        jobs = [
            {
                "id": row.id,
                "kind": row.kind,
                "state": row.state,
                "status_reason": row.status_reason,
                "payload_phase": row.payload.get("phase"),
                "result_state": (
                    row.result.get("state") if isinstance(row.result, dict) else None
                ),
                "result_keys": (
                    sorted(row.result) if isinstance(row.result, dict) else []
                ),
                "checkpoint": (
                    {
                        key: row.result.get(key)
                        for key in (
                            "phase",
                            "subphase",
                            "phase_index",
                            "item_index",
                            "child_operation_id",
                            "observation_due_at",
                            "preflight",
                        )
                        if key in row.result
                    }
                    if isinstance(row.result, dict)
                    else None
                ),
            }
            for row in session.scalars(select(Job).order_by(Job.created_at, Job.id))
        ]
        agent_operations = [
            {
                "id": row.id,
                "job_id": row.parent_job_id,
                "kind": row.kind,
                "state": row.state,
                "status_reason": row.status_reason,
                "payload_phase": row.payload.get("phase"),
                "attempt": row.current_attempt,
                "result": (
                    session.scalar(
                        select(AgentOperationAttempt.result).where(
                            AgentOperationAttempt.operation_id == row.id,
                            AgentOperationAttempt.attempt == row.current_attempt,
                        )
                    )
                    if row.current_attempt > 0
                    else None
                ),
            }
            for row in session.scalars(
                select(StoredAgentOperation).order_by(
                    StoredAgentOperation.created_at, StoredAgentOperation.id
                )
            )
        ]
        runs = [
            {
                "id": row.id,
                "state": row.state,
                "route_state": row.route_state,
                "route_error": row.route_error,
            }
            for row in session.scalars(select(RecipeRun))
        ]
    raise TimeoutError(
        f"application {application_id} did not reach its Profile-owned published route; "
        f"last owner events={owner_events[-24:]!r}; jobs={jobs!r}; "
        f"agent_operations={agent_operations!r}; runs={runs!r}"
    )


def _finish_smoke(
    *,
    executable: Path,
    environment: dict[str, str],
    cwd: Path,
    api: TestClient,
    headers: dict[str, str],
    owners: Any,
    node_id: str,
    application_id: str,
    profile_id: str,
) -> None:
    stop = threading.Event()
    errors: list[Exception] = []
    thread = threading.Thread(
        target=_owner_loop, args=(owners, node_id, api, stop, errors), daemon=True
    )
    thread.start()
    try:
        progress, _ = _wait_for_published_route(
            api,
            headers,
            application_id,
            owners=owners,
            owner_errors=errors,
            owner_events=owners.events,
        )
    finally:
        stop.set()
        thread.join(timeout=2)
    assert not thread.is_alive(), "bounded owner thread did not stop"
    assert not errors, f"deterministic owner boundary failed: {errors!r}"
    assert progress["profile_id"] == profile_id
    assert progress["state"] == "succeeded"

    # This is a new installed-CLI process following the exact durable ID, not
    # the mutable latest-application shortcut.
    followed = _assert_cli_success(
        _cli(
            executable,
            environment,
            cwd,
            "--profile",
            "1",
            "profile",
            "progress",
            "--application",
            application_id,
            "--json",
        )
    )
    assert followed["id"] == application_id
    assert followed["profile_id"] == profile_id
    assert followed["state"] == "succeeded"

    endpoints = _assert_cli_success(
        _cli(
            executable,
            environment,
            cwd,
            "--profile",
            "1",
            "profile",
            "endpoint",
            _ASSIGNMENT,
            "--json",
        )
    )
    assert endpoints["profile_id"] == profile_id
    assert endpoints["application_id"] == application_id
    assignments = endpoints.get("assignments")
    assert isinstance(assignments, list) and len(assignments) == 1
    assignment = assignments[0]
    assert isinstance(assignment, dict)
    assert assignment["alias"] == _ASSIGNMENT
    assert assignment["desired_state"] == "running"
    assert assignment["state"] == "published"
    endpoint = assignment.get("endpoint")
    assert isinstance(endpoint, dict)
    assert endpoint["alias"] == _ASSIGNMENT
    assert endpoint["state"] == "published"
    assert endpoint["generation"] >= 1
    assert str(endpoint["api_base"]).startswith("http://192.168.1.211:")
    print(
        "Linked journey owner boundary passed: the imported Profile's explicit "
        "running revision was reviewed and accepted, its exact application ID "
        "completed through typed AgentJobService receipts, and its Profile-owned "
        "route was discovered by a fresh installed CLI process. No model server "
        "or Spark was started.",
        flush=True,
    )


def _run_linked_journey(postgres_engine, mode: str, installed_vonkctl: Path) -> None:
    temporary_path: Path
    with tempfile.TemporaryDirectory(prefix="vonk-cli-linked-journey-") as temporary:
        workspace = Path(temporary)
        temporary_path = workspace
        workspace.chmod(0o700)
        (
            sessions,
            app,
            headers,
            node_id,
            _blocked_revision_id,
            _blocked_digest,
            _ready_revision_id,
            _ready_digest,
        ) = walkthrough._walkthrough_app(
            postgres_engine,
            workspace / "owner-services",
            linked_profile=True,
            interactive_clock=mode == "interactive",
        )
        owners = app.state.linked_profile_owners
        executable = installed_vonkctl
        operator_cwd = workspace / "operator-cwd"
        operator_cwd.mkdir(mode=0o700)
        with (
            TestClient(app) as api,
            walkthrough._https_api_peer(workspace, api, headers) as (
                url,
                certificate,
                peer,
            ),
        ):
            environment = walkthrough._session_environment(
                installed_vonkctl=executable,
                workspace=workspace,
                url=url,
                certificate=certificate,
                headers=headers,
            )
            token_file = Path(environment["VONK_CONTROL_TOKEN_FILE"])
            assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
            if mode == "smoke":
                owners.advance_clock()
                _refresh_inventory(api, owners, node_id)
            before_effects = walkthrough._effect_counts(sessions)
            _, installed_definition = _author_and_import(
                executable=executable,
                environment=environment,
                cwd=operator_cwd,
                node_id=node_id,
            )
            assert installed_definition["description"] == (
                "Linked owner journey rehearsal"
            )
            assert installed_definition["favorite"] is True
            assert installed_definition["labels"] == {"purpose": "linked-journey"}
            imported_assignments = installed_definition.get("assignments")
            assert isinstance(imported_assignments, list) and imported_assignments
            imported_assignment = imported_assignments[0]
            assert isinstance(imported_assignment, dict)
            assert imported_assignment.get("desired_state") == "installed"
            assert walkthrough._effect_counts(sessions) == before_effects
            current_profile = api.get("/api/profile/1", headers=headers)
            assert current_profile.status_code == 200, current_profile.text
            profile_id = current_profile.json().get("id")
            assert isinstance(profile_id, str) and profile_id

            definition_before_transition = api.get(
                "/api/profile/1/definition", headers=headers
            )
            assert definition_before_transition.status_code == 200
            installed_assignment = definition_before_transition.json()["definition"][
                "assignments"
            ][0]
            assert installed_assignment["desired_state"] == "installed"
            assert installed_assignment["assignment_name"] == _ASSIGNMENT

            if mode == "smoke":
                # The owner-boundary smoke explicitly changes the imported
                # installed-only definition, reviews its fresh plan, and
                # accepts that exact plan. Interactive mode leaves this action
                # for the operator.
                identity_before = installed_assignment.get("assignment_id")
                application_id, accepted_profile_id = _accept_running_review(
                    executable=executable,
                    environment=environment,
                    cwd=operator_cwd,
                    node_id=node_id,
                )
                assert accepted_profile_id == profile_id
                running_definition = api.get(
                    "/api/profile/1/definition", headers=headers
                )
                assert running_definition.status_code == 200
                running_assignment = running_definition.json()["definition"][
                    "assignments"
                ][0]
                assert running_assignment["desired_state"] == "running"
                assert running_assignment["assignment_name"] == _ASSIGNMENT
                assert (
                    running_definition.json()["definition"]["description"]
                    == installed_definition["description"]
                )
                assert running_definition.json()["definition"]["favorite"] is True
                assert running_definition.json()["definition"]["labels"] == {
                    "purpose": "linked-journey"
                }
                if identity_before is not None:
                    assert running_assignment.get("assignment_id") == identity_before
                assert any(
                    method == "POST" and path == "/api/profile/1/preview"
                    for method, path, _document in peer.calls
                )
                accepted_calls = [
                    document
                    for method, path, document in peer.calls
                    if method == "POST" and path == "/api/profile/1/load"
                ]
                assert len(accepted_calls) == 1
                accepted_body = accepted_calls[0]
                assert isinstance(accepted_body, dict)
                assert accepted_body.get("request_key") == _REQUEST_KEY
                _finish_smoke(
                    executable=executable,
                    environment=environment,
                    cwd=operator_cwd,
                    api=api,
                    headers=headers,
                    owners=owners,
                    node_id=node_id,
                    application_id=application_id,
                    profile_id=profile_id,
                )
            else:
                stop = threading.Event()
                errors: list[Exception] = []
                thread = threading.Thread(
                    target=_owner_loop,
                    args=(owners, node_id, api, stop, errors),
                    daemon=True,
                )
                thread.start()
                print(f"Disposable Controller: {url}", flush=True)
                print(f"Installed CLI: {executable}", flush=True)
                print(
                    "Start with Profile 1 and follow the shipped runbook. The "
                    "profile begins installed-only; no application is accepted "
                    "until you explicitly edit it, review, and consent. Typed "
                    "local agent receipts drive only the disposable owner graph. "
                    "No model process or Spark is running, so do not send model "
                    "traffic to the projected route.",
                    flush=True,
                )
                shell = shutil.which("bash") or "/bin/bash"
                try:
                    completed = subprocess.run(
                        [shell, "--noprofile", "--norc", "-i"],
                        env=environment,
                        cwd=operator_cwd,
                        check=False,
                    )
                finally:
                    stop.set()
                    thread.join(timeout=2)
                assert not thread.is_alive(), "bounded owner thread did not stop"
                assert not errors, f"deterministic owner boundary failed: {errors!r}"
                print(
                    f"Operator shell exited with status {completed.returncode}.",
                    flush=True,
                )
    assert not temporary_path.exists()


@pytest.mark.skipif(
    os.environ.get("VONK_LINKED_PROFILE_JOURNEY_MODE") != "smoke",
    reason="set VONK_LINKED_PROFILE_JOURNEY_MODE=smoke to run the installed journey",
)
def test_installed_cli_links_profile_import_load_progress_and_published_endpoint(
    postgres_engine, installed_vonkctl: Path
) -> None:
    _run_linked_journey(postgres_engine, "smoke", installed_vonkctl)


@pytest.mark.skipif(
    os.environ.get("VONK_LINKED_PROFILE_JOURNEY_MODE") != "interactive",
    reason="set VONK_LINKED_PROFILE_JOURNEY_MODE=interactive to open the session",
)
def test_installed_cli_linked_profile_interactive_session(
    postgres_engine, installed_vonkctl: Path
) -> None:
    _run_linked_journey(postgres_engine, "interactive", installed_vonkctl)
