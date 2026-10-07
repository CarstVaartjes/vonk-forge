"""Expired references must release CAS bytes without poisoning their next owner."""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from uuid import uuid4

from sqlalchemy import Engine, select
from vonk_agent_protocol import RecipeJobRunRequest, recipe_job_manifest_sha256
from vonk_control.agent_jobs import AgentJobService
from vonk_control.artifact_blob_store import ArtifactBlobStore
from vonk_control.artifact_jobs import ArtifactJobService
from vonk_control.models import (
    AgentOperation,
    AgentOperationAttempt,
    ArtifactJob,
    ArtifactJobBlob,
    ArtifactJobFile,
)

from .non_blocking import assert_no_orphaned_holds
from .runtime_identity_support import claim_agent
from .test_artifact_jobs import (
    MutableClock,
    artifact_create_request,
    create_artifact_job,
    running_artifact_service,
)
from .test_recipe_operations import NOW


def test_reference_safe_cas_retention_restart_reuploads_and_claims_identical_input(
    tmp_path: Path, postgres_engine: Engine
) -> None:
    """Catch retained dead bytes, lost shared bytes, or poisoned post-GC claims."""
    sessions, operations, _queue, _original, run_id, node_id = running_artifact_service(
        tmp_path, engine=postgres_engine
    )
    clock = MutableClock(NOW)
    root = tmp_path / "artifact-blobs"
    store = ArtifactBlobStore(root)
    service = ArtifactJobService(
        sessions, recipe_operations=operations, blob_store=store, clock=clock
    )
    exclusive = b"exclusive input recreated after retention"
    shared = b"input still owned by a surviving job"
    exclusive_digest = hashlib.sha256(exclusive).hexdigest()
    shared_digest = hashlib.sha256(shared).hexdigest()

    def prepare(owner: ArtifactJobService, content: bytes):
        request = artifact_create_request(run_id, str(uuid4()))
        request["inputs"][0]["size_bytes"] = len(content)
        request["inputs"][0]["sha256"] = hashlib.sha256(content).hexdigest()
        job = create_artifact_job(owner, **request)
        owner.put_input(
            job.id,
            name="input.png",
            media_type="image/png",
            expected_sha256=hashlib.sha256(content).hexdigest(),
            content=content,
        )
        ready = owner.finalize(job.id)
        assert ready.preparation == "ready"
        return ready

    expired_exclusive = prepare(service, exclusive)
    expired_shared = prepare(service, shared)
    survivor = prepare(service, shared)
    for job in (expired_exclusive, expired_shared):
        ended = service.cancel(
            job.id,
            actor="operator",
            request_id=str(uuid4()),
            reason="input owner ended before execution",
        )
        assert ended.state == "cancelled"

    exclusive_path = root / exclusive_digest[:2] / exclusive_digest
    shared_path = root / shared_digest[:2] / shared_digest
    assert exclusive_path.read_bytes() == exclusive
    assert shared_path.read_bytes() == shared
    # Advance the owning retention clock, not stored lifecycle/completion rows.
    clock.advance(seconds=8 * 24 * 60 * 60)
    # CAS uses filesystem age independently of the Controller clock. These are
    # real uploaded files; age only their grace observation, never ownership.
    old_mtime = time.time() - 3600
    for path in (exclusive_path, shared_path):
        os.utime(path, (old_mtime, old_mtime))

    report = service.reconcile_storage()
    assert not exclusive_path.exists(), "expired exclusive CAS bytes survived"
    assert report.expired_jobs == 2
    assert report.removed_blob_records == 1
    assert report.removed_orphan_blobs == 1
    assert shared_path.read_bytes() == shared
    with sessions() as session:
        assert session.get(ArtifactJob, expired_exclusive.id) is None
        assert session.get(ArtifactJob, expired_shared.id) is None
        assert session.get(ArtifactJobBlob, exclusive_digest) is None
        assert session.get(ArtifactJobBlob, shared_digest) is not None
        assert (
            session.scalar(
                select(ArtifactJobFile).where(
                    ArtifactJobFile.artifact_job_id == survivor.id,
                    ArtifactJobFile.blob_sha256 == shared_digest,
                )
            )
            is not None
        )
        assert_no_orphaned_holds(session)

    # Reconstruct both storage and execution owners over the existing authority.
    restarted_store = ArtifactBlobStore(root)
    restarted = ArtifactJobService(
        sessions,
        recipe_operations=operations,
        blob_store=restarted_store,
        clock=clock,
    )
    agents = AgentJobService(sessions, clock=clock)

    def consume(session, operation, attempt, message) -> None:
        restarted.consume_agent_result(session, operation, attempt, message)
        operations.consume_agent_result(session, operation, attempt, message)

    agents.set_result_consumer(consume)
    operations._agent_jobs = agents
    assert claim_agent(agents, node_id, "serial-0") is None
    fresh = prepare(restarted, exclusive)
    assert fresh.id not in {expired_exclusive.id, expired_shared.id, survivor.id}
    submission_key = str(uuid4())
    submitted = restarted.submit(fresh.id, actor="operator", request_id=submission_key)
    assert submitted.operation_id is not None
    assert submitted.submit_request_id == submission_key
    claim = claim_agent(agents, node_id, "serial-0")
    assert claim is not None and isinstance(claim.payload, RecipeJobRunRequest)
    assert claim.payload.job_id == fresh.id
    assert claim.payload.input_total_bytes == len(exclusive)
    assert claim.payload.input_manifest_sha256 == recipe_job_manifest_sha256(
        claim.payload.inputs
    )
    assert len(claim.payload.inputs) == 1
    assert claim.payload.inputs[0].sha256 == exclusive_digest
    path, media_type, size = restarted.input_blob(
        fresh.id, exclusive_digest, node_id=node_id
    )
    assert path.read_bytes() == exclusive
    assert media_type == "image/png" and size == len(exclusive)
    assert restarted.get(survivor.id).preparation == "ready"
    assert shared_path.read_bytes() == shared
    with sessions() as session:
        child = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == submitted.operation_id
            )
        )
        assert child is not None
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == child.id,
                AgentOperationAttempt.attempt == child.current_attempt,
            )
        )
        assert attempt is not None and attempt.fence == str(claim.fence)
        assert_no_orphaned_holds(session)
    usage = restarted_store.usage()
    assert usage.reserved_bytes == 0 and usage.in_flight_uploads == 0
    assert usage.used_bytes == len(exclusive) + len(shared)
