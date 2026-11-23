"""Artifact jobs: retention companions."""

from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from threading import Event

import pytest
from sqlalchemy import select
from vonk_agent_protocol.contracts import AgentResult, canonical_message
from vonk_agent_protocol.lifecycle_vocabulary import AgentResultState, LifecycleState
from vonk_agent_protocol.recipe_jobs import (
    RecipeJobFile,
    RecipeJobRunRequest,
    RecipeJobRunResult,
)
from vonk_agent_protocol.recipe_jobs import (
    manifest_sha256 as recipe_job_manifest_sha256,
)
from vonk_agent_protocol.wire_model import OperationProgress
from vonk_control.agent_jobs.contracts import (
    controller_result_consumer,
)
from vonk_control.agent_jobs.service import AgentJobService
from vonk_control.artifact_jobs import states as ajs
from vonk_control.artifact_jobs.blob_store import (
    ArtifactBlobStore,
)
from vonk_control.artifact_jobs.service import ArtifactJobService
from vonk_control.models.execution import (
    ArtifactJob,
    ArtifactJobBlob,
    ArtifactJobFile,
    RecipeRun,
)
from vonk_control.models.jobs import Job
from vonk_control.models.operations import AgentOperation
from vonk_control.recipe_operations.execution_contract import parse_stored_run_plan

from .artifact_jobs_support import (
    MutableClock,
    _configure_artifact_recipe,
    artifact_create_request,
    cancellation_result,
    create_artifact_job,
    running_artifact_service,
    submitted_artifact_job,
)
from .recipe_operations_support import NOW, installed_recipe, setup_services
from .runtime_identity_support import claim_agent


def test_terminal_job_retention_removes_only_unreferenced_cas_bytes(tmp_path) -> None:
    sessions, _operations, _queue, service, run_id, _node_id = running_artifact_service(
        tmp_path
    )
    content = b"png"
    digest = hashlib.sha256(content).hexdigest()
    job = create_artifact_job(
        service,
        run_id,
        interface="image-job",
        parameters={"prompt": "fox", "seed": 0},
        inputs=[
            {
                "slot": "input",
                "name": "input.png",
                "size_bytes": len(content),
                "sha256": digest,
            }
        ],
        output_limits={
            "max_files": 1,
            "max_file_bytes": 1024,
            "max_total_bytes": 4096,
            "allowed_media_types": ["image/png"],
        },
        timeout_seconds=3600,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000111",
    )
    service.put_input(
        job.id,
        name="input.png",
        media_type="image/png",
        expected_sha256=digest,
        content=content,
    )
    service.cancel(
        job.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000112",
        reason="test complete",
    )
    with sessions.begin() as session:
        stored = session.get(ArtifactJob, job.id)
        assert stored is not None
        stored.completed_at = stored.updated_at = NOW - timedelta(hours=23, minutes=59)
    assert service.reconcile_storage().expired_jobs == 0
    with sessions.begin() as session:
        stored = session.get(ArtifactJob, job.id)
        assert stored is not None
        stored.completed_at = stored.updated_at = NOW - timedelta(hours=24, seconds=1)
    report = service.reconcile_storage()
    assert report.remaining_work
    assert report.expired_jobs == 0
    with sessions() as session:
        assert session.get(ArtifactJob, job.id) is not None
    # Grace is also unfinished filesystem work: its authorization must survive.
    store = ArtifactBlobStore(tmp_path / "artifact-blobs")
    fresh_content = b"fresh admitted while reclamation waits"
    fresh = store.put_bytes(
        hashlib.sha256(fresh_content).hexdigest(),
        fresh_content,
        maximum_bytes=len(fresh_content),
    )
    assert fresh.path.read_bytes() == fresh_content
    os.utime(tmp_path / "artifact-blobs" / digest[:2] / digest, (0, 0))
    report = service.reconcile_storage()
    assert report.expired_jobs == 1
    assert fresh.path.read_bytes() == fresh_content
    with sessions() as session:
        assert session.get(ArtifactJob, job.id) is None
        assert session.get(ArtifactJobBlob, digest) is None


def test_reconcile_never_deletes_blobs_on_an_empty_reference_scan(tmp_path) -> None:
    """A restore that lost reference rows must not authorize a mass delete.

    With no ArtifactJobFile rows the sweep has no evidence about who referenced
    the stored bytes, so it must reclaim nothing rather than treat every blob as
    an orphan.
    """

    sessions, _recipe_operations, _queue, service, _run_id, _node_id = (
        running_artifact_service(tmp_path)
    )
    content = b"surviving bytes"
    digest = hashlib.sha256(content).hexdigest()
    root = tmp_path / "artifact-blobs"
    stored = ArtifactBlobStore(root).put_bytes(
        digest, content, maximum_bytes=len(content)
    )
    with sessions.begin() as session:
        session.add(
            ArtifactJobBlob(
                sha256=digest,
                size_bytes=len(content),
                storage_key=stored.storage_key,
                created_at=NOW,
            )
        )

    report = service.reconcile_storage()

    assert report.expired_jobs == 0
    assert report.removed_blob_records == 0
    assert stored.path.is_file()
    with sessions() as session:
        assert session.get(ArtifactJobBlob, digest) is not None


@pytest.mark.parametrize("contended", (False, True))
def test_reconcile_reclaims_only_the_expired_jobs_own_blobs(
    tmp_path, contended
) -> None:
    """Partial reference loss must not unlink bytes a surviving row still owns."""

    sessions, _recipe_operations, _queue, service, run_id, _node_id = (
        running_artifact_service(tmp_path)
    )
    expired_content = b"png"
    expired_digest = hashlib.sha256(expired_content).hexdigest()
    unproven_content = b"unproven bytes"
    unproven_digest = hashlib.sha256(unproven_content).hexdigest()
    job = create_artifact_job(
        service,
        **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000150"),
    )
    service.put_input(
        job.id,
        name="input.png",
        media_type="image/png",
        expected_sha256=expired_digest,
        content=expired_content,
    )
    root = tmp_path / "artifact-blobs"
    seeded = ArtifactBlobStore(root).put_bytes(
        unproven_digest, unproven_content, maximum_bytes=len(unproven_content)
    )
    with sessions.begin() as session:
        session.add(
            ArtifactJobBlob(
                sha256=unproven_digest,
                size_bytes=len(unproven_content),
                storage_key=seeded.storage_key,
                created_at=NOW,
            )
        )
    live_request = artifact_create_request(
        run_id, "00000000-0000-4000-8000-000000000152"
    )
    live_request["inputs"][0]["size_bytes"] = len(unproven_content)
    live_request["inputs"][0]["sha256"] = unproven_digest
    live = create_artifact_job(service, **live_request)
    service.put_input(
        live.id,
        name="input.png",
        media_type="image/png",
        expected_sha256=unproven_digest,
        content=unproven_content,
    )
    service.cancel(
        job.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000151",
        reason="test complete",
    )
    with sessions.begin() as session:
        stored = session.get(ArtifactJob, job.id)
        assert stored is not None
        stored.completed_at = stored.updated_at = NOW - timedelta(days=8)
    # Remove the orphan grace so an unguarded store would unlink immediately.
    expired_path = next(root.glob(f"*/{expired_digest}"))
    os.utime(expired_path, (0, 0))
    os.utime(seeded.path, (0, 0))

    if contended:
        import fcntl

        with (root / ".quota.lock").open("a+b") as quota:
            fcntl.flock(quota.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(Exception):  # noqa: B017 -- ending witness; durable evidence asserted below
                service.reconcile_storage()
            with sessions() as session:
                assert session.get(ArtifactJob, job.id) is not None
                assert session.get(ArtifactJobBlob, expired_digest) is not None
                assert (
                    session.scalar(
                        select(ArtifactJobFile).where(
                            ArtifactJobFile.artifact_job_id == job.id
                        )
                    )
                    is not None
                )
        # Recreate the consumer to prove evidence survives an executor restart.
        service = ArtifactJobService(
            sessions,
            recipe_operations=_recipe_operations,
            blob_store=ArtifactBlobStore(root),
            clock=lambda: NOW,
        )
    report = service.reconcile_storage()

    assert report.expired_jobs == 1
    assert report.removed_blob_records == 1
    assert not expired_path.exists()
    assert seeded.path.is_file()
    with sessions() as session:
        assert session.get(ArtifactJobBlob, expired_digest) is None
        assert session.get(ArtifactJobBlob, unproven_digest) is not None
    capacity = len(unproven_content) + len(expired_content)
    store = ArtifactBlobStore(root, max_stored_bytes=capacity)
    fresh = store.put_bytes(expired_digest, expired_content, maximum_bytes=capacity)
    assert fresh.path.read_bytes() == expired_content
    assert seeded.path.read_bytes() == unproven_content
    fresh_job = create_artifact_job(
        service,
        **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000153"),
    )
    assert fresh_job.id != job.id


def test_gc_cannot_delete_old_dedup_blob_during_database_attachment(
    tmp_path, monkeypatch
) -> None:
    sessions, recipe_operations, _queue, service, run_id, _node_id = (
        running_artifact_service(tmp_path)
    )
    content = b"png"
    digest = hashlib.sha256(content).hexdigest()
    job = create_artifact_job(
        service,
        **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000129"),
    )
    root = tmp_path / "artifact-blobs"
    seeded = ArtifactBlobStore(root).put_bytes(
        digest, content, maximum_bytes=len(content)
    )
    os.utime(seeded.path, (0, 0))

    attach_entered = Event()
    release_attach = Event()
    original_attach = service._attach_input

    def paused_attach(*args, **kwargs):
        attach_entered.set()
        assert release_attach.wait(timeout=30)
        return original_attach(*args, **kwargs)

    monkeypatch.setattr(service, "_attach_input", paused_attach)
    gc_store = ArtifactBlobStore(root)
    gc_started = Event()
    original_fence = gc_store.reference_reconciliation

    @contextmanager
    def observed_fence():
        gc_started.set()
        with original_fence():
            yield

    monkeypatch.setattr(gc_store, "reference_reconciliation", observed_fence)
    gc_service = ArtifactJobService(
        sessions,
        recipe_operations=recipe_operations,
        blob_store=gc_store,
        clock=lambda: NOW,
    )

    with ThreadPoolExecutor(max_workers=2) as workers:
        upload = workers.submit(
            service.put_input,
            job.id,
            name="input.png",
            media_type="image/png",
            expected_sha256=digest,
            content=content,
        )
        assert attach_entered.wait(timeout=30)
        gc = workers.submit(gc_service.reconcile_storage)
        assert gc_started.wait(timeout=30)
        assert gc.done() is False
        assert seeded.path.is_file()
        release_attach.set()
        assert upload.result(timeout=30).id == job.id
        report = gc.result(timeout=30)

    assert report.removed_orphan_blobs == 0
    assert seeded.path.is_file()
    with sessions() as session:
        attached = session.scalar(select(ArtifactJob).where(ArtifactJob.id == job.id))
        assert attached is not None
        assert service.get(job.id).input_files[0].sha256 == digest


@pytest.mark.parametrize(
    "damage", ["missing-files", "invalid-file", "wrong-total", "wrong-digest"]
)
@pytest.mark.usefixtures("damaged_json_rows")
def test_artifact_input_manifest_damage_is_readable_and_fresh_work_is_admitted(
    tmp_path, damage
):
    sessions, _operations, _queue, service, run_id, _node_id = running_artifact_service(
        tmp_path
    )
    created = create_artifact_job(
        service,
        **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000151"),
    )
    assert service.get(created.id).input_declarations == created.input_declarations
    with sessions.begin() as session:
        row = session.get(ArtifactJob, created.id)
        assert row is not None
        manifest = dict(row.input_manifest)
        if damage == "missing-files":
            del manifest["files"]
        elif damage == "invalid-file":
            files = manifest["files"]
            assert isinstance(files, list)
            manifest["files"] = [*files, "invalid"]
        elif damage == "wrong-total":
            total_bytes = manifest["total_bytes"]
            assert isinstance(total_bytes, int)
            manifest["total_bytes"] = total_bytes + 1
        elif damage == "wrong-digest":
            row.input_manifest_sha256 = "f" * 64
        else:
            manifest["undeclared"] = None
        row.input_manifest = manifest
    assert service.get(created.id).input_declarations is None
    ended = service.finalize(created.id)
    assert ended.state in ajs.ENDED and ended.operation_id is None
    fresh = submitted_artifact_job(service, run_id, request_suffix=390)
    assert fresh.operation_id is not None


@pytest.mark.usefixtures("damaged_json_rows")
def test_artifact_input_manifest_is_rebuilt_from_the_uploaded_inputs(tmp_path):
    sessions, _operations, _queue, service, run_id, _node_id = running_artifact_service(
        tmp_path
    )
    created = create_artifact_job(
        service,
        **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000154"),
    )
    content = b"png"
    service.put_input(
        created.id,
        name="input.png",
        media_type="image/png",
        expected_sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )
    with sessions.begin() as session:
        row = session.get(ArtifactJob, created.id)
        assert row is not None
        row.input_manifest = {"schema_version": 1}
    # The uploaded rows reproduce the digest the job was created under, so the
    # declared inputs are rebuilt from them and the job stays readable.
    assert service.get(created.id).input_declarations == created.input_declarations
    assert service.finalize(created.id).preparation == "ready"


@pytest.mark.parametrize("evidence", [[], "invalid", {"elapsed_milliseconds": "1"}])
@pytest.mark.usefixtures("damaged_json_rows")
def test_artifact_cancel_completes_over_corrupt_evidence(tmp_path, evidence):
    sessions, _operations, _queue, service, run_id, _node_id = running_artifact_service(
        tmp_path
    )
    created = create_artifact_job(
        service,
        **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000152"),
    )
    with sessions.begin() as session:
        row = session.get(ArtifactJob, created.id)
        assert row is not None
        row.result_evidence = evidence
    # Damaged result evidence is retired as unknown: a cancel still completes.
    cancelled = service.cancel(
        created.id, actor="operator", request_id="cancel-corrupt", reason="stop"
    )
    assert cancelled.state == "cancelled"


def test_artifact_cancel_preserves_declared_evidence_and_meaningful_values(
    tmp_path,
):
    sessions, _operations, _queue, service, run_id, _node_id = running_artifact_service(
        tmp_path
    )
    created = create_artifact_job(
        service,
        **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000153"),
    )
    evidence = {
        "elapsed_milliseconds": 0,
        "peak_memory_bytes": 0,
        "recoverable": False,
    }
    with sessions.begin() as session:
        stored = session.get(ArtifactJob, created.id)
        assert stored is not None
        stored.result_evidence = evidence
    cancelled = service.cancel(
        created.id, actor="operator", request_id="cancel-evidence", reason="stop"
    )
    assert cancelled.result_evidence is not None
    for key, value in evidence.items():
        assert getattr(cancelled.result_evidence, key) == value
    assert service.get(created.id).result_evidence == cancelled.result_evidence


@pytest.mark.postgres
@pytest.mark.parametrize("expires", (False, True))
@pytest.mark.parametrize(
    ("lease_lapses", "reconcile"), ((False, False), (True, False), (True, True))
)
def test_undelivered_job_outputs_resume_without_repeating_the_process(
    tmp_path, postgres_engine, expires, lease_lapses, reconcile
):
    """Ending on 413, losing the receipt, or resetting retention fails this flow."""
    from vonk_agent_protocol.agent_words import FailureStage
    from vonk_agent_protocol.lifecycle_vocabulary import OutcomeKind, WaitReason
    from vonk_agent_protocol.outcome import OutcomeEvidence, OutcomeUnknown
    from vonk_agent_protocol.recipe_jobs import (
        RecipeJobEvidence,
        RecipeJobOutputManifest,
    )

    sessions, recipe_operations, _, service, run_id, node_id = running_artifact_service(
        tmp_path, engine=postgres_engine
    )
    clock = MutableClock(NOW)
    jobs = AgentJobService(sessions, clock=clock)

    def consume(session, operation, attempt, message):
        service.consume_agent_result(session, operation, attempt, message)
        recipe_operations.consume_agent_result(session, operation, attempt, message)

    jobs.set_result_consumer(consume)
    recipe_operations._agent_jobs = jobs
    job = submitted_artifact_job(service, run_id, request_suffix=842)
    first = claim_agent(jobs, node_id, "serial-0")
    assert first is not None
    content = b"image"
    output = RecipeJobFile(
        name="output.png",
        media_type="image/png",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    files = (output,)
    receipt = RecipeJobRunResult(
        job_id=job.id,
        run_id=run_id,
        exit_code=0,
        output_manifest=RecipeJobOutputManifest(
            schema_version=1,
            total_bytes=len(content),
            files=files,
            manifest_sha256=recipe_job_manifest_sha256(files),
        ),
        evidence=RecipeJobEvidence(elapsed_milliseconds=100, peak_memory_bytes=None),
    )

    def undelivered(claim):
        return AgentResult(
            fence=claim.fence,
            state=AgentResultState.OBSERVING,
            result=OutcomeUnknown(
                kind=OutcomeKind.UNKNOWN,
                wait_reason=WaitReason.JOB_STATE_UNCERTAIN,
                reason="output undelivered; files retained for re-delivery",
                evidence=OutcomeEvidence(
                    stage=FailureStage.JOB_OUTPUT_DELIVERY,
                    diagnostic="HTTP 413",
                ),
                receipt=receipt,
            ),
        )

    # Inference finished, but the lease expired before output delivery.
    clock.advance(seconds=31 if lease_lapses else 5)
    if reconcile:
        jobs.reconcile_orders()
    if lease_lapses:
        from vonk_control.agent_jobs.contracts import StaleAgentFence

        with pytest.raises(StaleAgentFence):
            jobs.record_result(undelivered(first))
        # The HTTP ingress reconciles a stale result through this same path.
        jobs.record_late_result(undelivered(first))
    else:
        jobs.record_result(undelivered(first))
    waiting = service.get(job.id)
    assert waiting.state == LifecycleState.BACKOFF
    assert waiting.result_evidence is not None
    assert waiting.result_evidence.exit_code == 0
    with sessions() as session:
        order = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == job.operation_id
            )
        )
        assert order is not None and order.next_action_at is not None
        deadline = order.recovery_deadline
        due = order.next_action_at
        order_id = order.id
    assert deadline == clock.now + timedelta(hours=24)
    clock.advance(seconds=int((due - clock.now).total_seconds()) + 1)
    # Restart queue ownership on the same PostgreSQL store. The retry keeps
    # the job identity and asks the agent to resume its durable exit receipt.
    restarted = AgentJobService(sessions, clock=clock)
    restarted.set_result_consumer(consume)
    second = claim_agent(restarted, node_id, "serial-0")
    assert second is not None and second.fence != first.fence
    from vonk_agent_protocol.recipe_jobs import RecipeJobRunRequest

    assert isinstance(second.payload, RecipeJobRunRequest)
    assert second.payload.output_delivery_only is True
    assert (
        second.payload.model_copy(update={"output_delivery_only": None})
        == first.payload
    )
    if expires:
        restarted.record_result(undelivered(second))
        with sessions() as session:
            retained = session.get(AgentOperation, order_id)
            assert retained is not None and retained.recovery_deadline == deadline
        clock.advance(seconds=24 * 60 * 60 + 1)
        for _ in range(3):
            restarted.reconcile_orders()
        assert claim_agent(restarted, node_id, "serial-0") is None
        return
    service.put_output(
        job.id,
        node_id=node_id,
        name=output.name,
        media_type=output.media_type,
        expected_sha256=output.sha256,
        content=content,
    )
    restarted.succeed(second, receipt)
    delivered = service.get(job.id)
    assert delivered.state == LifecycleState.SUCCEEDED
    assert delivered.output_files[0].sha256 == output.sha256
    assert (
        service.result_blob(job.id, output.name, output.sha256)[0].read_bytes()
        == content
    )


def running_two_rank_artifact_service(tmp_path):
    sessions, recipe_operations, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2, recipe_transform=_configure_artifact_recipe
    )
    installed = installed_recipe(
        recipe_operations,
        mapping_id,
        build_id,
        nodes,
        request_id="00000000-0000-4000-8000-000000000101",
    )
    run_plan = recipe_operations.preview_run(installed.owner_id, "image-job")
    activated = recipe_operations.activate_job_run(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000102",
    )
    service = ArtifactJobService(
        sessions,
        recipe_operations=recipe_operations,
        blob_store=ArtifactBlobStore(tmp_path / "artifact-blobs"),
        clock=lambda: NOW,
    )
    return sessions, recipe_operations, service, activated.owner_id, nodes


def test_a_job_on_a_two_node_run_is_one_host_network_order_per_rank(tmp_path) -> None:
    sessions, _operations, service, run_id, nodes = running_two_rank_artifact_service(
        tmp_path
    )
    # Admission accepts the job on both nodes and reserves the rendezvous on the
    # owner alone (main: "artifact job recipes currently require one node").
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        planned = parse_stored_run_plan(run.plan).nodes
    assert [node.endpoint_owner for node in planned].count(True) == 1
    assert {node.node_id for node in planned if node.rendezvous_port} == {
        node.node_id for node in planned if node.endpoint_owner
    }
    submitted = submitted_artifact_job(service, run_id, request_suffix=118)
    with sessions() as session:
        orders = session.scalars(
            select(AgentOperation)
            .where(AgentOperation.parent_job_id == submitted.operation_id)
            .order_by(AgentOperation.node_id)
        ).all()
        assert [order.node_id for order in orders] == sorted(nodes)
        plans = [
            RecipeJobRunRequest.model_validate(order.payload).compiled_execution_plan
            for order in orders
        ]
    placements = [plan.runtime.placement for plan in plans]
    assert {plan.security.network_mode for plan in plans} == {"host"}
    assert sorted(item.rank for item in placements) == [0, 1]
    # Only the rendezvous owner's fabric address is the master's.
    owners = [item for item in placements if item.local_address == item.master_address]
    assert [item.rank for item in owners] == [0]
    assert all(item.port is None for item in placements)


def _two_rank_claims(tmp_path, suffix: int):
    """A submitted job on a two-Spark run, with both Sparks' claimed orders."""

    sessions, recipe_operations, service, run_id, nodes = (
        running_two_rank_artifact_service(tmp_path)
    )
    clock = MutableClock(NOW)
    agent_jobs = AgentJobService(sessions, clock=clock)
    agent_jobs.set_result_consumer(
        controller_result_consumer(
            artifact_jobs=service.consume_agent_result,
            recipe_operations=recipe_operations.consume_agent_result,
        )
    )
    recipe_operations._agent_jobs = agent_jobs
    submitted = submitted_artifact_job(service, run_id, request_suffix=suffix)
    claims = {
        node_id: claim_agent(agent_jobs, node_id, f"serial-{index}")
        for index, node_id in enumerate(nodes)
    }
    assert all(claim is not None for claim in claims.values())
    with sessions() as session:
        ranks = {
            order.node_id: RecipeJobRunRequest.model_validate(
                order.payload
            ).compiled_execution_plan.runtime.placement.rank
            for order in session.scalars(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == submitted.operation_id
                )
            )
        }
    owner_node = next(node for node, rank in ranks.items() if rank == 0)
    companion_node = next(node for node in nodes if node != owner_node)
    owner_claim = claims[owner_node]
    companion_claim = claims[companion_node]
    assert owner_claim is not None and companion_claim is not None
    return (
        sessions,
        recipe_operations,
        service,
        agent_jobs,
        run_id,
        submitted,
        (owner_node, owner_claim),
        (companion_node, companion_claim),
        nodes,
    )


def test_a_newer_submit_fences_every_unissued_order_of_a_two_rank_job(tmp_path) -> None:
    sessions, _operations, service, run_id, _nodes = running_two_rank_artifact_service(
        tmp_path
    )
    first = submitted_artifact_job(service, run_id, request_suffix=118)
    second = submitted_artifact_job(service, run_id, request_suffix=128)
    assert second.state not in ajs.ENDED
    with sessions() as session:
        first_orders = session.scalars(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == first.operation_id
            )
        ).all()
        assert len(first_orders) == 2
        assert {order.state for order in first_orders} == {"cancelled"}
    assert service.get(first.id).state == "cancelled"


@pytest.mark.parametrize("failing_rank", ["companion", "owner"])
def test_a_rank_that_fails_ends_the_two_rank_job_and_stops_the_other(
    tmp_path, failing_rank: str
) -> None:
    (
        sessions,
        recipe_operations,
        service,
        agent_jobs,
        run_id,
        submitted,
        (owner_node, owner),
        (companion_node, companion),
        nodes,
    ) = _two_rank_claims(tmp_path, 118 if failing_rank == "companion" else 178)
    failing, surviving = (
        (companion, owner) if failing_rank == "companion" else (owner, companion)
    )
    agent_jobs.record_result(
        AgentResult.model_validate_json(
            canonical_message(
                {
                    "fence": str(failing.fence),
                    "state": "failed",
                    "result": {
                        "kind": "failed",
                        "code": "recipe_job_run_failed",
                        "reason": "rank 1 exited",
                    },
                }
            )
        )
    )
    assert service.get(submitted.id).state == "failed"
    # The surviving rank is told to stop: nothing waits on it. The owner
    # receipts its stop; a surviving companion stays live until the run's Stop.
    directive = agent_jobs.heartbeat(surviving, OperationProgress(phase="running"), 30)
    assert directive.cancel_requested is True
    if failing_rank == "companion":
        agent_jobs.record_result(
            cancellation_result(
                owner,
                submitted,
                state="cancelled",
                reason="controller cancellation requested",
            )
        )

    # The run's Stop removes the job's container on each Spark: one exact Stop
    # per rank, each naming the job, and the run is stopped once both receipt.
    from vonk_agent_protocol.recipe_operations import (
        RecipeStopPayload,
        RecipeStopResult,
    )

    plan_digest = recipe_operations.preview_stop(run_id).plan_digest

    def stop():
        return recipe_operations.stop(
            run_id,
            plan_digest=plan_digest,
            actor="operator",
            request_id="00000000-0000-4000-8000-000000000154",
        )

    assert stop().state not in {"succeeded", "failed", "cancelled"}
    if failing_rank == "owner":
        # The companion's Spark answers its cancel directive with its receipt,
        # then the Stop removes each rank's container.
        agent_jobs.record_result(
            cancellation_result(
                companion,
                submitted,
                state="cancelled",
                reason="controller cancellation requested",
            )
        )
    # Sequential phases: each rank's exact Stop is claimed in turn.
    stopped: set[str] = set()
    serials = {node_id: f"serial-{index}" for index, node_id in enumerate(nodes)}
    for _poll in range(4):
        for node_id in (owner_node, companion_node):
            if node_id in stopped:
                continue
            exact = claim_agent(agent_jobs, node_id, serials[node_id])
            if exact is None:
                continue
            assert isinstance(exact.payload, RecipeStopPayload)
            assert exact.payload.target_runtime_id == submitted.id
            agent_jobs.record_result(
                AgentResult(
                    fence=exact.fence,
                    state=AgentResultState.SUCCEEDED,
                    result=RecipeStopResult(),
                )
            )
            stopped.add(node_id)
    assert stopped == {owner_node, companion_node}
    assert stop().state == "succeeded"

    # The stopped run takes fresh work: a new job run is admitted on it.
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.state == "stopped"
        installation_id = run.installation_id
    fresh_plan = recipe_operations.preview_run(installation_id, "image-job")
    fresh = recipe_operations.activate_job_run(
        fresh_plan,
        plan_digest=fresh_plan.plan_digest,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000160",
    )
    assert fresh.owner_id != run_id


def test_a_companion_receipt_without_files_never_ends_the_job_and_the_owner_does(
    tmp_path,
) -> None:
    (
        sessions,
        _recipe_operations,
        service,
        agent_jobs,
        run_id,
        submitted,
        (owner_node, owner),
        (_companion_node, companion),
        _nodes,
    ) = _two_rank_claims(tmp_path, 138)
    from vonk_agent_protocol.recipe_jobs import (
        RecipeJobEvidence,
        RecipeJobOutputManifest,
    )

    content = b"image"
    output = RecipeJobFile(
        name="output.png",
        media_type="image/png",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    empty: tuple[RecipeJobFile, ...] = ()

    def receipt(files: tuple[RecipeJobFile, ...]) -> RecipeJobRunResult:
        return RecipeJobRunResult(
            job_id=submitted.id,
            run_id=run_id,
            exit_code=0,
            output_manifest=RecipeJobOutputManifest(
                schema_version=1,
                total_bytes=sum(file.size_bytes for file in files),
                files=files,
                manifest_sha256=recipe_job_manifest_sha256(files),
            ),
            evidence=RecipeJobEvidence(
                elapsed_milliseconds=100, peak_memory_bytes=None
            ),
        )

    agent_jobs.succeed(companion, receipt(empty))
    # A companion's empty receipt is all the job learns: it keeps running.
    assert service.get(submitted.id).state == "running"
    service.put_output(
        submitted.id,
        node_id=owner_node,
        name=output.name,
        media_type=output.media_type,
        expected_sha256=output.sha256,
        content=content,
    )
    agent_jobs.succeed(owner, receipt((output,)))
    ended = service.get(submitted.id)
    # The owner's delivery ends the job succeeded.
    assert ended.state == "succeeded"
    assert ended.output_files[0].sha256 == output.sha256
    with sessions() as session:
        parent = session.get(Job, ended.operation_id)
        assert parent is not None
        assert parent.status_reason != "superseded_by_intent"


@pytest.mark.parametrize("companion_ends", ["cancelled", "exits-zero", "fails"])
@pytest.mark.parametrize("owner_outcome", ["succeeded", "failed"])
def test_a_running_companion_is_told_to_stop_when_the_owner_ends(
    tmp_path, owner_outcome: str, companion_ends: str
) -> None:
    (
        sessions,
        _recipe_operations,
        service,
        agent_jobs,
        run_id,
        submitted,
        (owner_node, owner),
        (_companion_node, companion),
        _nodes,
    ) = _two_rank_claims(
        tmp_path, 200 + 10 * ["cancelled", "exits-zero", "fails"].index(companion_ends)
    )
    if owner_outcome == "succeeded":
        from vonk_agent_protocol.recipe_jobs import (
            RecipeJobEvidence,
            RecipeJobOutputManifest,
        )

        content = b"image"
        output = RecipeJobFile(
            name="output.png",
            media_type="image/png",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )
        service.put_output(
            submitted.id,
            node_id=owner_node,
            name=output.name,
            media_type=output.media_type,
            expected_sha256=output.sha256,
            content=content,
        )
        agent_jobs.succeed(
            owner,
            RecipeJobRunResult(
                job_id=submitted.id,
                run_id=run_id,
                exit_code=0,
                output_manifest=RecipeJobOutputManifest(
                    schema_version=1,
                    total_bytes=len(content),
                    files=(output,),
                    manifest_sha256=recipe_job_manifest_sha256((output,)),
                ),
                evidence=RecipeJobEvidence(
                    elapsed_milliseconds=100, peak_memory_bytes=None
                ),
            ),
        )
    else:
        agent_jobs.record_result(
            AgentResult.model_validate_json(
                canonical_message(
                    {
                        "fence": str(owner.fence),
                        "state": "failed",
                        "result": {
                            "kind": "failed",
                            "code": "recipe_job_run_failed",
                            "reason": "rank 0 exited",
                        },
                    }
                )
            )
        )
    assert service.get(submitted.id).state == owner_outcome

    def parent_outcome() -> tuple[str, str | None]:
        with sessions() as session:
            parent = session.get(Job, service.get(submitted.id).operation_id)
            assert parent is not None
            return parent.state, parent.status_reason

    # The companion is still running its rank: it is told to stop, so the
    # Spark is free for the next job.
    directive = agent_jobs.heartbeat(companion, OperationProgress(phase="running"), 30)
    assert directive.cancel_requested is True
    owner_reason = "rank 0 exited" if owner_outcome == "failed" else None
    if companion_ends == "cancelled":
        agent_jobs.record_result(
            cancellation_result(
                companion,
                submitted,
                state="cancelled",
                reason="controller cancellation requested",
            )
        )
    elif companion_ends == "fails":
        agent_jobs.record_result(
            AgentResult.model_validate_json(
                canonical_message(
                    {
                        "fence": str(companion.fence),
                        "state": "failed",
                        "result": {
                            "kind": "failed",
                            "code": "recipe_job_run_failed",
                            "reason": "rank 1 exited",
                        },
                    }
                )
            )
        )
    else:
        from vonk_agent_protocol.recipe_jobs import (
            RecipeJobEvidence,
            RecipeJobOutputManifest,
        )

        agent_jobs.succeed(
            companion,
            RecipeJobRunResult(
                job_id=submitted.id,
                run_id=run_id,
                exit_code=0,
                output_manifest=RecipeJobOutputManifest(
                    schema_version=1,
                    total_bytes=0,
                    files=(),
                    manifest_sha256=recipe_job_manifest_sha256(()),
                ),
                evidence=RecipeJobEvidence(
                    elapsed_milliseconds=100, peak_memory_bytes=None
                ),
            ),
        )
    # The artifact job owns the outcome: it and its parent keep the owner's,
    # however the companion ends afterwards (cancelled, exit 0, or failing).
    assert service.get(submitted.id).state == owner_outcome
    assert parent_outcome() == (owner_outcome, owner_reason)
    # A fresh job on the same run is admitted.
    fresh = submitted_artifact_job(service, run_id, request_suffix=158)
    assert fresh.id != submitted.id and fresh.state not in ajs.ENDED


def test_a_companion_that_fails_first_makes_the_job_and_parent_failed(tmp_path) -> None:
    (
        sessions,
        _recipe_operations,
        service,
        agent_jobs,
        run_id,
        submitted,
        (_owner_node, owner),
        (_companion_node, companion),
        _nodes,
    ) = _two_rank_claims(tmp_path, 188)
    agent_jobs.record_result(
        AgentResult.model_validate_json(
            canonical_message(
                {
                    "fence": str(companion.fence),
                    "state": "failed",
                    "result": {
                        "kind": "failed",
                        "code": "recipe_job_run_failed",
                        "reason": "rank 1 exited",
                    },
                }
            )
        )
    )
    assert service.get(submitted.id).state == "failed"
    from vonk_agent_protocol.recipe_jobs import (
        RecipeJobEvidence,
        RecipeJobOutputManifest,
    )

    content = b"image"
    output = RecipeJobFile(
        name="output.png",
        media_type="image/png",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    # The owner's late success cannot turn the failed job or its parent around.
    agent_jobs.succeed(
        owner,
        RecipeJobRunResult(
            job_id=submitted.id,
            run_id=run_id,
            exit_code=0,
            output_manifest=RecipeJobOutputManifest(
                schema_version=1,
                total_bytes=len(content),
                files=(output,),
                manifest_sha256=recipe_job_manifest_sha256((output,)),
            ),
            evidence=RecipeJobEvidence(
                elapsed_milliseconds=100, peak_memory_bytes=None
            ),
        ),
    )
    assert service.get(submitted.id).state == "failed"
    with sessions() as session:
        parent = session.get(Job, service.get(submitted.id).operation_id)
        assert parent is not None
        # One fact: the parent carries the artifact job's own state and reason.
        assert parent.state == "failed"
        assert parent.status_reason == service.get(submitted.id).status_reason
        assert "rank 1 exited" in (parent.status_reason or "")
