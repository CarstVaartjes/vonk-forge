from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_agent_protocol import (
    AgentResultState,
    DistributionObject,
    LifecycleState,
    OperationMemberProgress,
    OperationProgress,
    ProgressPhase,
    canonical_message,
)
from vonk_agent_protocol.contracts import ArtifactDistributionPayload
from vonk_agent_protocol.host_helper import ExecuteContainerRuntimeRequestOperation
from vonk_control.agent_jobs import AgentJobService
from vonk_control.auth import TokenCodec
from vonk_control.bounded_json import require_mapping
from vonk_control.distribution import (
    DistributionService,
    MemoryObjectSource,
    RecipeBuildObjectSource,
    build_distribution_service_from_components,
)
from vonk_control.distribution_assignment import NodeDistributionAssignment
from vonk_control.distribution_executor import (
    CompositeDistributionPhaseExecutor,
    DurableDistributionPhaseExecutor,
    RuntimeImagePull,
    _phase_receipt,
)
from vonk_control.distribution_executor import children as executor_module
from vonk_control.distribution_executor import durable as durable_executor_module
from vonk_control.job_documents import (
    DistributionJobPayload,
    DistributionTransferProgress,
)
from vonk_control.model_cache import ModelCacheService
from vonk_control.model_cache_api import model_cache_operation_provider
from vonk_control.model_cache_contract import (
    ModelCacheCounters,
    ModelCacheDownloadResult,
)
from vonk_control.model_cache_progress import cache_progress, progress_document
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Base,
    CatalogDocument,
    CatalogDocumentRevision,
    Job,
    RecipeBuild,
)
from vonk_control.operation_api import merge_operation_providers
from vonk_control.operation_item_contract import OperationRow, operation_item
from vonk_control.run_switch_contract import (
    ArtifactStorageImpact,
    RunSwitchDistributionChildResult,
    RunSwitchModelDownloadResult,
    RunSwitchOperationResult,
    RunSwitchPhase,
    RunSwitchPlan,
    RunSwitchPreviewRequest,
    RunSwitchRuntimeImageResult,
    RunSwitchRuntimePlanResult,
    RunSwitchTargetTransferEvidenceResult,
    RunSwitchTargetTransferResult,
    RunSwitchVerifyResult,
    SparkGroup,
    SparkGroupNode,
)
from vonk_control.run_switch_operations import (
    RunSwitchOperationConflict,
    RunSwitchOperationService,
    _validate_artifact_execution,
)
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    RuntimeImageReceipt,
)
from vonk_control.strict_json import read_stored_model
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, document_sha256

from .runtime_image_fixtures import place_test_image
from .test_agent_api import NODE_A, NODE_B, agent_headers, agent_system  # noqa: F401
from .test_recipe_operations import NOW, setup_services
from .test_run_switch_operations import _runtime_receipt


def _receipt_json(receipt: BaseModel | None) -> dict[str, object]:
    assert receipt is not None
    return receipt.model_dump(mode="json", exclude_unset=True)


def _phase(**values: object) -> RunSwitchPhase:
    """Build a partial phase fixture typed as the canonical contract."""

    return RunSwitchPhase.model_construct(None, **values)


def _plan(**values: object) -> RunSwitchPlan:
    """Build a partial plan fixture typed as the canonical contract."""

    return RunSwitchPlan.model_construct(None, **values)


def _unused_executor_services(
    clock: Callable[[], datetime],
) -> tuple[sessionmaker[Session], AgentJobService, DistributionService]:
    """Typed durable services for executor paths that never reach them."""

    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    sessions = sessionmaker(engine, expire_on_commit=False)
    operations = AgentJobService(sessions, clock=clock)
    distribution = DistributionService(MemoryObjectSource(), sessions=sessions)
    return sessions, operations, distribution


def _call_argument(call: dict[str, object], key: str) -> Mapping[str, object]:
    return require_mapping(call[key], f"cache call {key}")


def _operation_progress(item: OperationRow) -> OperationProgress:
    progress = operation_item(item).progress
    assert progress is not None
    return progress


def _member_totals(member: OperationMemberProgress) -> tuple[str, int, int | None]:
    return (member.member_id, member.completed_bytes, member.total_bytes)


def test_phase_receipt_rejects_explicit_cross_phase_receipt() -> None:
    receipt = {
        "phase": "prepare",
        "subphase": "runtime-plan",
        "installation_id": str(uuid.uuid4()),
        "mapping_id": str(uuid.uuid4()),
        "install_plan_digest": "a" * 64,
        "compiled_plan_persisted": True,
    }
    phase = _phase(kind="transfer", subphase="target-copy")
    with pytest.raises(RuntimeError):
        _phase_receipt(RunSwitchRuntimePlanResult(**receipt), phase=phase)


def _target(node: str, *, image: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        node_id=node,
        state="ready",
        verified_sha256=("e" * 64 if image else "b" * 64),
        imported_image_digest=("sha256:" + "d" * 64 if image else None),
        verified_at=datetime.now(UTC),
    )


def test_complete_two_node_distribution_is_a_verified_skip() -> None:
    nodes = ("spk_" + "a" * 32, "spk_" + "b" * 32)
    preparation = SimpleNamespace(
        model=SimpleNamespace(
            artifact_set_sha256="b" * 64,
            artifact_set_bytes=0,
            targets=[_target(node) for node in nodes],
        ),
        runtime_image=SimpleNamespace(
            image_digest="sha256:" + "d" * 64,
            oci_layout_sha256="e" * 64,
            image_bytes=11,
            build_id=None,
            targets=[_target(node, image=True) for node in nodes],
        ),
    )
    plan = _plan(
        preparation=preparation,
        storage=SimpleNamespace(artifact_digests=["c" * 64]),
        image_digest="sha256:" + "d" * 64,
        build=SimpleNamespace(oci_layout_sha256="e" * 64, image_bytes=11),
        recipe_build_id=None,
    )
    phase = _phase(kind="transfer", node_ids=list(nodes), index=0)
    executor = DurableDistributionPhaseExecutor(
        *_unused_executor_services(lambda: datetime.now(UTC)),
        clock=lambda: datetime.now(UTC),
    )
    result = executor.execute(
        plan,
        phase,
        item_index=0,
        actor="test",
        request_key="00000000-0000-4000-8000-000000000001",
        progress=RunSwitchOperationResult(),
    )
    assert result.operation_id is None
    assert _receipt_json(result.result) == {
        "phase": "transfer",
        "subphase": "target-copy",
        "skipped": True,
        "verified": False,
        "verified_digests": ["c" * 64],
        "verified_build_id": None,
        "verified_image_digest": "sha256:" + "d" * 64,
        "verified_oci_layout_sha256": "e" * 64,
        "cached_nodes": list(nodes),
        "cached_target_totals": {node: 11 for node in nodes},
    }


def test_partial_child_replays_and_aggregates_cached_target(agent_system) -> None:  # noqa: F811
    _client, services, _tokens, clock = agent_system
    with services.sessions.begin() as session:
        for node_id in (NODE_A, NODE_B):
            node = session.get(AgentNode, node_id)
            assert node is not None
            node.workload_intent_ordinal = 7
    model = DistributionObject(
        name="weights/model.bin", sha256="a" * 64, bytes=10, kind="model"
    )
    config = DistributionObject(
        name="config/tokenizer.json", sha256="b" * 64, bytes=5, kind="model"
    )
    archive = RuntimeImagePull(
        image_digest="sha256:" + "e" * 64,
        config_digest="sha256:" + "9" * 64,
        address="c" * 64,
    )
    source = MemoryObjectSource({"a" * 64: b"x" * 10, "b" * 64: b"y" * 5})
    source.register_artifact_set("d" * 64, (model, config))
    source.register_runtime_image(archive.image_digest, archive.address)
    distribution = DistributionService(source, clock=clock, sessions=services.sessions)

    class StubExecutor(DurableDistributionPhaseExecutor):
        source_available = True

        def _model_objects(self, _plan, _progress):
            if not self.source_available:
                raise RuntimeError("NAS source is temporarily unavailable")
            return (model, config), "d" * 64, 15

        def _archive(self, **_kwargs):
            return archive

    executor = StubExecutor(
        services.sessions, services.operations, distribution, clock=clock
    )
    targets = [
        SimpleNamespace(
            node_id=NODE_A,
            state="preparing",
            verified_sha256=None,
            imported_image_digest=None,
            verified_at=None,
        ),
        SimpleNamespace(
            node_id=NODE_B,
            state="ready",
            verified_sha256="d" * 64,
            imported_image_digest=None,
            verified_at=datetime.now(UTC),
        ),
    ]
    image_targets = [
        SimpleNamespace(
            node_id=NODE_A,
            state="preparing",
            verified_sha256=None,
            imported_image_digest=None,
            verified_at=None,
        ),
        SimpleNamespace(
            node_id=NODE_B,
            state="ready",
            verified_sha256="c" * 64,
            imported_image_digest="sha256:" + "e" * 64,
            verified_at=datetime.now(UTC),
        ),
    ]
    preparation = SimpleNamespace(
        model=SimpleNamespace(
            artifact_set_sha256="d" * 64, artifact_set_bytes=15, targets=targets
        ),
        runtime_image=SimpleNamespace(
            image_digest="sha256:" + "e" * 64,
            oci_layout_sha256="c" * 64,
            image_bytes=11,
            build_id=None,
            targets=image_targets,
        ),
    )
    plan = _plan(
        preparation=preparation,
        storage=SimpleNamespace(artifact_digests=["a" * 64, "b" * 64]),
        image_digest=None,
        build=SimpleNamespace(oci_layout_sha256=None, image_bytes=None),
        recipe_build_id=None,
        recipe_revision_id=None,
        generated_at=clock.now,
        plan_digest="f" * 64,
        mapping=None,
    )
    phase = _phase(kind="transfer", node_ids=[NODE_A, NODE_B], index=0)
    build_progress = {"workload_intent_ordinal": 7}
    first = executor.execute(
        plan,
        phase,
        item_index=0,
        actor="test",
        request_key="00000000-0000-4000-8000-000000000001",
        progress=RunSwitchOperationResult.model_validate_json(
            canonical_message(build_progress)
        ),
    )
    assert first.operation_id is not None
    assert isinstance(first.result, RunSwitchTargetTransferResult)
    # Lose real child bookkeeping after dispatch. Reconstructing the owner's
    # deterministic identity must retain, rather than duplicate, the issued order.
    with services.sessions.begin() as session:
        original_order = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == first.operation_id
            )
        )
        assert original_order is not None
        order_id = original_order.id
        session.delete(session.get(Job, first.operation_id))
    recovered_child = executor.execute(
        plan,
        phase,
        item_index=0,
        actor="test",
        request_key="00000000-0000-4000-8000-000000000001",
        progress=RunSwitchOperationResult(
            workload_intent_ordinal=7, recovery_child_operation_id=first.operation_id
        ),
    )
    assert recovered_child.operation_id == first.operation_id
    with services.sessions() as session:
        orders = list(
            session.scalars(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == first.operation_id
                )
            )
        )
        assert [order.id for order in orders] == [order_id]
    pending = executor.get(first.operation_id)
    assert pending is not None
    assert isinstance(pending.result, RunSwitchDistributionChildResult)
    assert pending.result.progress.members[0].completed_bytes == 0
    assert isinstance(pending.result.progress.members[0].completed_bytes, int)
    with services.sessions.begin() as session:
        child = session.get(Job, first.operation_id)
        assert child is not None
        assert child.payload["workload_intent_ordinal"] == 7
        operation = next(iter(child.payload["assignments"].values()))
        assert operation["assignment_id"]
        stored = session.query(AgentOperation).filter_by(parent_job_id=child.id).one()
        assert stored.workload_intent_ordinal == 7
        # Exercise the actual privileged-action consumer contract with IDs
        # emitted by the transfer producer, rather than hand-written UUIDs.
        ExecuteContainerRuntimeRequestOperation(
            type="execute-container-runtime-request",
            action="image-pull",
            fence=str(uuid4()),
            request_sha256="a" * 64,
        )
        # ArtifactDistributionRequest is a plan reference. The agent fetches
        # the registered assignment through its authenticated distribution API.
        assert stored.payload == {"plan_digest": plan.plan_digest}
        assert (
            distribution.authorize(
                node_id=NODE_A, plan_digest=plan.plan_digest
            ).assignment_id
            == operation["assignment_id"]
        )
        stored.state = "succeeded"
        stored.current_attempt = 1
        session.add(
            AgentOperationAttempt(
                operation_id=stored.id,
                attempt=1,
                fence=str(uuid4()),
                lease_deadline=clock.now,
                agent_certificate_serial="serial-a",
                state="succeeded",
                # The heartbeat is intentionally stale and partial; terminal
                # evidence below is the authoritative aggregate.
                progress={
                    "phase": "copying",
                    "completed_bytes": 3,
                    "total_bytes": 15,
                    "total_bytes_known": True,
                },
                result={
                    "downloaded_bytes": 15,
                },
            )
        )
    # Worker aggregation progresses and publishes without any reader.
    with services.sessions.begin() as session:
        services.operations._aggregate_parent(session, first.operation_id)
    with services.sessions() as session:
        before_read = session.get(Job, first.operation_id).result
    view = executor.get(first.operation_id)
    assert view is not None
    executor.get(first.operation_id)
    with services.sessions() as session:
        assert session.get(Job, first.operation_id).result == before_read
    assert isinstance(view.result, RunSwitchDistributionChildResult)
    assert view.state == "succeeded"
    assert [member.node_id for member in view.result.members] == [
        NODE_A,
        NODE_B,
    ]
    assert view.result.progress.completed_bytes == 30
    assert view.result.progress.total_bytes == 30
    with services.sessions() as session:
        persisted = session.get(Job, first.operation_id)
        assert persisted is not None and persisted.result is not None
        persisted_receipt = read_stored_model(
            RunSwitchDistributionChildResult, persisted.result
        )
        assert persisted_receipt.progress.completed_bytes == 30
        assert persisted_receipt.progress.members[0].state == "succeeded"
    executor.source_available = False
    replay = executor.execute(
        plan,
        phase,
        item_index=0,
        actor="test",
        request_key="00000000-0000-4000-8000-000000000001",
        progress=RunSwitchOperationResult.model_validate_json(
            canonical_message(build_progress)
        ),
    )
    assert replay.operation_id == first.operation_id
    with pytest.raises(Exception) as _ending:
        executor.execute(
            plan,
            phase,
            item_index=0,
            actor="test",
            request_key="00000000-0000-4000-8000-000000000001",
            progress=RunSwitchOperationResult.model_validate_json(
                canonical_message({**build_progress, "workload_intent_ordinal": 8})
            ),
        )
    verify = executor.execute(
        plan,
        _phase(kind="verify", node_ids=[NODE_A, NODE_B], index=1),
        item_index=0,
        actor="test",
        request_key="00000000-0000-4000-8000-000000000001",
        progress=RunSwitchOperationResult(
            phase_results=[
                first.result,
                *[
                    RunSwitchTargetTransferEvidenceResult(
                        phase="transfer", subphase="target-copy", **item.model_dump()
                    )
                    for item in view.result.evidence
                ],
            ]
        ),
    )
    assert isinstance(verify.result, RunSwitchVerifyResult)
    assert verify.result.verified is True

    with services.sessions.begin() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == stored.id,
                AgentOperationAttempt.attempt == stored.current_attempt,
            )
        )
        assert attempt is not None and attempt.result is not None
        attempt.result = {**attempt.result, "downloaded_bytes": 14}
    mismatch = executor.get(first.operation_id)
    assert mismatch is not None
    assert isinstance(mismatch.result, RunSwitchDistributionChildResult)
    assert mismatch.state == LifecycleState.SUCCEEDED
    assert mismatch.result.members[0].state == LifecycleState.SUCCEEDED
    assert mismatch.result.members[0].total_bytes is None
    assert mismatch.result.progress.total_bytes is None
    with services.sessions.begin() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == stored.id,
                AgentOperationAttempt.attempt == stored.current_attempt,
            )
        )
        attempt.result = {"downloaded_bytes": 15}
    repaired = executor.get(first.operation_id)
    assert repaired is not None
    assert repaired.progress is not None and repaired.progress.total_bytes == 30
    with services.sessions.begin() as session:
        child = session.get(Job, first.operation_id)
        assert child is not None
        child.targets = [NODE_B]
    with pytest.raises(Exception) as _ending:
        executor.execute(
            plan,
            phase,
            item_index=0,
            actor="test",
            request_key="00000000-0000-4000-8000-000000000001",
            progress=RunSwitchOperationResult.model_validate_json(
                canonical_message(build_progress)
            ),
        )


@pytest.mark.parametrize("build_id", [None, "00000000-0000-4000-8000-000000000002"])
def test_stored_runtime_identity_reuses_content_without_producer_history(
    tmp_path: Path, build_id: str | None
) -> None:
    """A provenance query fails here: the database has no producer tables."""
    clock = lambda: datetime.now(UTC)
    sessions, operations, _distribution = _unused_executor_services(clock)
    source = RecipeBuildObjectSource(sessions, tmp_path)
    storage = FilesystemRuntimeImageStorage(tmp_path)
    address = "b" * 64
    image_digest = "sha256:" + "a" * 64
    place_test_image(storage, address, 11)
    executor = DurableDistributionPhaseExecutor(
        sessions, operations, DistributionService(source), clock=clock
    )
    image = executor._archive(
        image_digest=image_digest,
        layout_digest=address,
        image_bytes=11,
        build_id=build_id,
    )
    assert image.image_digest == image_digest
    assert image.address == address
    assert image.config_digest.startswith("sha256:")
    # Missing content is still a miss; restoring it immediately permits reuse.
    (storage.layout.root / "blobs" / "sha256" / address).unlink()
    with pytest.raises(Exception) as _ending:
        executor._archive(
            image_digest=image_digest,
            layout_digest=address,
            image_bytes=11,
            build_id=build_id,
        )
    place_test_image(storage, address, 11)
    assert (
        executor._archive(
            image_digest=image_digest,
            layout_digest=address,
            image_bytes=11,
            build_id=build_id,
        )
        == image
    )


def test_build_verify_handoff_reuses_content_from_another_producer() -> None:
    node_id = NODE_A
    build_id = str(uuid4())
    artifact_digest = "c" * 64
    image_digest = "sha256:" + "a" * 64
    layout_digest = "b" * 64
    assignment = NodeDistributionAssignment.parse(
        {
            "assignment_id": str(uuid4()),
            "plan_digest": "d" * 64,
            "generation": 1,
            "node_id": node_id,
            "expires_at": datetime.now(UTC).isoformat(),
            "model_artifact_set_sha256": artifact_digest,
            "objects": [
                {
                    "name": "configuration.json",
                    "sha256": artifact_digest,
                    "bytes": 7,
                    "kind": "model",
                },
            ],
            "oci_image_digest": image_digest,
            "oci_image_config_digest": "sha256:" + "9" * 64,
            "oci_archive_sha256": layout_digest,
        }
    )
    plan = _plan(
        preparation=SimpleNamespace(
            model=SimpleNamespace(
                artifact_set_sha256=artifact_digest,
                artifact_set_bytes=7,
                targets=[
                    SimpleNamespace(
                        node_id=node_id,
                        state="ready",
                        verified_sha256=artifact_digest,
                        verified_at=datetime.now(UTC),
                        imported_image_digest=None,
                    )
                ],
            ),
            runtime_image=SimpleNamespace(
                build_id=build_id,
                image_digest=image_digest,
                oci_layout_sha256=layout_digest,
                image_bytes=11,
                targets=[
                    SimpleNamespace(
                        node_id=node_id,
                        state="ready",
                        verified_sha256=layout_digest,
                        verified_at=datetime.now(UTC),
                        imported_image_digest=image_digest,
                    )
                ],
            ),
        ),
        storage=SimpleNamespace(artifact_digests=[artifact_digest]),
        image_digest=None,
        build=SimpleNamespace(oci_layout_sha256=layout_digest, image_bytes=11),
        recipe_build_id=build_id,
        plan_digest="d" * 64,
    )
    progress = RunSwitchOperationResult.model_validate_json(
        canonical_message(
            {
                "phase_results": [
                    {
                        "phase": "transfer",
                        "subphase": "target-copy",
                        "assignments": {node_id: assignment.to_mapping()},
                    },
                    {
                        "phase": "transfer",
                        "subphase": "target-copy",
                        "node_id": node_id,
                        "downloaded_bytes": 7,
                    },
                ]
            }
        )
    )
    executor = CompositeDistributionPhaseExecutor(
        None,
        None,
        None,
        model_cache=None,
        clock=lambda: datetime.now(UTC),
    )
    result = _receipt_json(executor._verify_evidence(plan, progress, (node_id,), ()))
    assert result["verified_image_digest"] == image_digest
    assert result["verified_oci_layout_sha256"] == layout_digest
    _validate_artifact_execution(plan, _phase(kind="verify"), result)

    # Persist a preparation receipt from a different producer, then feed it
    # through the real distribution identity and final verification consumer.
    foreign_id = str(uuid4())
    foreign = RunSwitchRuntimeImageResult(
        phase="prepare",
        subphase="runtime-image",
        image_digest=image_digest,
        oci_layout_sha256=layout_digest,
        image_bytes=11,
        build_id=foreign_id,
        runtime_image=RuntimeImageReceipt.model_validate_json(
            canonical_message(
                _runtime_receipt(
                    plan,
                    image=image_digest,
                    layout=layout_digest,
                    size=11,
                    build_id=foreign_id,
                )
            )
        ),
    )
    progress.phase_results.append(foreign)
    restored = RunSwitchOperationResult.model_validate_json(progress.model_dump_json())
    verified = executor._verify_evidence(plan, restored, (node_id,), ())
    assert verified.verified_image_digest == image_digest
    assert verified.verified_oci_layout_sha256 == layout_digest
    _validate_artifact_execution(
        plan,
        _phase(kind="verify"),
        verified.model_copy(update={"verified_build_id": foreign_id}),
    )
    _validate_artifact_execution(
        plan,
        _phase(kind="verify"),
        verified.model_copy(update={"verified_build_id": None}),
    )

    cached = DurableDistributionPhaseExecutor(
        *_unused_executor_services(lambda: datetime.now(UTC)),
        clock=lambda: datetime.now(UTC),
    ).execute(
        plan,
        _phase(kind="verify", node_ids=[node_id], index=0),
        item_index=0,
        actor="test",
        request_key="00000000-0000-4000-8000-000000000001",
        progress=RunSwitchOperationResult(),
    )
    assert isinstance(cached.result, RunSwitchVerifyResult)
    assert cached.result.skipped is True
    assert cached.result.verified_image_digest == image_digest
    assert cached.result.verified_oci_layout_sha256 == layout_digest
    _validate_artifact_execution(plan, _phase(kind="verify"), cached.result)


@pytest.mark.usefixtures("damaged_json_rows")
def test_partial_child_failure_is_projected_after_aggregation(agent_system) -> None:  # noqa: F811
    _client, services, _tokens, clock = agent_system
    source = MemoryObjectSource()
    distribution = DistributionService(source, clock=clock, sessions=services.sessions)
    executor = DurableDistributionPhaseExecutor(
        services.sessions, services.operations, distribution, clock=clock
    )
    # The failure path is intentionally checked at the durable projection
    # boundary; no fabricated verification receipt can make it succeed.
    with services.sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.workload_intent_ordinal = 1
        child = Job(
            id=str(uuid4()),
            request_id=str(uuid4()),
            kind="artifact-distribution",
            state="queued",
            actor="test",
            authority_revision="f" * 64,
            targets=[NODE_A],
            payload_digest="0" * 64,
            payload=DistributionJobPayload(
                plan_digest="f" * 64,
                workload_intent_ordinal=1,
                phase="transfer",
                progress=DistributionTransferProgress(
                    phase="transfer",
                    completed_bytes=0,
                    total_bytes=26,
                    total_bytes_known=True,
                    members=[],
                ),
                cached_nodes=[],
                target_order=[NODE_A],
                target_totals={NODE_A: 26},
                assignments={},
            ).model_dump(mode="json", exclude_none=True),
            result=None,
            created_at=clock.now,
            updated_at=clock.now,
        )
        session.add(child)
        session.flush()
        services.operations.enqueue_in_session(
            session,
            child.id,
            NODE_A,
            "artifact.distribution.v1",
            "f" * 64,
            ArtifactDistributionPayload(plan_digest="f" * 64).model_dump(mode="json"),
            operation_id=str(uuid4()),
        )
        operation = (
            session.query(AgentOperation).filter_by(parent_job_id=child.id).one()
        )
        operation.state = "failed"
        operation.current_attempt = 1
        session.add(
            AgentOperationAttempt(
                operation_id=operation.id,
                attempt=1,
                fence=str(uuid4()),
                lease_deadline=clock.now,
                agent_certificate_serial="serial-a",
                state="failed",
                progress={
                    "phase": "copying",
                    "completed_bytes": 2,
                    "total_bytes": 26,
                    "total_bytes_known": True,
                },
                result={"reason": "digest mismatch"},
            )
        )
        child_id = child.id
    with services.sessions.begin() as session:
        services.operations._aggregate_parent(session, child_id)
    view = executor.get(child_id)
    assert view is not None
    assert isinstance(view.result, RunSwitchDistributionChildResult)
    assert view.state == "failed"
    assert view.result.members[0].error == "digest mismatch"
    assert view.result.reason == "digest mismatch"


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.usefixtures("damaged_json_rows")
def test_abandon_closes_only_a_parked_distribution_child(
    agent_system,  # noqa: F811
    running: bool,
) -> None:
    _client, services, _tokens, clock = agent_system
    distribution = DistributionService(
        MemoryObjectSource(), clock=clock, sessions=services.sessions
    )
    executor = DurableDistributionPhaseExecutor(
        services.sessions, services.operations, distribution, clock=clock
    )
    with services.sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.workload_intent_ordinal = 1
        child = Job(
            id=str(uuid4()),
            request_id=str(uuid4()),
            kind="artifact-distribution",
            state="queued",
            actor="test",
            authority_revision="f" * 64,
            targets=[NODE_A],
            payload_digest="0" * 64,
            payload=DistributionJobPayload(
                plan_digest="f" * 64,
                workload_intent_ordinal=1,
                phase="transfer",
                progress=DistributionTransferProgress(
                    phase="transfer",
                    completed_bytes=0,
                    total_bytes=26,
                    total_bytes_known=True,
                    members=[],
                ),
                cached_nodes=[],
                target_order=[NODE_A],
                target_totals={NODE_A: 26},
                assignments={},
            ).model_dump(mode="json", exclude_none=True),
            result=None,
            created_at=clock.now,
            updated_at=clock.now,
        )
        session.add(child)
        session.flush()
        services.operations.enqueue_in_session(
            session,
            child.id,
            NODE_A,
            "artifact.distribution.v1",
            "f" * 64,
            ArtifactDistributionPayload(plan_digest="f" * 64).model_dump(mode="json"),
            operation_id=str(uuid4()),
        )
        operation = (
            session.query(AgentOperation).filter_by(parent_job_id=child.id).one()
        )
        operation.state = "running" if running else "waiting-for-operator"
        operation.current_attempt = 1
        child.state = "waiting-for-operator"
        child_id, operation_id = child.id, operation.id

    with services.sessions.begin() as session:
        closed = executor.abandon(session, child_id, clock.now, reason="cancelled")
    assert closed is not running
    with services.sessions() as session:
        assert session.get(AgentOperation, operation_id).state == (
            "running" if running else "cancelled"
        )
        assert session.get(Job, child_id).state == (
            "waiting-for-operator" if running else "cancelled"
        )
    if not running:
        cancelled = executor.get(child_id)
        assert cancelled is not None
        assert cancelled.state == LifecycleState.CANCELLED
    with services.sessions.begin() as session:
        assert not executor.abandon(session, str(uuid4()), clock.now, reason="x")


@pytest.mark.parametrize(
    "failure_kind",
    ["temporary-dependency", "integrity-failure"],
)
@pytest.mark.usefixtures("damaged_json_rows")
def test_member_failure_kind_and_diagnostic_survive_aggregation(
    agent_system,  # noqa: F811
    failure_kind: str,
) -> None:
    """The agent's typed failure decides the parent's retry and stays visible."""

    _client, services, _tokens, clock = agent_system
    executor = DurableDistributionPhaseExecutor(
        services.sessions,
        services.operations,
        DistributionService(
            MemoryObjectSource(), clock=clock, sessions=services.sessions
        ),
        clock=clock,
    )
    with services.sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.workload_intent_ordinal = 1
        child = Job(
            id=str(uuid4()),
            request_id=str(uuid4()),
            kind="artifact-distribution",
            state="queued",
            actor="test",
            authority_revision="f" * 64,
            targets=[NODE_A],
            payload_digest="0" * 64,
            payload=DistributionJobPayload(
                plan_digest="f" * 64,
                workload_intent_ordinal=1,
                phase="transfer",
                progress=DistributionTransferProgress(
                    phase="transfer",
                    completed_bytes=0,
                    total_bytes=26,
                    total_bytes_known=True,
                    members=[],
                ),
                cached_nodes=[],
                target_order=[NODE_A],
                target_totals={NODE_A: 26},
                assignments={},
            ).model_dump(mode="json", exclude_none=True),
            result=None,
            created_at=clock.now,
            updated_at=clock.now,
        )
        session.add(child)
        session.flush()
        services.operations.enqueue_in_session(
            session,
            child.id,
            NODE_A,
            "artifact.distribution.v1",
            "f" * 64,
            ArtifactDistributionPayload(plan_digest="f" * 64).model_dump(mode="json"),
            operation_id=str(uuid4()),
        )
        operation = (
            session.query(AgentOperation).filter_by(parent_job_id=child.id).one()
        )
        operation.state = "failed"
        operation.current_attempt = 1
        session.add(
            AgentOperationAttempt(
                operation_id=operation.id,
                attempt=1,
                fence=str(uuid4()),
                lease_deadline=clock.now,
                agent_certificate_serial="serial-a",
                state="failed",
                result={
                    "reason": "Controller distribution could not be verified and retained",
                    "failure_kind": failure_kind,
                    "error_code": "artifact_distribution_failed",
                    "stage": "artifact-distribution",
                    "diagnostic": "http_status=404 error_code=controller.http_404",
                },
            )
        )
        child_id = child.id
    with services.sessions.begin() as session:
        services.operations._aggregate_parent(session, child_id)
    view = executor.get(child_id)
    assert view is not None
    assert isinstance(view.result, RunSwitchDistributionChildResult)
    assert view.result.members[0].diagnostic
    with services.sessions() as session:
        persisted = session.get(Job, child_id).result
    for _ in range(3):
        executor.get(child_id)
    with services.sessions() as session:
        assert session.get(Job, child_id).result == persisted
    # Repair the original operation's evidence. Its successful effect is not
    # demoted by absent totals and reads never rewrite the worker's verdict.
    with services.sessions.begin() as session:
        operation = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == child_id)
        )
        operation.state = LifecycleState.SUCCEEDED
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation.id,
                AgentOperationAttempt.attempt == 1,
            )
        )
        attempt.state = LifecycleState.SUCCEEDED
        attempt.result = {"downloaded_bytes": 26}
        child = session.get(Job, child_id)
        child.state = LifecycleState.RUNNING
        services.operations._aggregate_parent(session, child_id)
    repaired = executor.get(child_id)
    assert repaired is not None
    assert repaired.state == LifecycleState.SUCCEEDED
    measured = repaired.progress
    assert measured is not None
    assert measured.total_bytes == 26
    assert measured.completed_bytes == 26


@pytest.mark.parametrize("image_prepared", [True, False])
def test_model_download_is_a_durable_cache_child_with_exact_pins(
    image_prepared: bool,
) -> None:
    calls: list[dict[str, object]] = []
    cache_view = SimpleNamespace(
        id=str(uuid4()),
        state="queued",
        artifact_set_sha256="d" * 64,
        progress=progress_document(
            cache_progress(
                ModelCacheCounters.model_validate(
                    {
                        "phase": "downloading",
                        "completed_artifacts": 0,
                        "total_artifacts": 1,
                        "downloaded_bytes": 3,
                        "expected_bytes": 15,
                    }
                ),
                previous=None,
                now=datetime.now(UTC),
            )
        ),
        last_error=None,
        result=None,
    )

    class Cache:
        def download_preview(self, **kwargs):
            calls.append({"preview": kwargs})
            return {
                "artifact_set_sha256": "d" * 64,
                "plan_digest": "e" * 64,
                "expected_bytes": 15,
                "artifact_count": 2,
                "new_bytes": 12,
                "already_cached_bytes": 3,
                "warnings": [],
                "blockers": [],
                "_manifest": SimpleNamespace(
                    digest="d" * 64,
                    recipe_revision_sha256="9" * 64,
                ),
            }

        def start_download(self, **kwargs):
            calls.append({"start": kwargs})
            return cache_view

        def manifest_for_artifact_set(self, _digest):
            raise AssertionError("first download has no persisted cache set")

        def get_operation(self, operation_id):
            if operation_id != cache_view.id:
                from vonk_control.model_cache import ModelCacheNotFound

                raise ModelCacheNotFound("model_cache.operation_missing", "missing")
            return cache_view

    plan = _plan(
        preparation=SimpleNamespace(
            model=SimpleNamespace(
                artifact_set_sha256="d" * 64,
                model_content_sha256="a" * 64,
                recipe_revision_sha256="b" * 64,
                artifact_count=2,
                artifact_set_bytes=15,
            )
        ),
        recipe_revision_id=str(uuid4()),
    )
    if not image_prepared:
        preparation = plan.preparation
        assert preparation is not None
        model = preparation.model
        plan.model_content_sha256 = model.model_content_sha256
        plan.recipe_content_sha256 = model.recipe_revision_sha256
        plan.storage = ArtifactStorageImpact.model_construct(
            artifact_set_sha256=model.artifact_set_sha256,
            artifact_set_bytes=model.artifact_set_bytes,
            artifact_digests=["1" * 64, "2" * 64],
        )
        plan.preparation = None
    phase = _phase(kind="transfer", subphase="model-download", index=2)
    executor = CompositeDistributionPhaseExecutor(
        None,
        None,
        None,
        model_cache=Cache(),
        clock=lambda: datetime.now(UTC),
    )
    result = executor.execute(
        plan,
        phase,
        item_index=0,
        actor="operator",
        request_key="00000000-0000-4000-8000-000000000001",
        progress=RunSwitchOperationResult(),
    )
    assert result.operation_id == cache_view.id
    assert _call_argument(calls[0], "preview")["artifact_set_sha256"] == "d" * 64
    assert _call_argument(calls[1], "start")["plan_digest"] == "e" * 64
    replay = executor.execute(
        plan,
        phase,
        item_index=0,
        actor="operator",
        request_key="00000000-0000-4000-8000-000000000001",
        progress=RunSwitchOperationResult(),
    )
    assert replay.operation_id == cache_view.id
    assert (
        _call_argument(calls[1], "start")["request_key"]
        == _call_argument(calls[3], "start")["request_key"]
    )
    projected = executor.get(cache_view.id)
    assert projected is not None
    assert projected.state == "queued"
    assert projected.progress is not None
    assert projected.progress.completed_bytes == 3
    cache_view.state = "running"
    running = executor.get(cache_view.id)
    assert running is not None
    assert running.state == LifecycleState.RUNNING
    cache_view.state = "cancelled"
    cancelled = executor.get(cache_view.id)
    assert cancelled is not None
    assert cancelled.state == LifecycleState.CANCELLED
    if image_prepared:
        plan.storage = ArtifactStorageImpact.model_construct(missing_nas_bytes=12)
    else:
        plan.storage.missing_nas_bytes = 12
    receipt = {
        "artifact_set_sha256": "d" * 64,
        "coverage": "complete",
        "downloaded_bytes": 12,
        "total_bytes": 12,
    }
    _validate_artifact_execution(plan, phase, receipt)
    with pytest.raises(RunSwitchOperationConflict):
        _validate_artifact_execution(
            plan, phase, {**receipt, "artifact_set_sha256": "f" * 64}
        )
    with pytest.raises(RunSwitchOperationConflict):
        _validate_artifact_execution(plan, phase, {**receipt, "downloaded_bytes": 11})


def test_model_download_uses_real_cache_manifest_and_reports_complete_coverage(
    tmp_path: Path,
) -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = ModelCacheService(
        sessions,
        tmp_path / "nas-cache",
        reserve_bytes=0,
        fixture_sources=True,
    )
    model_content_sha256 = "a" * 64
    recipe_digest = "b" * 64
    payload = (b"model-payload-" * 100_000) + b"!"
    source = tmp_path / "weights.source"
    source.write_bytes(payload)
    artifact = {
        "id": "weights",
        "path": "weights.bin",
        "kind": "file",
        "source": source.as_uri(),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "download_bytes": len(payload),
        "roles": ["model"],
        "model_content_sha256": model_content_sha256,
    }
    seed_preview = service.download_preview(
        model_content_sha256=model_content_sha256,
        recipe_revision_sha256=recipe_digest,
        artifacts=[artifact],
    )
    seeded = service.start_download(
        actor="test",
        request_key="00000000-0000-4000-8000-000000000011",
        plan_digest=str(seed_preview["plan_digest"]),
        model_content_sha256=model_content_sha256,
        recipe_revision_sha256=recipe_digest,
        artifacts=[artifact],
        interrupt_after_bytes=1024,
    )
    assert seeded.state == LifecycleState.BACKOFF
    artifact_set = seeded.artifact_set_sha256
    assert artifact_set
    manifest = service.manifest_for_artifact_set(artifact_set)
    plan = _plan(
        preparation=SimpleNamespace(
            model=SimpleNamespace(
                artifact_set_sha256=artifact_set,
                model_content_sha256=model_content_sha256,
                recipe_revision_sha256=recipe_digest,
                artifact_count=1,
                artifact_set_bytes=len(payload),
            )
        ),
        recipe_revision_id=None,
    )
    phase = _phase(kind="transfer", subphase="model-download", index=2)
    executor = CompositeDistributionPhaseExecutor(
        None,
        None,
        None,
        model_cache=service,
        clock=lambda: datetime.now(UTC),
    )
    first = executor.execute(
        plan,
        phase,
        item_index=0,
        actor="operator",
        request_key="00000000-0000-4000-8000-000000000012",
        progress=RunSwitchOperationResult(),
    )
    assert first.operation_id
    service.run_pending(limit=2)
    completed = executor.get(first.operation_id)
    assert completed is not None
    assert isinstance(completed.result, RunSwitchModelDownloadResult)
    assert completed.state == "succeeded"
    assert completed.result.artifact_set_sha256 == manifest.digest == artifact_set
    assert completed.result.coverage == "complete"
    assert completed.result.evidence is not None
    assert completed.result.evidence.coverage == "complete"
    # The set was completed by the earlier resumable operation before this
    # child ran, so this operation received no bytes despite its planned
    # remaining range.
    assert completed.result.progress.completed_bytes == 0
    assert completed.result.progress.total_bytes is not None
    assert completed.result.progress.total_bytes < len(payload)


@pytest.mark.usefixtures("damaged_json_rows")
def test_production_composite_uncached_cache_then_two_target_distribution(
    agent_system,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Exercise the production source pair and durable child handoff.

    The model source is a fixture-backed cache because catalog/network inputs
    are unavailable in this unit lane.  OCI bytes still cross the production
    RecipeBuildObjectSource boundary, and the agent HTTP boundary is
    exercised with the enrolled certificate identities.
    """
    client, services, _tokens, clock = agent_system
    cache = ModelCacheService(
        services.sessions,
        tmp_path / "model-cache",
        reserve_bytes=0,
        fixture_sources=True,
    )
    model = ModelDefinition.model_validate(
        json.loads(
            files("vonk_forge_contracts")
            .joinpath("examples", "model-definition.json")
            .read_text(encoding="utf-8")
        )
    )
    model_content_sha256 = document_sha256(model.model_dump(mode="json"))
    recipe_document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-source-build.json")
        .read_text(encoding="utf-8")
    )
    recipe_document["identity"]["slug"] = "production-composite"
    recipe_document["models"][0]["model"]["content_sha256"] = model_content_sha256
    recipe = RecipeDefinition.model_validate(recipe_document)
    recipe_document = recipe.model_dump(mode="json")
    recipe_digest = document_sha256(recipe.model_dump(mode="json"))
    model_payload = b"model weights"
    auxiliary_payload = b"tokenizer auxiliary"
    model_source = tmp_path / "weights.source"
    auxiliary_source = tmp_path / "tokenizer.source"
    model_source.write_bytes(model_payload)
    auxiliary_source.write_bytes(auxiliary_payload)
    artifacts = [
        {
            "id": "weights",
            "path": "weights.bin",
            "kind": "file",
            "source": model_source.as_uri(),
            "sha256": hashlib.sha256(model_payload).hexdigest(),
            "download_bytes": len(model_payload),
            "roles": ["model"],
            "model_content_sha256": model_content_sha256,
        },
        {
            "id": "tokenizer",
            "path": "tokenizer.json",
            "kind": "file",
            "source": auxiliary_source.as_uri(),
            "sha256": hashlib.sha256(auxiliary_payload).hexdigest(),
            "download_bytes": len(auxiliary_payload),
            "roles": ["auxiliary"],
            "model_content_sha256": model_content_sha256,
        },
    ]
    preview = cache.download_preview(
        model_content_sha256=model_content_sha256,
        recipe_revision_sha256=recipe_digest,
        artifacts=artifacts,
    )
    parent_request = "00000000-0000-4000-8000-000000000021"
    cache_request = str(
        uuid.uuid5(
            uuid.UUID(parent_request),
            f"model-download:0:{preview['artifact_set_sha256']}",
        )
    )
    # Seed the trusted fixture manifest and durable cache child.  The
    # production composite below replays this exact child by its deterministic
    # request key and performs the normal worker transition.
    seeded = cache.start_download(
        actor="operator",
        request_key=cache_request,
        plan_digest=str(preview["plan_digest"]),
        model_content_sha256=model_content_sha256,
        recipe_revision_sha256=recipe_digest,
        artifacts=artifacts,
    )
    assert seeded.state == "queued"
    artifact_set = str(seeded.artifact_set_sha256)
    archive_payload = b"prebuilt arm64 oci archive"
    archive_digest = hashlib.sha256(archive_payload).hexdigest()
    image_digest = "sha256:" + archive_digest
    recipe_id = str(uuid.uuid4())
    revision_id = str(uuid.uuid4())
    model_id = str(uuid.uuid4())
    model_revision_id = str(uuid.uuid4())
    build_id = str(uuid.uuid4())
    now = clock.now
    with services.sessions.begin() as session:
        session.add_all(
            [
                CatalogDocument(
                    id=recipe_id,
                    kind="recipe",
                    publisher=recipe.identity.publisher,
                    slug=recipe.identity.slug,
                    title=recipe.metadata.title,
                    created_by="test",
                    created_at=now,
                    updated_at=now,
                ),
                CatalogDocument(
                    id=model_id,
                    kind="model",
                    publisher=model.identity.publisher,
                    slug=model.identity.slug,
                    title=model.identity.model.title,
                    created_by="test",
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                CatalogDocumentRevision(
                    id=revision_id,
                    document_id=recipe_id,
                    kind="recipe",
                    publisher=recipe.identity.publisher,
                    slug=recipe.identity.slug,
                    revision_number=1,
                    schema_version=2,
                    state="active",
                    document=recipe_document,
                    content_digest=recipe_digest,
                    projected={},
                    created_by="test",
                    created_at=now,
                ),
                CatalogDocumentRevision(
                    id=model_revision_id,
                    document_id=model_id,
                    kind="model",
                    publisher=model.identity.publisher,
                    slug=model.identity.slug,
                    revision_number=1,
                    schema_version=2,
                    state="active",
                    document=model.model_dump(mode="json"),
                    content_digest=model_content_sha256,
                    projected={},
                    created_by="test",
                    created_at=now,
                ),
            ]
        )
        session.add(
            RecipeBuild(
                id=build_id,
                recipe_revision_id=revision_id,
                builder_node_id=NODE_A,
                source_bundle_sha256="c" * 64,
                build_input_sha256="e" * 64,
                state="succeeded",
                policy_report={"fixture": "prebuilt-oci"},
                plan={"architecture": "linux-arm64"},
                image_digest=image_digest,
                oci_layout_sha256=archive_digest,
                image_bytes=len(archive_payload),
                error=None,
                created_at=now,
                updated_at=now,
            )
        )
    storage = FilesystemRuntimeImageStorage(services.artifact_root)
    place_test_image(storage, archive_digest, len(archive_payload))
    distribution = build_distribution_service_from_components(
        cache,
        services.sessions,
        services.artifact_root,
        clock=clock,
    )
    object.__setattr__(services, "distribution", distribution)
    nodes = (NODE_A, NODE_B)
    preparation = SimpleNamespace(
        model=SimpleNamespace(
            artifact_set_sha256=artifact_set,
            model_content_sha256=model_content_sha256,
            recipe_revision_sha256=recipe_digest,
            artifact_count=2,
            artifact_set_bytes=len(model_payload) + len(auxiliary_payload),
            targets=[
                SimpleNamespace(
                    node_id=node,
                    state="pending",
                    verified_sha256=None,
                    verified_at=None,
                )
                for node in nodes
            ],
        ),
        runtime_image=SimpleNamespace(
            image_digest=image_digest,
            oci_layout_sha256=archive_digest,
            image_bytes=len(archive_payload),
            targets=[
                SimpleNamespace(
                    node_id=node,
                    state="pending",
                    verified_sha256=None,
                    imported_image_digest=None,
                    verified_at=None,
                )
                for node in nodes
            ],
        ),
    )
    plan = _plan(
        preparation=preparation,
        storage=SimpleNamespace(
            artifact_digests=[item["sha256"] for item in artifacts],
            missing_nas_bytes=0,
        ),
        image_digest=image_digest,
        build=SimpleNamespace(
            oci_layout_sha256=archive_digest,
            image_bytes=len(archive_payload),
            build_input_sha256="e" * 64,
            build_id=build_id,
        ),
        recipe_build_id=build_id,
        recipe_revision_id=None,
        generated_at=now,
        plan_digest="f" * 64,
        mapping=SimpleNamespace(mapping_generation=1),
        spark_group=SimpleNamespace(nodes=[]),
    )
    executor = CompositeDistributionPhaseExecutor(
        services.sessions,
        services.operations,
        distribution,
        model_cache=cache,
        clock=clock,
    )
    model_phase = _phase(kind="transfer", subphase="model-download", index=0)
    model_child = executor.execute(
        plan,
        model_phase,
        item_index=0,
        actor="operator",
        request_key=parent_request,
        progress=RunSwitchOperationResult(),
    )
    assert model_child.operation_id == seeded.id
    assert cache.run_pending() == 1
    assert model_child.operation_id is not None
    model_result = executor.get(model_child.operation_id)
    assert model_result is not None
    assert isinstance(model_result.result, RunSwitchModelDownloadResult)
    assert model_result.state == "succeeded"
    assert model_result.result.coverage == "complete"
    assert model_result.result.artifact_set_sha256 == artifact_set
    assert model_result.result.progress.completed_bytes == (
        len(model_payload) + len(auxiliary_payload)
    )

    copy_phase = _phase(
        kind="transfer", subphase="target-copy", index=1, node_ids=list(nodes)
    )
    with services.sessions.begin() as session:
        for node_id in nodes:
            node = session.get(AgentNode, node_id)
            assert node is not None
            node.workload_intent_ordinal = 1
    # Preparation may have been produced by another recipe/build. Its
    # serialized content receipt still feeds the real durable distribution.
    foreign_id = str(uuid4())
    foreign_image = RunSwitchRuntimeImageResult(
        phase="prepare",
        subphase="runtime-image",
        runtime_image=RuntimeImageReceipt.model_validate_json(
            canonical_message(
                _runtime_receipt(
                    plan,
                    image=image_digest,
                    layout=archive_digest,
                    size=len(archive_payload),
                    build_id=foreign_id,
                )
            )
        ),
        image_digest=image_digest,
        oci_layout_sha256=archive_digest,
        image_bytes=len(archive_payload),
        build_id=foreign_id,
    )
    copy_progress = RunSwitchOperationResult.model_validate_json(
        RunSwitchOperationResult(
            workload_intent_ordinal=1, phase_results=[foreign_image]
        ).model_dump_json()
    )
    # Both foreign provenance and absent producer history leave managed content usable.
    with services.sessions.begin() as session:
        historical_build = session.get(RecipeBuild, build_id)
        assert historical_build is not None
        session.delete(historical_build)
    copy_child = executor.execute(
        plan,
        copy_phase,
        item_index=0,
        actor="operator",
        request_key=parent_request,
        progress=copy_progress,
    )
    assert copy_child.operation_id
    with services.sessions.begin() as session:
        child = session.get(Job, copy_child.operation_id)
        assert child is not None
        assignments = child.payload["assignments"]
        assert set(assignments) == set(nodes)
        for node in nodes:
            assignment = assignments[node]
            assert assignment["model_artifact_set_sha256"] == artifact_set
            assert {item["sha256"] for item in assignment["objects"]} == {
                item["sha256"] for item in artifacts
            }
            assert assignment["oci_image_digest"] == image_digest
            assert assignment["oci_image_config_digest"].startswith("sha256:")
            operation = session.scalar(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == child.id,
                    AgentOperation.node_id == node,
                )
            )
            assert operation is not None
            target_bytes = sum(item["bytes"] for item in assignment["objects"])
            operation.state = "succeeded"
            operation.current_attempt = 1
            session.add(
                AgentOperationAttempt(
                    operation_id=operation.id,
                    attempt=1,
                    fence=str(uuid.uuid4()),
                    lease_deadline=now,
                    agent_certificate_serial="serial-a"
                    if node == NODE_A
                    else "serial-b",
                    state="succeeded",
                    # Keep the heartbeat partial to prove terminal
                    # downloaded_bytes is the authoritative total.
                    progress={
                        "phase": "copying",
                        "completed_bytes": 1,
                        "total_bytes": target_bytes,
                        "total_bytes_known": True,
                    },
                    result={
                        "downloaded_bytes": target_bytes,
                    },
                )
            )
    manifest_response = client.get(
        "/agent/distribution/manifests/" + plan.plan_digest,
        headers=agent_headers(NODE_A, "serial-a"),
    )
    assert manifest_response.status_code == 200
    assert {item["sha256"] for item in manifest_response.json()["objects"]} == {
        item["sha256"] for item in artifacts
    }
    for node, serial in ((NODE_A, "serial-a"), (NODE_B, "serial-b")):
        response = client.get(
            "/agent/distribution/manifests/" + plan.plan_digest,
            headers=agent_headers(node, serial),
        )
        assert response.status_code == 200
        for item in response.json()["objects"]:
            payload_response = client.get(
                "/agent/distribution/objects/"
                + item["sha256"]
                + "?plan_digest="
                + plan.plan_digest,
                headers=agent_headers(node, serial),
            )
            assert payload_response.status_code == 200
            assert payload_response.headers["x-vonk-file"].endswith(item["sha256"])
    with services.sessions.begin() as session:
        services.operations._aggregate_parent(session, copy_child.operation_id)
    view = executor.get(copy_child.operation_id)
    assert view is not None
    assert isinstance(view.result, RunSwitchDistributionChildResult)
    assert view.state == "succeeded"
    assert {member.node_id for member in view.result.members} == set(nodes)
    assert len(view.result.evidence) == 2
    copy_progress.phase_results.extend(
        RunSwitchTargetTransferEvidenceResult.model_validate_json(
            canonical_message(
                {
                    **item.model_dump(mode="json"),
                    "phase": "transfer",
                    "subphase": "target-copy",
                }
            )
        )
        for item in view.result.evidence
    )
    verified = executor.execute(
        plan,
        _phase(kind="verify", node_ids=list(nodes), index=2),
        item_index=0,
        actor="operator",
        request_key=parent_request,
        progress=RunSwitchOperationResult.model_validate_json(
            copy_progress.model_dump_json()
        ),
    )
    assert isinstance(verified.result, RunSwitchVerifyResult)
    assert verified.result.verified_image_digest == image_digest
    assert verified.result.verified_oci_layout_sha256 == archive_digest
    assert {item.node_id for item in verified.result.evidence} == set(nodes)
    _validate_artifact_execution(plan, _phase(kind="verify"), verified.result)

    replay = executor.execute(
        plan,
        copy_phase,
        item_index=0,
        actor="operator",
        request_key=parent_request,
        progress=RunSwitchOperationResult.model_validate_json(
            canonical_message({"workload_intent_ordinal": 1})
        ),
    )
    assert replay.operation_id == copy_child.operation_id

    cached_model = executor.execute(
        plan,
        model_phase,
        item_index=0,
        actor="operator",
        request_key=parent_request,
        progress=RunSwitchOperationResult(),
    )
    cached_id = cached_model.operation_id
    assert cached_id is not None and cached_id == seeded.id
    assert cache.run_pending() == 0
    reused = executor.get(cached_id)
    assert reused is not None
    assert reused.state == LifecycleState.SUCCEEDED
    assert (
        cast(RunSwitchModelDownloadResult, reused.result).artifact_set_sha256
        == artifact_set
    )

    copy_view = executor.get(copy_child.operation_id)

    assert copy_view is not None
    assert isinstance(copy_view.result, RunSwitchDistributionChildResult)
    member_progress = copy_view.result.progress.members
    (tmp_path / "run-switch-plan").mkdir()
    (
        plan_sessions,
        _plan_lifecycle,
        _plan_queue,
        _plan_mapping_id,
        _plan_build_id,
        plan_nodes,
    ) = setup_services(tmp_path / "run-switch-plan", nodes=2)
    with plan_sessions() as session:
        plan_revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
        assert plan_revision is not None
        plan_recipe = RecipeDefinition.model_validate(plan_revision.document)
        plan_model_digest = plan_recipe.models[0].model.content_sha256
    operation_plan = RunSwitchOperationService(
        plan_sessions, clock=lambda: NOW
    ).preview(
        RunSwitchPreviewRequest(
            model_content_sha256=plan_model_digest,
            recipe_revision_id=plan_revision.id,
            spark_group=SparkGroup(
                nodes=[
                    SparkGroupNode(
                        node_id=plan_nodes[0],
                        rank=0,
                        role="entrypoint",
                        endpoint_owner=True,
                    ),
                    SparkGroupNode(node_id=plan_nodes[1], rank=1, role="worker"),
                ]
            ),
            alias="qwen",
        ),
        actor="operator",
    )
    operation_plan_payload = operation_plan.model_dump(mode="json")
    persisted_members = [
        {**member.model_dump(mode="json"), "node_id": plan_nodes[index]}
        for index, member in enumerate(member_progress)
    ]
    run_id = str(uuid.uuid4())
    with services.sessions.begin() as session:
        session.add(
            Job(
                id=run_id,
                request_id=str(uuid.uuid4()),
                kind="recipe.run-switch.v2",
                state="running",
                actor="operator",
                authority_revision=operation_plan.plan_digest,
                targets=list(plan_nodes),
                payload_digest=operation_plan.plan_digest,
                payload={
                    "action": operation_plan.action,
                    "plan_digest": operation_plan.plan_digest,
                    "plan": operation_plan_payload,
                },
                result={
                    "phase": "transfer",
                    "phase_index": 0,
                    "completed_bytes": 90,
                    "total_bytes": 90,
                    "total_bytes_known": True,
                    "members": persisted_members,
                },
                created_at=now,
                updated_at=now,
            )
        )
    run_provider = RunSwitchOperationService(
        services.sessions,
        clock=clock,
    ).activity_provider()
    family_item = run_provider.get_operation(run_id)
    family_progress = _operation_progress(family_item)
    assert family_progress.completed_bytes == 90
    expected_target_bytes = view.result.members[0].total_bytes
    assert {_member_totals(item) for item in family_progress.members} == {
        (plan_nodes[0], expected_target_bytes, expected_target_bytes),
        (plan_nodes[1], expected_target_bytes, expected_target_bytes),
    }
    restarted_provider = RunSwitchOperationService(
        services.sessions,
        clock=clock,
    ).activity_provider()
    assert (
        _operation_progress(restarted_provider.get_operation(run_id)) == family_progress
    )

    cursors = TokenCodec(b"p" * 32).cursor_codec()
    merged = merge_operation_providers(
        (run_provider, model_cache_operation_provider(cache, cursors)),
        cursor=None,
        limit=100,
        state=None,
        node_id=None,
        cursors=cursors,
    )
    merged_run = next(
        operation_item(item).model_dump(mode="json")
        for item in merged.items
        if operation_item(item).id == run_id
    )
    merged_progress = _operation_progress(merged_run)
    assert merged_progress.completed_bytes == 90
    assert {_member_totals(item) for item in merged_progress.members} == {
        (plan_nodes[0], expected_target_bytes, expected_target_bytes),
        (plan_nodes[1], expected_target_bytes, expected_target_bytes),
    }

    unknown_id = str(uuid.uuid4())
    with services.sessions.begin() as session:
        session.add(
            Job(
                id=unknown_id,
                request_id=str(uuid.uuid4()),
                kind="recipe.run-switch.v2",
                state="running",
                actor="operator",
                authority_revision=operation_plan.plan_digest,
                targets=[plan_nodes[0]],
                payload_digest=operation_plan.plan_digest,
                payload={
                    "action": operation_plan.action,
                    "plan_digest": operation_plan.plan_digest,
                    "plan": operation_plan_payload,
                },
                result={
                    "phase": "transfer",
                    "phase_index": 0,
                    "completed_bytes": 0,
                    "total_bytes": None,
                    "total_bytes_known": False,
                    "members": [
                        {
                            "node_id": plan_nodes[0],
                            "phase": "transfer",
                            "state": "running",
                            "completed_bytes": 0,
                            "total_bytes": None,
                        }
                    ],
                },
                created_at=now,
                updated_at=now,
            )
        )
    unknown_item = run_provider.get_operation(unknown_id)
    unknown_progress = _operation_progress(unknown_item)
    assert unknown_progress.total_bytes is None
    assert unknown_progress.total_bytes_known is False
    unknown_members = unknown_progress.members
    assert unknown_members[0].total_bytes is None


def test_runtime_image_phase_hands_preparation_to_background_executor() -> None:

    entered = threading.Event()
    release = threading.Event()
    executor = cast(Any, object.__new__(CompositeDistributionPhaseExecutor))
    executor._async_runtime_image_preparation = True
    executor._runtime_image_pool = ThreadPoolExecutor(max_workers=1)
    executor._runtime_image_futures = {}
    executor._runtime_image_lock = threading.RLock()
    executor._runtime_image_inflight = set()
    executor._runtime_image_parallelism = 4
    executor.reconcile_background = lambda: False
    executor._clock = lambda: datetime(2026, 9, 30, 12, tzinfo=UTC)

    def prepare(
        _plan,
        _phase,
        *,
        item_index,
        actor,
        request_key,
        progress,
    ) -> None:
        assert item_index == 0
        assert actor == "operator"
        assert request_key
        assert progress == RunSwitchOperationResult()
        entered.set()
        assert release.wait(5)

    executor._prepare_runtime_image = prepare
    phase = _phase(index=2, kind="prepare", subphase="runtime-image")
    request_key = str(uuid4())
    try:
        result = executor.execute(
            _plan(),
            phase,
            item_index=0,
            actor="operator",
            request_key=request_key,
            progress=RunSwitchOperationResult(),
        )
        assert result.waiting
        assert result.operation_id is None
        assert "background" in (result.status_reason or "")
        assert entered.wait(1)
        observed = executor.execute(
            _plan(),
            phase,
            item_index=0,
            actor="operator",
            request_key=request_key,
            progress=RunSwitchOperationResult(),
        )
        assert observed.waiting
        assert "still running" in (observed.status_reason or "")
        assert "started 2026-09-30T12:00:00+00:00" in (observed.status_reason or "")
    finally:
        release.set()
        executor.close()


def test_zero_byte_model_download_uses_the_cache_admission() -> None:
    """A complete content decision still goes through its owning admission."""
    calls = []
    view = SimpleNamespace(
        id=str(uuid4()),
        state=LifecycleState.SUCCEEDED,
        artifact_set_sha256="d" * 64,
        last_error=None,
        result=ModelCacheDownloadResult(
            schema_version=2, artifact_set_sha256="d" * 64, coverage="complete"
        ),
        progress=progress_document(
            cache_progress(
                ModelCacheCounters(
                    phase="completed",
                    completed_artifacts=2,
                    total_artifacts=2,
                    downloaded_bytes=0,
                    expected_bytes=0,
                ),
                previous=None,
                now=datetime.now(UTC),
            )
        ),
    )

    class Cache:
        def download_preview(self, **kwargs):
            return {
                "artifact_set_sha256": "d" * 64,
                "plan_digest": "e" * 64,
                "expected_bytes": 15,
                "artifact_count": 2,
                "new_bytes": 0,
                "already_cached_bytes": 15,
                "blockers": [],
                "warnings": [],
            }

        def start_download(self, **kwargs):
            calls.append(kwargs)
            return view

    plan = _plan(
        preparation=SimpleNamespace(
            model=SimpleNamespace(
                artifact_set_sha256="d" * 64,
                model_content_sha256="a" * 64,
                artifact_count=2,
                artifact_set_bytes=15,
            )
        ),
        recipe_revision_id=str(uuid4()),
    )
    executor = CompositeDistributionPhaseExecutor(
        None, None, None, model_cache=Cache(), clock=lambda: datetime.now(UTC)
    )
    result = executor.execute(
        plan,
        _phase(kind="transfer", subphase="model-download", index=0),
        item_index=0,
        actor="operator",
        request_key=str(uuid4()),
        progress=RunSwitchOperationResult(),
    )
    assert result.operation_id == view.id
    assert len(calls) == 1
    completed_receipt = cast(RunSwitchModelDownloadResult, result.result)
    assert completed_receipt.evidence == view.result
    assert completed_receipt.progress.completed_bytes == 0


@pytest.mark.parametrize(
    "initial", [True, False], ids=["create-child", "refresh-child"]
)
def test_child_persistence_retains_in_place_default_members(
    agent_system,  # noqa: F811
    monkeypatch,
    initial,
):
    """Both child writers must retain mutations nested in their typed receipt."""
    writer_module = executor_module if initial else durable_executor_module
    original = writer_module._child_receipt
    member = OperationMemberProgress(member_id=NODE_A, phase=ProgressPhase.TRANSFER)
    writes = []

    def mutated_receipt(*args, **kwargs):
        receipt = original(*args, **kwargs)
        operation = OperationProgress(phase=ProgressPhase.TRANSFER)
        operation.members.append(member)
        assert "members" not in operation.model_fields_set
        receipt.progress.operation = operation
        return receipt

    def check_written_receipt(session, _context):
        rows = session.new if initial else session.dirty
        for row in rows:
            if isinstance(row, Job) and row.result is not None:
                stored = (
                    session.connection()
                    .execute(select(Job.result).where(Job.id == row.id))
                    .scalar_one()
                )
                receipt = read_stored_model(RunSwitchDistributionChildResult, stored)
                if receipt.progress.operation is not None:
                    writes.append(receipt)
                    assert receipt.progress.operation.members == [member]

    monkeypatch.setattr(writer_module, "_child_receipt", mutated_receipt)
    sessions = agent_system[1].sessions
    event.listen(sessions, "after_flush", check_written_receipt)
    try:
        test_partial_child_replays_and_aggregates_cached_target(agent_system)
    finally:
        event.remove(sessions, "after_flush", check_written_receipt)
    assert writes, "the selected persistence writer must be exercised"


def test_expired_distribution_fence_cannot_revive_or_block_fresh_transfer(agent_system):  # noqa: F811
    """Issued transfer expiry releases dispatch while retaining late evidence."""
    from datetime import timedelta

    from vonk_agent_protocol import AgentResult
    from vonk_agent_protocol.contracts import ArtifactDistributionResult

    from .runtime_identity_support import claim_agent
    from .test_distribution import _assignment

    _client, services, _tokens, clock = agent_system
    source = MemoryObjectSource()
    model = source.put(b"model payload")
    config = source.put(b"config!")
    archive = source.put(b"oci archive")
    assignment = _assignment(NODE_A, model, config, archive).model_copy(
        update={"expires_at": clock.now + timedelta(hours=1)}
    )
    source.register_artifact_set(
        assignment.model_artifact_set_sha256, assignment.objects
    )
    source.register_runtime_image(assignment.oci_image_digest, archive)
    distribution = DistributionService(source, clock=clock, sessions=services.sessions)
    executor = DurableDistributionPhaseExecutor(
        services.sessions, services.operations, distribution, clock=clock
    )
    with services.sessions.begin() as session:
        session.get(AgentNode, NODE_A).workload_intent_ordinal = 1
    plan = _plan(plan_digest=assignment.plan_digest)
    phase = _phase(index=0, kind="transfer", node_ids=[NODE_A])
    old = executor._ensure_child(
        plan,
        phase,
        actor="test",
        request_key=str(uuid4()),
        cached=(),
        assignments={NODE_A: assignment},
        target_order=(NODE_A,),
        workload_intent_ordinal=1,
        target_bytes=20,
    )
    claim = claim_agent(services.operations, NODE_A, "serial-a")
    assert claim is not None
    with services.sessions.begin() as session:
        executor.expire(session, old, clock.now, plan_digest=plan.plan_digest)
    with services.sessions() as session:
        old_order = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == old)
        )
        assert old_order.state == LifecycleState.CANCELLED
        old_state = old_order.state
    late = AgentResult(
        fence=claim.fence,
        state=AgentResultState.SUCCEEDED,
        result=ArtifactDistributionResult(downloaded_bytes=20),
    )
    services.operations.record_late_result(late)
    with services.sessions() as session:
        old_order = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == old)
        )
        assert old_order.state == old_state
    fresh = executor._ensure_child(
        plan,
        phase,
        actor="test",
        request_key=str(uuid4()),
        cached=(),
        assignments={NODE_A: assignment},
        target_order=(NODE_A,),
        workload_intent_ordinal=1,
        target_bytes=20,
    )
    assert fresh != old
    next_claim = claim_agent(services.operations, NODE_A, "serial-a")
    assert next_claim is not None and next_claim.fence != claim.fence
    services.operations.record_result(
        AgentResult(
            fence=next_claim.fence,
            state=AgentResultState.SUCCEEDED,
            result=ArtifactDistributionResult(downloaded_bytes=20),
        )
    )
    completed = executor.get(fresh)
    assert completed is not None
    assert completed.state == LifecycleState.SUCCEEDED
    measurement = completed.progress
    assert measurement is not None
    assert measurement.completed_bytes == 20
