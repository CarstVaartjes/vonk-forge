from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import TypedDict

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import (
    CompiledExecutionPlan,
    ContainerRuntimeAction,
    ExecuteContainerRuntimeRequestOperation,
    RestartVonkUnitOperation,
    ScheduleRebootOperation,
    canonical_message,
    host_helper_grant_signing_bytes,
)
from vonk_agent_protocol.host_helper import (
    HostRuntimeRequest,
    RecipeReconciliationIdentity,
)
from vonk_agent_protocol.recipe_jobs import RecipeJobRunRequest
from vonk_agent_protocol.recipe_operations import (
    RecipeStartPayload,
    RecipeUninstallPayload,
)
from vonk_control.agent_api import HostRuntimeGrantRequest
from vonk_control.host_helper_authority import (
    HostHelperAuthorityError,
    HostHelperGrantIssuer,
    HostRuntimeAuthorityService,
)
from vonk_control.host_runtime_plan_authority import derive_runtime_plan_binding
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    ArtifactJob,
    Base,
    CatalogDocument,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    Job,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from vonk_control.recipe_execution_contract import (
    StoredInstallationPlan,
    StoredInstallNodePlan,
    StoredRunNodePlan,
    StoredRunPlan,
    installation_plan_document,
    run_plan_document,
)
from vonk_control.recipe_start_payloads import (
    RecipeStartPlacement,
    build_recipe_start_payload,
)
from vonk_control.recipe_stop_payloads import (
    stop_payload_from_job_run,
    stop_payload_from_start,
)
from vonk_forge_contracts import RecipeDefinition, content_sha256

NOW = datetime(2036, 7, 1, 12, 0, tzinfo=UTC)
REQUEST_ID = "10000000-0000-4000-8000-000000000001"
RUNTIME_RUN_ID = "70000000-0000-4000-8000-000000000007"
RUNTIME_INSTALLATION_ID = "80000000-0000-4000-8000-000000000008"


class _RuntimePlanBinding(TypedDict):
    start_plan_sha256: str | None
    stop_plan_sha256: str | None
    run_generation: int | None
    runtime_run_id: str | None
    runtime_target_id: str | None
    runtime_installation_id: str | None


def runtime_plan_binding(
    service: HostRuntimeAuthorityService,
    action: ContainerRuntimeAction,
    *,
    job_id: str = "20000000-0000-4000-8000-000000000002",
    operation_id: str = "30000000-0000-4000-8000-000000000003",
) -> _RuntimePlanBinding:
    with service._sessions() as session:
        parent = session.get(Job, job_id)
        operation = session.get(AgentOperation, operation_id)
        assert parent is not None and operation is not None
        binding = derive_runtime_plan_binding(
            session,
            parent=parent,
            operation=operation,
            node_id="spk_" + "1" * 32,
            action=action,
            cancellation_requested=bool(
                isinstance(parent.result, dict)
                and parent.result.get("cancel_requested") is True
            ),
            now=NOW,
        )
    return {
        "start_plan_sha256": binding.start_plan_sha256,
        "stop_plan_sha256": binding.stop_plan_sha256,
        "run_generation": binding.run_generation,
        "runtime_run_id": binding.runtime_run_id,
        "runtime_target_id": binding.runtime_target_id,
        "runtime_installation_id": binding.runtime_installation_id,
    }


def service_stop_runtime_service() -> tuple[HostRuntimeAuthorityService, str, str, str]:
    service = runtime_service()
    node_id = "spk_" + "1" * 32
    job_id = "a4000000-0000-4000-8000-00000000000a"
    operation_id = "a5000000-0000-4000-8000-00000000000a"
    fence = "a6000000-0000-4000-8000-00000000000a"
    with service._sessions.begin() as session:
        start_operation = session.get(
            AgentOperation, "30000000-0000-4000-8000-000000000003"
        )
        assert start_operation is not None
        start = RecipeStartPayload.model_validate_json(
            canonical_message(start_operation.payload)
        )
        stop = stop_payload_from_start(start, node_id, cancel_pending_start=False)
        stop_payload = json.loads(canonical_message(stop))
        parent_payload: dict[str, object] = {
            "schema_version": 1,
            "workload_intent_ordinal": 1,
            "owner_kind": "run",
            "owner_id": start.run_id,
            "plan_digest": start.plan_digest,
            "phases": [
                [
                    {
                        "operation_id": operation_id,
                        "node_id": node_id,
                        "payload": stop_payload,
                    }
                ]
            ],
        }
        authority_revision = start.plan_digest.removeprefix("sha256:")
        session.add(
            Job(
                id=job_id,
                request_id="a7000000-0000-4000-8000-00000000000a",
                kind="recipe.stop",
                state="running",
                actor="test",
                authority_revision=authority_revision,
                targets=[node_id],
                payload_digest=hashlib.sha256(
                    canonical_message(parent_payload)
                ).hexdigest(),
                payload=parent_payload,
                result=None,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.flush()
        session.add(
            AgentOperation(
                id=operation_id,
                parent_job_id=job_id,
                node_id=node_id,
                kind="recipe.stop",
                payload_digest=hashlib.sha256(
                    canonical_message(stop_payload)
                ).hexdigest(),
                payload=stop_payload,
                authority_revision=authority_revision,
                workload_intent_ordinal=1,
                state="running",
                current_attempt=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            AgentOperationAttempt(
                id="a8000000-0000-4000-8000-00000000000a",
                operation_id=operation_id,
                attempt=1,
                fence=fence,
                lease_deadline=NOW + timedelta(seconds=60),
                agent_certificate_serial="certificate-1",
                state="running",
            )
        )
    return service, job_id, operation_id, fence


def issuer() -> HostHelperGrantIssuer:
    return HostHelperGrantIssuer(
        ed25519.Ed25519PrivateKey.from_private_bytes(b"m" * 32),
        clock=lambda: NOW,
        request_id_factory=lambda: REQUEST_ID,
    )


def test_controller_issues_exact_short_lived_host_grant() -> None:
    authority = issuer()
    grant = authority.issue_grant(
        node_id="spk_" + "1" * 32,
        operation=RestartVonkUnitOperation(type="restart-vonk-unit", unit="agent"),
        expires_in_seconds=90,
    )

    assert grant.claims.request_id == REQUEST_ID
    assert grant.claims.expires_at - grant.claims.issued_at == 90
    authority.public_key.verify(
        bytes.fromhex(grant.signature.value),
        host_helper_grant_signing_bytes(grant.claims),
    )
    assert authority.public_key_document()["usage"] == "host-maintenance-grant"


def test_controller_signs_exact_job_bound_container_runtime_request() -> None:
    authority = issuer()
    grant = authority.issue_grant(
        node_id="spk_" + "1" * 32,
        operation=ExecuteContainerRuntimeRequestOperation(
            type="execute-container-runtime-request",
            action=ContainerRuntimeAction.START.value,
            job_id="20000000-0000-4000-8000-000000000002",
            operation_id="30000000-0000-4000-8000-000000000003",
            attempt=2,
            fence="40000000-0000-4000-8000-000000000004",
            request_sha256="a" * 64,
            start_plan_sha256="b" * 64,
            run_generation=1,
            runtime_run_id="50000000-0000-4000-8000-000000000005",
            runtime_target_id="50000000-0000-4000-8000-000000000005",
            runtime_installation_id="60000000-0000-4000-8000-000000000006",
        ),
        expires_in_seconds=30,
    )

    assert grant.claims.operation.to_mapping() == {
        "type": "execute-container-runtime-request",
        "action": "start",
        "job_id": "20000000-0000-4000-8000-000000000002",
        "operation_id": "30000000-0000-4000-8000-000000000003",
        "attempt": 2,
        "fence": "40000000-0000-4000-8000-000000000004",
        "request_sha256": "a" * 64,
        "start_plan_sha256": "b" * 64,
        "run_generation": 1,
        "runtime_run_id": "50000000-0000-4000-8000-000000000005",
        "runtime_target_id": "50000000-0000-4000-8000-000000000005",
        "runtime_installation_id": "60000000-0000-4000-8000-000000000006",
    }


@pytest.mark.parametrize("seconds", (0, 301, True))
def test_controller_refuses_unbounded_host_grants(seconds: object) -> None:
    with pytest.raises(HostHelperAuthorityError, match="expiry"):
        issuer().issue_grant(
            node_id="spk_" + "1" * 32,
            operation=ScheduleRebootOperation(
                type="schedule-reboot", delay_seconds=120
            ),
            expires_in_seconds=seconds,
        )


def test_controller_refuses_mapping_shaped_or_untyped_operations() -> None:
    with pytest.raises(HostHelperAuthorityError, match="operation"):
        issuer().issue_grant(
            node_id="spk_" + "1" * 32,
            operation={"type": "restart-vonk-unit", "unit": "agent"},
            expires_in_seconds=30,
        )


def runtime_service(
    *,
    lease_seconds: int = 60,
    operation_kind: str = "recipe.start",
    operation_payload: dict[str, object] | None = None,
    cancel_requested: bool = False,
    node_intent: int = 1,
    include_stop_hook: bool = False,
) -> HostRuntimeAuthorityService:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    node_id = "spk_" + "1" * 32
    second_node_id = "spk_" + "2" * 32
    job_id = "20000000-0000-4000-8000-000000000002"
    operation_id = "30000000-0000-4000-8000-000000000003"
    fence = "40000000-0000-4000-8000-000000000004"
    run_id = "70000000-0000-4000-8000-000000000007"
    installation_id = "80000000-0000-4000-8000-000000000008"
    revision_id = "90000000-0000-4000-8000-000000000009"
    mapping_id = "a0000000-0000-4000-8000-00000000000a"
    plan_digest = "a" * 64
    lifecycle = operation_kind in {"recipe.start", "recipe.job.run.v1"}
    recipe_raw = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-image.json")
        .read_text(encoding="utf-8")
    )
    recipe_raw["identity"].update(publisher="vonk-forge", slug="authority-test")
    recipe = RecipeDefinition.model_validate_json(canonical_message(recipe_raw))
    recipe_document = json.loads(canonical_message(recipe))
    recipe_digest = content_sha256(recipe)
    collective = (
        operation_kind == "recipe.start"
        and operation_payload is not None
        and operation_payload.get("phase") == "collective-readiness"
    )
    target_nodes = [node_id, second_node_id] if collective else [node_id]
    job_run_request: RecipeJobRunRequest | None = None
    phases: list[list[dict[str, object]]]

    if operation_kind == "recipe.start":
        fixture_path = Path(__file__).parent / "fixtures/compiled_workload_v2.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        fixture["identity"]["recipe_revision_sha256"] = recipe_digest
        if collective:
            fixture["topology"].update(
                name="dual", mode="distributed", node_count=2, backend="mp"
            )
            fixture["security"]["devices"] = ["nvidia.com/gpu=all"]
        image_digest = fixture["runtime"]["image_digest"]
        deadline = (NOW + timedelta(minutes=5)).isoformat()

        def build_start(
            *, rank: int, target_node: str, phase: str | None
        ) -> dict[str, object]:
            role = "entrypoint" if rank == 0 else "worker"
            local_address = "192.0.2.10" if rank == 0 else "192.0.2.11"
            distributed = len(target_nodes) > 1
            document = build_recipe_start_payload(
                run_id=run_id,
                installation_id=installation_id,
                recipe_revision_id=revision_id,
                recipe_content_sha256=recipe_digest,
                mapping_id=mapping_id,
                mapping_generation=1,
                run_generation=1,
                image_digest=image_digest,
                plan_digest=plan_digest,
                alias="authority-test",
                placement=RecipeStartPlacement(
                    node_id=target_node,
                    rank=rank,
                    role=role,
                    port=8000,
                    reserved_memory_bytes=80_000_000,
                    memory_floor_bytes=0,
                    memory_kind="unified",
                    fabric_address=local_address if distributed else None,
                ),
                endpoint_address=local_address,
                compiled_endpoint_address=("192.0.2.10" if rank == 0 else None)
                if distributed
                else "192.0.2.10",
                world_size=len(target_nodes),
                compiled_execution_plan=fixture,
                local_address=local_address if distributed else None,
                master_address="192.0.2.10" if distributed else None,
                master_port=29500 if distributed else None,
                phase=phase,
                start_deadline=deadline if phase is not None else None,
            )
            return json.loads(canonical_message(document))

        if collective:
            launch_documents = [
                build_start(rank=rank, target_node=target, phase="rank-launch")
                for rank, target in enumerate(target_nodes)
            ]
            operation_document = build_start(
                rank=0, target_node=node_id, phase="collective-readiness"
            )
            phases = [
                [
                    {
                        "operation_id": (
                            "30000000-0000-4000-8000-00000000000d"
                            if rank == 0
                            else "30000000-0000-4000-8000-00000000000c"
                        ),
                        "node_id": target,
                        "payload": document,
                    }
                    for rank, (target, document) in enumerate(
                        zip(target_nodes, launch_documents, strict=True)
                    )
                ],
                [
                    {
                        "operation_id": operation_id,
                        "node_id": node_id,
                        "payload": operation_document,
                    }
                ],
            ]
        else:
            operation_document = build_start(rank=0, target_node=node_id, phase=None)
            phases = [
                [
                    {
                        "operation_id": operation_id,
                        "node_id": node_id,
                        "payload": operation_document,
                    }
                ]
            ]
    elif operation_kind == "recipe.job.run.v1":
        vector_path = (
            Path(__file__).parents[2]
            / "agent_protocol/src/vonk_agent_protocol/vectors/recipe-job-run-claim-v1.json"
        )
        vector = json.loads(vector_path.read_text(encoding="utf-8"))
        request_document = vector["payload"]
        if include_stop_hook:
            compiled = request_document["compiled_execution_plan"]
            assert isinstance(compiled, dict)
            lifecycle = compiled["lifecycle"]
            assert isinstance(lifecycle, dict)
            lifecycle["post_stop"] = [["/usr/bin/true"]]
        request_document.update(
            {
                "job_id": "b0000000-0000-4000-8000-00000000000b",
                "run_id": run_id,
                "installation_id": installation_id,
                "mapping_id": mapping_id,
                "mapping_generation": 1,
                "run_generation": 1,
                "recipe_revision_id": revision_id,
                "plan_digest": plan_digest,
                "recipe_content_sha256": recipe_digest,
            }
        )
        request_document["compiled_execution_plan"]["identity"][
            "recipe_revision_sha256"
        ] = recipe_digest
        job_run_request = RecipeJobRunRequest.model_validate_json(
            canonical_message(request_document)
        )
        operation_document = json.loads(canonical_message(job_run_request))
        phases = [
            [
                {
                    "operation_id": operation_id,
                    "node_id": node_id,
                    "payload": operation_document,
                }
            ]
        ]
    else:
        operation_document = operation_payload or {}
        phases = []

    operation_digest = hashlib.sha256(canonical_message(operation_document)).hexdigest()
    authority_revision = (
        job_run_request.recipe_content_sha256
        if job_run_request is not None
        else RecipeStartPayload.model_validate_json(
            canonical_message(operation_document)
        ).recipe_content_sha256
        if operation_kind == "recipe.start"
        else "b" * 64
    )
    compiled_by_node: dict[str, CompiledExecutionPlan] = {}
    if job_run_request is not None:
        compiled_by_node[node_id] = job_run_request.compiled_execution_plan
    elif operation_kind == "recipe.start":
        for entry in phases[0]:
            payload = entry["payload"]
            assert isinstance(payload, dict)
            compiled_by_node[str(entry["node_id"])] = (
                CompiledExecutionPlan.model_validate_json(
                    canonical_message(payload["compiled_execution_plan"])
                )
            )
    parent_payload: dict[str, object] = {
        "schema_version": 1,
        "workload_intent_ordinal": 1,
    }
    if lifecycle:
        parent_payload.update(
            {
                "owner_kind": (
                    "artifact-job" if job_run_request is not None else "run"
                ),
                "owner_id": (
                    job_run_request.job_id if job_run_request is not None else run_id
                ),
                "plan_digest": plan_digest,
                "phases": phases,
            }
        )
    parent_digest = hashlib.sha256(canonical_message(parent_payload)).hexdigest()

    with sessions.begin() as session:
        for target_node in target_nodes:
            session.add(
                AgentNode(
                    node_id=target_node,
                    state="active",
                    capabilities=[operation_kind],
                    workload_intent_ordinal=node_intent,
                )
            )
        session.add(
            AgentCertificate(
                serial="certificate-1",
                node_id=node_id,
                not_before=NOW,
                not_after=NOW + timedelta(minutes=5),
                fingerprint="fingerprint-1",
            )
        )
        if lifecycle:
            if job_run_request is not None:
                recipe_digest = job_run_request.recipe_content_sha256
                image_digest = job_run_request.image_digest
            else:
                start = RecipeStartPayload.model_validate_json(
                    canonical_message(operation_document)
                )
                recipe_digest = start.recipe_content_sha256
                image_digest = start.image_digest

            install_nodes = []
            run_nodes = []
            for rank, target_node in enumerate(target_nodes):
                compiled = compiled_by_node[target_node]
                placement = compiled.runtime.placement
                role = placement.role
                required_bytes = compiled.identity.model_artifact_bytes
                install_nodes.append(
                    StoredInstallNodePlan(
                        node_id=target_node,
                        rank=rank,
                        role=role,
                        allowed=True,
                        inventory_observed_at=NOW.isoformat(),
                        free_bytes=1_000_000_000,
                        active_reserved_bytes=0,
                        reused_bytes=required_bytes,
                        required_download_bytes=0,
                        required_bytes=required_bytes,
                        disk_floor_bytes=0,
                        free_after_bytes=1_000_000_000 - required_bytes,
                        blockers=[],
                        warnings=[],
                    )
                )
                run_nodes.append(
                    StoredRunNodePlan(
                        node_id=target_node,
                        rank=rank,
                        role=role,
                        endpoint_owner=rank == 0,
                        port=placement.port or 8000,
                        allowed=True,
                        inventory_observed_at=NOW.isoformat(),
                        memory_kind=placement.memory_kind,
                        memory_pool="shared",
                        required_memory_bytes=placement.reserved_memory_bytes,
                        available_memory_bytes=None,
                        active_reserved_bytes=0,
                        free_after_bytes=None,
                        memory_floor_bytes=placement.memory_floor_bytes,
                        fabric_address=placement.local_address,
                        fabric_bandwidth_mbps=None,
                        rendezvous_port=placement.master_port,
                        blockers=[],
                        warnings=[],
                    )
                )
            stored_installation = StoredInstallationPlan(
                schema_version=1,
                mapping_id=mapping_id,
                mapping_generation=1,
                recipe_build_id=None,
                image_digest=image_digest,
                recipe_revision_id=revision_id,
                recipe_content_sha256=recipe_digest,
                allowed=True,
                nodes=install_nodes,
                plan_digest=plan_digest,
                compiled_execution_plans=compiled_by_node,
            )

            session.add(
                CatalogDocument(
                    id="a1000000-0000-4000-8000-00000000000a",
                    kind="recipe",
                    publisher="vonk-forge",
                    slug="authority-test",
                    title=recipe.metadata.title,
                    created_by="test",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            session.add(
                CatalogDocumentRevision(
                    id=revision_id,
                    document_id="a1000000-0000-4000-8000-00000000000a",
                    kind="recipe",
                    publisher="vonk-forge",
                    slug="authority-test",
                    revision_number=1,
                    schema_version=2,
                    state="active",
                    document=recipe_document,
                    content_digest=recipe_digest,
                    projected={},
                    created_by="test",
                    created_at=NOW,
                )
            )
            session.flush()
            mapping = ClusterMapping(
                id=mapping_id,
                recipe_revision_id=revision_id,
                topology_name="dual" if collective else "solo",
                generation=1,
                node_count=len(target_nodes),
                state="planned",
                parameters={},
                placement_digest="e" * 64,
                endpoint_owner_node_id=node_id,
                created_by="test",
                created_at=NOW,
                updated_at=NOW,
            )
            session.add(mapping)
            session.flush()
            for rank, target_node in enumerate(target_nodes):
                session.add(
                    ClusterMappingNode(
                        id=(
                            "a2000000-0000-4000-8000-00000000000a"
                            if rank == 0
                            else "a2000000-0000-4000-8000-00000000000b"
                        ),
                        mapping_id=mapping_id,
                        node_id=target_node,
                        rank=rank,
                        role="entrypoint" if rank == 0 else "worker",
                        endpoint_owner=rank == 0,
                        created_at=NOW,
                    )
                )
            session.flush()
            mapping.state = "ready"

            session.add(
                RecipeInstallation(
                    id=installation_id,
                    recipe_revision_id=revision_id,
                    mapping_id=mapping_id,
                    mapping_generation=1,
                    image_digest=image_digest,
                    plan_digest=plan_digest,
                    plan=installation_plan_document(stored_installation),
                    state="installed",
                    actor="test",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            stored_plan_document: dict[str, object] = {
                "schema_version": 1,
                "observation_schema_version": 2,
                "run_generation": 1,
                "installation_id": installation_id,
                "alias": "authority-test",
                "mapping_id": mapping_id,
                "mapping_generation": 1,
                "recipe_revision_id": revision_id,
                "plan_digest": plan_digest,
                "nodes": [node.model_dump(mode="json") for node in run_nodes],
            }
            if job_run_request is not None:
                stored_plan_document["execution_mode"] = "one-shot-jobs"
            stored_plan = StoredRunPlan.model_validate_json(
                canonical_message(stored_plan_document)
            )
            session.add(
                RecipeRun(
                    id=run_id,
                    installation_id=installation_id,
                    mapping_id=mapping_id,
                    mapping_generation=1,
                    run_generation=1,
                    alias="authority-test",
                    plan_digest=plan_digest,
                    plan=run_plan_document(stored_plan),
                    state="running" if job_run_request is not None else "starting",
                    route_state="withdrawn",
                    route_attempts=0,
                    actor="test",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            for rank, target_node in enumerate(target_nodes):
                session.add(
                    RunNode(
                        id=(
                            "a3000000-0000-4000-8000-00000000000a"
                            if rank == 0
                            else "a3000000-0000-4000-8000-00000000000b"
                        ),
                        run_id=run_id,
                        node_id=target_node,
                        rank=rank,
                        role=run_nodes[rank].role,
                        state="starting",
                        port=8000,
                        reserved_memory_bytes=run_nodes[rank].required_memory_bytes,
                        updated_at=NOW,
                    )
                )

        session.add(
            Job(
                id=job_id,
                request_id="50000000-0000-4000-8000-000000000005",
                kind=operation_kind,
                state="running",
                actor="admin",
                authority_revision=authority_revision,
                targets=sorted(target_nodes),
                payload_digest=parent_digest,
                payload=parent_payload,
                result={
                    "cancel_requested": True,
                    "cancel_requested_at": NOW.isoformat(),
                }
                if cancel_requested
                else None,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            AgentOperation(
                id=operation_id,
                parent_job_id=job_id,
                node_id=node_id,
                kind=operation_kind,
                payload_digest=operation_digest,
                payload=operation_document,
                authority_revision=authority_revision,
                workload_intent_ordinal=1,
                state="running",
                current_attempt=2,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        if job_run_request is not None:
            job_contract = job_run_request.compiled_execution_plan.job
            assert job_contract is not None
            session.add(
                ArtifactJob(
                    id=job_run_request.job_id,
                    run_id=run_id,
                    operation_id=job_id,
                    request_id="b1000000-0000-4000-8000-00000000000b",
                    interface=job_run_request.interface,
                    parameters={},
                    output_limits=job_run_request.output_limits.model_dump(
                        mode="json", exclude_none=True
                    ),
                    compiled_contract=job_contract.model_dump(
                        mode="json", exclude_none=True
                    ),
                    contract_sha256=job_run_request.contract_sha256,
                    state="running",
                    input_manifest={
                        "files": [
                            item.model_dump(mode="json", exclude_none=True)
                            for item in job_run_request.inputs
                        ],
                        "manifest_sha256": job_run_request.input_manifest_sha256,
                        "total_bytes": job_run_request.input_total_bytes,
                    },
                    input_manifest_sha256=job_run_request.input_manifest_sha256,
                    input_total_bytes=job_run_request.input_total_bytes,
                    timeout_seconds=job_run_request.timeout_seconds,
                    actor="test",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        session.add(
            AgentOperationAttempt(
                id="60000000-0000-4000-8000-000000000006",
                operation_id=operation_id,
                attempt=2,
                fence=fence,
                lease_deadline=NOW + timedelta(seconds=lease_seconds),
                agent_certificate_serial="certificate-1",
                state="running",
            )
        )
    return HostRuntimeAuthorityService(sessions, issuer(), clock=lambda: NOW)


def test_agent_upgrade_authority_binds_the_live_attempt_and_exact_signed_package() -> (
    None
):
    package = upgrade_payload()
    service = runtime_service(
        operation_kind="agent.upgrade.v1",
        operation_payload=package,
    )

    grant = service.issue_agent_upgrade_grant(
        node_id="spk_" + "1" * 32,
        job_id="20000000-0000-4000-8000-000000000002",
        operation_id="30000000-0000-4000-8000-000000000003",
        attempt=2,
        fence="40000000-0000-4000-8000-000000000004",
        package_sha256=package["package_sha256"],
        package_signature=package["package_signature"],
        certificate_serial="certificate-1",
        expires_in_seconds=30,
    )

    assert grant.claims.operation.to_mapping() == {
        "type": "install-vonk-deb",
        "package_sha256": package["package_sha256"],
        "package_signature": package["package_signature"],
        "rollback": package["rollback"],
    }
    with pytest.raises(HostHelperAuthorityError, match="stale"):
        service.issue_agent_upgrade_grant(
            node_id="spk_" + "1" * 32,
            job_id="20000000-0000-4000-8000-000000000002",
            operation_id="30000000-0000-4000-8000-000000000003",
            attempt=2,
            fence="40000000-0000-4000-8000-000000000004",
            package_sha256="c" * 64,
            package_signature=package["package_signature"],
            certificate_serial="certificate-1",
            expires_in_seconds=30,
        )


def test_runtime_authority_binds_active_attempt_action_and_request() -> None:
    service = runtime_service()
    binding = runtime_plan_binding(service, ContainerRuntimeAction.START)
    grant = service.issue_grant(
        node_id="spk_" + "1" * 32,
        job_id="20000000-0000-4000-8000-000000000002",
        operation_id="30000000-0000-4000-8000-000000000003",
        attempt=2,
        fence="40000000-0000-4000-8000-000000000004",
        action=ContainerRuntimeAction.START,
        request_sha256="e" * 64,
        certificate_serial="certificate-1",
        **binding,
    )

    operation = grant.claims.operation
    assert isinstance(operation, ExecuteContainerRuntimeRequestOperation)
    assert operation.request_sha256 == "e" * 64
    assert operation.action == "start"
    assert operation.start_plan_sha256 == binding["start_plan_sha256"]
    assert operation.run_generation == 1
    assert operation.runtime_run_id == RUNTIME_RUN_ID
    assert operation.runtime_target_id == RUNTIME_RUN_ID
    assert operation.runtime_installation_id == RUNTIME_INSTALLATION_ID
    issuer().public_key.verify(
        bytes.fromhex(grant.signature.value),
        host_helper_grant_signing_bytes(grant.claims),
    )

    inspect = service.issue_grant(
        node_id="spk_" + "1" * 32,
        job_id="20000000-0000-4000-8000-000000000002",
        operation_id="30000000-0000-4000-8000-000000000003",
        attempt=2,
        fence="40000000-0000-4000-8000-000000000004",
        action=ContainerRuntimeAction.RUN_INSPECT,
        request_sha256="f" * 64,
        certificate_serial="certificate-1",
    )
    inspect_operation = inspect.claims.operation
    assert isinstance(inspect_operation, ExecuteContainerRuntimeRequestOperation)
    assert inspect_operation.action == "run-inspect"


def test_job_run_stop_grant_preserves_logical_and_runtime_target_identity() -> None:
    service = runtime_service(operation_kind="recipe.job.run.v1", cancel_requested=True)
    binding = runtime_plan_binding(service, ContainerRuntimeAction.STOP)
    grant = service.issue_grant(
        node_id="spk_" + "1" * 32,
        job_id="20000000-0000-4000-8000-000000000002",
        operation_id="30000000-0000-4000-8000-000000000003",
        attempt=2,
        fence="40000000-0000-4000-8000-000000000004",
        action=ContainerRuntimeAction.STOP,
        request_sha256="e" * 64,
        certificate_serial="certificate-1",
        **binding,
    )

    operation = grant.claims.operation
    assert isinstance(operation, ExecuteContainerRuntimeRequestOperation)
    assert operation.stop_plan_sha256 == binding["stop_plan_sha256"]
    assert operation.run_generation == 1
    assert operation.runtime_run_id == RUNTIME_RUN_ID
    assert operation.runtime_target_id == "b0000000-0000-4000-8000-00000000000b"
    assert operation.runtime_target_id != operation.runtime_run_id
    assert operation.runtime_installation_id == RUNTIME_INSTALLATION_ID
    issuer().public_key.verify(
        bytes.fromhex(grant.signature.value),
        host_helper_grant_signing_bytes(grant.claims),
    )


def test_service_stop_grant_signs_exact_durable_prior_start() -> None:
    service, job_id, operation_id, fence = service_stop_runtime_service()
    binding = runtime_plan_binding(
        service,
        ContainerRuntimeAction.STOP,
        job_id=job_id,
        operation_id=operation_id,
    )
    grant = service.issue_grant(
        node_id="spk_" + "1" * 32,
        job_id=job_id,
        operation_id=operation_id,
        attempt=1,
        fence=fence,
        action=ContainerRuntimeAction.STOP,
        request_sha256="e" * 64,
        certificate_serial="certificate-1",
        **binding,
    )

    operation = grant.claims.operation
    assert isinstance(operation, ExecuteContainerRuntimeRequestOperation)
    assert operation.stop_plan_sha256 == binding["stop_plan_sha256"]
    assert operation.run_generation == 1
    assert operation.runtime_run_id == RUNTIME_RUN_ID
    assert operation.runtime_target_id == RUNTIME_RUN_ID
    assert operation.runtime_installation_id == RUNTIME_INSTALLATION_ID
    issuer().public_key.verify(
        bytes.fromhex(grant.signature.value),
        host_helper_grant_signing_bytes(grant.claims),
    )


def test_job_run_stop_rejects_wrong_runtime_target() -> None:
    service = runtime_service(operation_kind="recipe.job.run.v1", cancel_requested=True)
    binding = runtime_plan_binding(service, ContainerRuntimeAction.STOP)
    wrong_target_binding: _RuntimePlanBinding = {
        **binding,
        "runtime_target_id": RUNTIME_RUN_ID,
    }

    with pytest.raises(HostHelperAuthorityError, match="lifecycle binding"):
        service.issue_grant(
            node_id="spk_" + "1" * 32,
            job_id="20000000-0000-4000-8000-000000000002",
            operation_id="30000000-0000-4000-8000-000000000003",
            attempt=2,
            fence="40000000-0000-4000-8000-000000000004",
            action=ContainerRuntimeAction.STOP,
            request_sha256="e" * 64,
            certificate_serial="certificate-1",
            **wrong_target_binding,
        )


def test_hook_bearing_job_run_stop_fails_closed_before_signing() -> None:
    service = runtime_service(
        operation_kind="recipe.job.run.v1",
        cancel_requested=True,
        include_stop_hook=True,
    )
    with service._sessions() as session:
        operation = session.get(AgentOperation, "30000000-0000-4000-8000-000000000003")
        assert operation is not None
        job_plan = RecipeJobRunRequest.model_validate_json(
            canonical_message(operation.payload)
        )
        stop = stop_payload_from_job_run(
            job_plan,
            "spk_" + "1" * 32,
            cancel_pending_start=True,
        )
    binding = {
        "start_plan_sha256": None,
        "stop_plan_sha256": hashlib.sha256(canonical_message(stop)).hexdigest(),
        "run_generation": job_plan.run_generation,
        "runtime_run_id": job_plan.run_id,
        "runtime_target_id": job_plan.job_id,
        "runtime_installation_id": job_plan.installation_id,
    }
    with pytest.raises(HostHelperAuthorityError, match="lifecycle authority"):
        service.issue_grant(
            node_id="spk_" + "1" * 32,
            job_id="20000000-0000-4000-8000-000000000002",
            operation_id="30000000-0000-4000-8000-000000000003",
            attempt=2,
            fence="40000000-0000-4000-8000-000000000004",
            action=ContainerRuntimeAction.STOP,
            request_sha256="e" * 64,
            certificate_serial="certificate-1",
            **binding,
        )


def test_collective_readiness_grant_is_strictly_inspect_only() -> None:
    service = runtime_service(operation_payload={"phase": "collective-readiness"})
    common = {
        "node_id": "spk_" + "1" * 32,
        "job_id": "20000000-0000-4000-8000-000000000002",
        "operation_id": "30000000-0000-4000-8000-000000000003",
        "attempt": 2,
        "fence": "40000000-0000-4000-8000-000000000004",
        "request_sha256": "e" * 64,
        "certificate_serial": "certificate-1",
    }
    inspected = service.issue_grant(
        **common, action=ContainerRuntimeAction.RUN_INSPECT
    ).claims.operation
    assert isinstance(inspected, ExecuteContainerRuntimeRequestOperation)
    assert inspected.action == "run-inspect"
    for action in (ContainerRuntimeAction.START, ContainerRuntimeAction.STOP):
        with pytest.raises(HostHelperAuthorityError, match="stale"):
            service.issue_grant(**common, action=action)


@pytest.mark.parametrize("operation_kind", ["recipe.start", "recipe.job.run.v1"])
@pytest.mark.parametrize("node_intent", [1, 2])
def test_cancellation_permits_only_stop_under_the_original_live_fence(
    operation_kind: str,
    node_intent: int,
) -> None:
    service = runtime_service(
        operation_kind=operation_kind,
        cancel_requested=True,
        node_intent=node_intent,
    )

    arguments = {
        "node_id": "spk_" + "1" * 32,
        "job_id": "20000000-0000-4000-8000-000000000002",
        "operation_id": "30000000-0000-4000-8000-000000000003",
        "attempt": 2,
        "fence": "40000000-0000-4000-8000-000000000004",
        "request_sha256": "e" * 64,
        "certificate_serial": "certificate-1",
    }
    grant = service.issue_grant(
        **arguments,
        action=ContainerRuntimeAction.STOP,
        **runtime_plan_binding(service, ContainerRuntimeAction.STOP),
    )

    operation = grant.claims.operation
    assert isinstance(operation, ExecuteContainerRuntimeRequestOperation)
    assert operation.action == ContainerRuntimeAction.STOP.value
    for action in (ContainerRuntimeAction.START, ContainerRuntimeAction.RUN_INSPECT):
        with pytest.raises(HostHelperAuthorityError, match="stale"):
            service.issue_grant(**arguments, action=action)
    for changes in ({"fence": "0" * 36}, {"certificate_serial": "wrong-certificate"}):
        with pytest.raises(HostHelperAuthorityError, match="stale"):
            service.issue_grant(
                **{**arguments, **changes},
                action=ContainerRuntimeAction.STOP,
                **runtime_plan_binding(service, ContainerRuntimeAction.STOP),
            )


def test_superseded_attempt_needs_recorded_cancellation_even_for_stop() -> None:
    service = runtime_service(node_intent=2)
    for action in (ContainerRuntimeAction.START, ContainerRuntimeAction.STOP):
        with pytest.raises(HostHelperAuthorityError, match="stale"):
            service.issue_grant(
                node_id="spk_" + "1" * 32,
                job_id="20000000-0000-4000-8000-000000000002",
                operation_id="30000000-0000-4000-8000-000000000003",
                attempt=2,
                fence="40000000-0000-4000-8000-000000000004",
                action=action,
                request_sha256="e" * 64,
                certificate_serial="certificate-1",
            )


def test_collective_cancellation_can_stop_but_cannot_extend_old_work() -> None:
    service = runtime_service(
        operation_payload={"phase": "collective-readiness"},
        cancel_requested=True,
        node_intent=2,
    )
    arguments = {
        "node_id": "spk_" + "1" * 32,
        "job_id": "20000000-0000-4000-8000-000000000002",
        "operation_id": "30000000-0000-4000-8000-000000000003",
        "attempt": 2,
        "fence": "40000000-0000-4000-8000-000000000004",
        "request_sha256": "e" * 64,
        "certificate_serial": "certificate-1",
    }
    operation = service.issue_grant(
        **arguments,
        action=ContainerRuntimeAction.STOP,
        **runtime_plan_binding(service, ContainerRuntimeAction.STOP),
    ).claims.operation
    assert isinstance(operation, ExecuteContainerRuntimeRequestOperation)
    assert operation.action == "stop"
    with pytest.raises(HostHelperAuthorityError, match="stale"):
        service.issue_grant(**arguments, action=ContainerRuntimeAction.RUN_INSPECT)
    expired = runtime_service(lease_seconds=0, cancel_requested=True, node_intent=2)
    with pytest.raises(HostHelperAuthorityError, match="stale"):
        expired.issue_grant(
            **arguments,
            action=ContainerRuntimeAction.STOP,
            **runtime_plan_binding(expired, ContainerRuntimeAction.STOP),
        )


def test_long_attempt_lease_does_not_extend_the_cancellation_deadline() -> None:
    service = runtime_service(lease_seconds=1200, cancel_requested=True, node_intent=2)
    with service._sessions.begin() as session:
        job = session.get(Job, "20000000-0000-4000-8000-000000000002")
        assert job is not None
        job.result = {
            "cancel_requested": True,
            "cancel_requested_at": (NOW - timedelta(seconds=660)).isoformat(),
        }
    with pytest.raises(HostHelperAuthorityError, match="stale"):
        service.issue_grant(
            node_id="spk_" + "1" * 32,
            job_id="20000000-0000-4000-8000-000000000002",
            operation_id="30000000-0000-4000-8000-000000000003",
            attempt=2,
            fence="40000000-0000-4000-8000-000000000004",
            action=ContainerRuntimeAction.STOP,
            request_sha256="e" * 64,
            certificate_serial="certificate-1",
        )


def test_runtime_authority_rejects_action_not_owned_by_active_operation() -> None:
    with pytest.raises(HostHelperAuthorityError, match="action"):
        runtime_service().issue_grant(
            node_id="spk_" + "1" * 32,
            job_id="20000000-0000-4000-8000-000000000002",
            operation_id="30000000-0000-4000-8000-000000000003",
            attempt=2,
            fence="40000000-0000-4000-8000-000000000004",
            action=ContainerRuntimeAction.IMAGE_IMPORT,
            request_sha256="e" * 64,
            certificate_serial="certificate-1",
        )


def test_runtime_preflight_grant_is_bound_to_its_own_fenced_operation() -> None:
    arguments = {
        "node_id": "spk_" + "1" * 32,
        "job_id": "20000000-0000-4000-8000-000000000002",
        "operation_id": "30000000-0000-4000-8000-000000000003",
        "attempt": 2,
        "fence": "40000000-0000-4000-8000-000000000004",
        "action": ContainerRuntimeAction.RUNTIME_PREFLIGHT,
        "request_sha256": "e" * 64,
        "certificate_serial": "certificate-1",
    }
    service = runtime_service(operation_kind="runtime.preflight.v1")
    grant = service.issue_grant(**arguments)
    operation = grant.claims.operation
    assert isinstance(operation, ExecuteContainerRuntimeRequestOperation)
    assert operation.action == "runtime-preflight"
    assert operation.request_sha256 == "e" * 64
    for changes in [
        {"fence": "50000000-0000-4000-8000-000000000005"},
        {"action": ContainerRuntimeAction.START},
    ]:
        with pytest.raises(HostHelperAuthorityError):
            service.issue_grant(**{**arguments, **changes})
    with pytest.raises(HostHelperAuthorityError):
        runtime_service(operation_kind="recipe.start").issue_grant(**arguments)


def test_runtime_authority_never_issues_a_grant_past_the_attempt_lease() -> None:
    service = runtime_service(lease_seconds=10)
    with pytest.raises(HostHelperAuthorityError, match="lease"):
        service.issue_grant(
            node_id="spk_" + "1" * 32,
            job_id="20000000-0000-4000-8000-000000000002",
            operation_id="30000000-0000-4000-8000-000000000003",
            attempt=2,
            fence="40000000-0000-4000-8000-000000000004",
            action=ContainerRuntimeAction.START,
            request_sha256="e" * 64,
            certificate_serial="certificate-1",
            expires_in_seconds=30,
            **runtime_plan_binding(service, ContainerRuntimeAction.START),
        )


def upgrade_payload():
    return {
        "schema_version": 1,
        "architecture": "linux-arm64",
        "package_sha256": "a" * 64,
        "package_signature": "b" * 128,
        "package_bytes": 1000,
        "package_version": "0.1.2",
        "package_url": "https://install.vonkforge.ai/artifacts/candidate/vonk-forge-agent.deb",
        "target_binary_digest": "c" * 64,
        "target_build_digest": "sha256:" + "d" * 64,
        "source_package_url": "https://install.vonkforge.ai/artifacts/source/vonk-forge-agent.deb",
        "source_package_bytes": 999,
        "rollback": {
            "attempt_nonce": "e" * 64,
            "activation_deadline": int(NOW.timestamp()) + 900,
            "source": {
                "package_sha256": "f" * 64,
                "package_signature": "1" * 128,
                "package_version": "0.1.1",
                "binary_sha256": "2" * 64,
                "helper_sha256": "3" * 64,
            },
        },
    }


def test_activation_grant_is_bound_to_live_source_candidate_nonce_and_identity():
    from vonk_agent_protocol.claims import AgentRuntimeIdentity
    from vonk_agent_protocol.package_upgrade import PackageActivationReceipt

    payload = upgrade_payload()
    service = runtime_service(
        operation_kind="agent.upgrade.v1", operation_payload=payload
    )
    receipt = PackageActivationReceipt(
        schema_version=2,
        node_id="spk_" + "1" * 32,
        source_package_sha256="f" * 64,
        source_version="0.1.1",
        source_binary_sha256="2" * 64,
        candidate_package_sha256="a" * 64,
        candidate_version="0.1.2",
        candidate_binary_sha256="c" * 64,
        attempt_nonce="e" * 64,
        phase="armed",
        created_at=int(NOW.timestamp()),
        updated_at=int(NOW.timestamp()),
        outcome="watchdog_armed",
    )
    identity = AgentRuntimeIdentity(
        architecture="linux-arm64",
        semantic_version="0.1.2",
        build_digest="sha256:" + "d" * 64,
        binary_digest="c" * 64,
        self_test_passed=True,
        observation_receipt_public_key="4" * 64,
    )
    grant = service.issue_package_activation_grant(
        node_id=receipt.node_id,
        receipt=receipt,
        runtime_identity=identity,
        certificate_serial="certificate-1",
    )
    assert grant.claims.operation.to_mapping() == {
        "type": "confirm-package-activation",
        "package_sha256": "a" * 64,
        "attempt_nonce": "e" * 64,
    }
    issuer().public_key.verify(
        bytes.fromhex(grant.signature.value),
        host_helper_grant_signing_bytes(grant.claims),
    )
    for changed in (
        {"attempt_nonce": "0" * 64},
        {"source_package_sha256": "0" * 64},
        {"phase": "rolled_back"},
        {"candidate_binary_sha256": "0" * 64},
    ):
        invalid = PackageActivationReceipt.model_validate(
            {**receipt.model_dump(mode="json"), **changed}
        )
        with pytest.raises(HostHelperAuthorityError):
            service.issue_package_activation_grant(
                node_id=receipt.node_id,
                receipt=invalid,
                runtime_identity=identity,
                certificate_serial="certificate-1",
            )
    with pytest.raises(HostHelperAuthorityError):
        service.issue_package_activation_grant(
            node_id=receipt.node_id,
            receipt=receipt,
            runtime_identity=identity.model_copy(update={"binary_digest": "0" * 64}),
            certificate_serial="certificate-1",
        )


INSTALLATION_ID = "70000000-0000-4000-8000-000000000007"
SECOND_INSTALLATION_ID = "80000000-0000-4000-8000-000000000008"
OUTSIDE_INSTALLATION_ID = "90000000-0000-4000-8000-000000000009"


class _CleanupGrantArguments(TypedDict):
    node_id: str
    job_id: str
    operation_id: str
    attempt: int
    fence: str
    action: ContainerRuntimeAction
    request_sha256: str
    certificate_serial: str
    installation_id: str | None


def cleanup_grant_arguments(installation_id: str) -> _CleanupGrantArguments:
    request = HostRuntimeRequest(
        schema_version=1,
        action="installation-cleanup",
        job_id="20000000-0000-4000-8000-000000000002",
        operation_id="30000000-0000-4000-8000-000000000003",
        attempt=2,
        fence="40000000-0000-4000-8000-000000000004",
        arguments=[],
        installation_id=installation_id,
    )
    return {
        "node_id": "spk_" + "1" * 32,
        "job_id": request.job_id,
        "operation_id": request.operation_id,
        "attempt": request.attempt,
        "fence": request.fence,
        "action": ContainerRuntimeAction.INSTALLATION_CLEANUP,
        "request_sha256": hashlib.sha256(canonical_message(request)).hexdigest(),
        "certificate_serial": "certificate-1",
        "installation_id": installation_id,
    }


def test_cleanup_grants_bind_only_installations_in_canonical_operation_payload() -> (
    None
):
    operation_kind = "recipe.uninstall"
    payload = RecipeUninstallPayload(
        schema_version=1,
        installation_id=INSTALLATION_ID,
        plan_digest="a" * 64,
        recipe_content_sha256="b" * 64,
        cleanup_model_content_sha256=None,
    )
    authorized = [INSTALLATION_ID]
    unauthorized = [SECOND_INSTALLATION_ID, OUTSIDE_INSTALLATION_ID]
    service = runtime_service(
        operation_kind=operation_kind,
        operation_payload=json.loads(canonical_message(payload)),
    )
    for installation_id in authorized:
        arguments = cleanup_grant_arguments(installation_id)
        grant = service.issue_grant(**arguments)
        operation = grant.claims.operation
        assert isinstance(operation, ExecuteContainerRuntimeRequestOperation)
        assert operation.installation_id == installation_id
        assert operation.action == "installation-cleanup"
        assert operation.request_sha256 == arguments["request_sha256"]
        issuer().public_key.verify(
            bytes.fromhex(grant.signature.value),
            host_helper_grant_signing_bytes(grant.claims),
        )
    for installation_id in unauthorized:
        with pytest.raises(
            HostHelperAuthorityError, match="installation is unauthorized"
        ):
            service.issue_grant(**cleanup_grant_arguments(installation_id))


def test_runtime_authority_rejects_installation_binding_on_noncleanup_action() -> None:
    arguments = cleanup_grant_arguments(INSTALLATION_ID)
    arguments["action"] = ContainerRuntimeAction.START
    with pytest.raises(
        HostHelperAuthorityError, match="installation binding is invalid"
    ):
        runtime_service().issue_grant(**arguments)


def test_cleanup_authority_rejects_malformed_persisted_payload() -> None:
    payload = {
        "schema_version": 1,
        "installation_id": INSTALLATION_ID,
        "plan_digest": "a" * 64,
        "recipe_content_sha256": "b" * 64,
    }
    # The required nullable cleanup field cannot disappear from stored authority.
    service = runtime_service(
        operation_kind="recipe.uninstall", operation_payload=payload
    )
    with pytest.raises(HostHelperAuthorityError, match="cleanup authority is invalid"):
        service.issue_grant(**cleanup_grant_arguments(INSTALLATION_ID))


def test_runtime_grant_request_enforces_cleanup_identity_and_null_policy() -> None:
    document: dict[str, object] = {}
    document.update(cleanup_grant_arguments(INSTALLATION_ID))
    document.pop("certificate_serial")
    document["action"] = "installation-cleanup"
    document["expires_in_seconds"] = 30
    assert (
        HostRuntimeGrantRequest.model_validate(document).installation_id
        == INSTALLATION_ID
    )
    for missing in ({}, {"installation_id": None}):
        incomplete = {
            key: value for key, value in document.items() if key != "installation_id"
        }
        with pytest.raises(ValidationError, match="installation binding"):
            HostRuntimeGrantRequest.model_validate(incomplete | missing)
    ordinary = document | {"action": "start"}
    with pytest.raises(ValidationError, match="installation binding"):
        HostRuntimeGrantRequest.model_validate(ordinary)
    ordinary.pop("installation_id")
    ordinary.update(
        start_plan_sha256="a" * 64,
        run_generation=1,
        runtime_run_id="50000000-0000-4000-8000-000000000005",
        runtime_target_id="50000000-0000-4000-8000-000000000005",
        runtime_installation_id="60000000-0000-4000-8000-000000000006",
    )
    omitted = HostRuntimeGrantRequest.model_validate(ordinary)
    explicit_null = HostRuntimeGrantRequest.model_validate(
        ordinary | {"installation_id": None}
    )
    assert canonical_message(omitted) == canonical_message(explicit_null)
    assert "installation_id" not in json.loads(canonical_message(explicit_null))
    with pytest.raises(ValidationError, match="plan binding"):
        HostRuntimeGrantRequest.model_validate(
            {
                key: value
                for key, value in ordinary.items()
                if key != "start_plan_sha256"
            }
        )
    with pytest.raises(ValidationError, match="plan binding"):
        HostRuntimeGrantRequest.model_validate(
            ordinary | {"stop_plan_sha256": "b" * 64}
        )


def reconciliation_identity() -> RecipeReconciliationIdentity:
    return RecipeReconciliationIdentity(
        schema_version=1,
        node_id="spk_" + "1" * 32,
        installation_id=INSTALLATION_ID,
        install_operation_id="a0000000-0000-4000-8000-00000000000a",
        install_operation_payload_sha256="a" * 64,
        plan_digest="b" * 64,
        recipe_revision_id="b0000000-0000-4000-8000-00000000000b",
        recipe_content_sha256="c" * 64,
        compiled_spec_canonical_sha256="d" * 64,
    )


def reconciliation_service(
    *, cancel_requested: bool = False, lease_seconds: int = 60, node_intent: int = 1
) -> HostRuntimeAuthorityService:
    identity = reconciliation_identity()
    service = runtime_service(
        operation_kind="recipe.reconcile",
        operation_payload=json.loads(canonical_message(identity)),
        cancel_requested=cancel_requested,
        lease_seconds=lease_seconds,
        node_intent=node_intent,
    )
    with service._sessions.begin() as session:
        operation = session.get(AgentOperation, "30000000-0000-4000-8000-000000000003")
        assert operation is not None
        operation.payload_digest = hashlib.sha256(
            canonical_message(identity)
        ).hexdigest()
    return service


class _ReconciliationGrantArguments(_CleanupGrantArguments):
    reconciliation_identity: RecipeReconciliationIdentity


def reconciliation_grant_arguments(
    identity: RecipeReconciliationIdentity,
) -> _ReconciliationGrantArguments:
    arguments = cleanup_grant_arguments(identity.installation_id)
    request = HostRuntimeRequest(
        schema_version=1,
        action="installation-cleanup",
        job_id=arguments["job_id"],
        operation_id=arguments["operation_id"],
        attempt=arguments["attempt"],
        fence=arguments["fence"],
        arguments=[],
        installation_id=identity.installation_id,
        reconciliation_identity=identity,
    )
    return {
        **arguments,
        "reconciliation_identity": identity,
        "request_sha256": hashlib.sha256(canonical_message(request)).hexdigest(),
    }


def test_reconciliation_grant_signs_the_exact_leased_cleanup_identity() -> None:
    identity = reconciliation_identity()
    arguments = reconciliation_grant_arguments(identity)
    grant = reconciliation_service().issue_grant(**arguments)
    operation = grant.claims.operation
    assert isinstance(operation, ExecuteContainerRuntimeRequestOperation)
    assert operation.reconciliation_identity == identity
    assert operation.request_sha256 == arguments["request_sha256"]
    issuer().public_key.verify(
        bytes.fromhex(grant.signature.value),
        host_helper_grant_signing_bytes(grant.claims),
    )


@pytest.mark.parametrize(
    "changed",
    [
        {"node_id": "spk_" + "2" * 32},
        {"installation_id": SECOND_INSTALLATION_ID},
        {"install_operation_id": OUTSIDE_INSTALLATION_ID},
        {"install_operation_payload_sha256": "e" * 64},
        {"plan_digest": "e" * 64},
        {"recipe_revision_id": OUTSIDE_INSTALLATION_ID},
        {"recipe_content_sha256": "e" * 64},
        {"compiled_spec_canonical_sha256": "e" * 64},
    ],
)
def test_reconciliation_grant_refuses_substituted_source_identity(changed) -> None:
    identity = reconciliation_identity().model_copy(update=changed)
    with pytest.raises(HostHelperAuthorityError):
        reconciliation_service().issue_grant(**reconciliation_grant_arguments(identity))


def test_reconciliation_grant_refuses_missing_identity_or_unbound_request() -> None:
    service = reconciliation_service()
    with pytest.raises(HostHelperAuthorityError):
        service.issue_grant(**cleanup_grant_arguments(INSTALLATION_ID))
    arguments = reconciliation_grant_arguments(reconciliation_identity())
    wrong_request = arguments.copy()
    wrong_request["request_sha256"] = "f" * 64
    with pytest.raises(HostHelperAuthorityError):
        service.issue_grant(**wrong_request)
    with service._sessions.begin() as session:
        operation = session.get(AgentOperation, "30000000-0000-4000-8000-000000000003")
        assert operation is not None
        operation.payload_digest = "f" * 64
    with pytest.raises(HostHelperAuthorityError):
        service.issue_grant(**arguments)


@pytest.mark.parametrize(
    "changes", [{"fence": OUTSIDE_INSTALLATION_ID}, {"attempt": 1}]
)
def test_reconciliation_grant_refuses_stale_attempt(changes) -> None:
    arguments = reconciliation_grant_arguments(reconciliation_identity())
    with pytest.raises(HostHelperAuthorityError):
        reconciliation_service().issue_grant(**(arguments | changes))


def test_reconciliation_grant_refuses_cancelled_or_expired_authority() -> None:
    arguments = reconciliation_grant_arguments(reconciliation_identity())
    for service in (
        reconciliation_service(cancel_requested=True),
        reconciliation_service(lease_seconds=-1),
    ):
        with pytest.raises(HostHelperAuthorityError):
            service.issue_grant(**arguments)


def test_ordinary_uninstall_cannot_supply_reconciliation_authority() -> None:
    payload = RecipeUninstallPayload(
        schema_version=1,
        installation_id=INSTALLATION_ID,
        plan_digest="a" * 64,
        recipe_content_sha256="b" * 64,
        cleanup_model_content_sha256=None,
    )
    service = runtime_service(
        operation_kind="recipe.uninstall",
        operation_payload=json.loads(canonical_message(payload)),
    )
    with pytest.raises(HostHelperAuthorityError):
        service.issue_grant(**reconciliation_grant_arguments(reconciliation_identity()))


def test_reconciliation_grant_refuses_superseded_node_intent() -> None:
    service = reconciliation_service(node_intent=2)
    with pytest.raises(HostHelperAuthorityError, match="stale"):
        service.issue_grant(**reconciliation_grant_arguments(reconciliation_identity()))
