"""Artifact jobs: dispatch receipts."""

from __future__ import annotations

import asyncio
import copy
import hashlib
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from vonk_forge_contracts import RecipeDefinition, document_sha256

from vonk_agent_protocol.contracts import canonical_message
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_agent_protocol.recipe_jobs import (
    RecipeJobFile,
)
from vonk_agent_protocol.recipe_jobs import (
    manifest_document as recipe_job_manifest_document,
)
from vonk_agent_protocol.recipe_jobs import (
    manifest_sha256 as recipe_job_manifest_sha256,
)
from vonk_agent_protocol.wire_model import OperationProgress
from vonk_control.agent_jobs.service import AgentJobService
from vonk_control.artifact_jobs import states as ajs
from vonk_control.artifact_jobs.blob_store import (
    ArtifactBlobStore,
    ArtifactBlobStoreError,
    StoredArtifactBlob,
)
from vonk_control.artifact_jobs.contracts import (
    ArtifactJobError,
    ArtifactJobView,
)
from vonk_control.models.catalog import CatalogDocumentRevision
from vonk_control.models.execution import (
    ArtifactJob,
    RecipeInstallation,
    RecipeRun,
)
from vonk_control.models.fleet import AgentNode
from vonk_control.models.jobs import Job
from vonk_control.models.operations import AgentOperation
from vonk_control.recipe_operations.errors import (
    RecipeArtifactJobCancellationPending,
    RecipeRequestInvalid,
)
from vonk_control.recipe_operations.interfaces import RecipeOperationView

from .artifact_jobs_support import (
    MutableClock,
    _cancelling,
    _mapping,
    _sequence,
    artifact_create_request,
    cancellation_result,
    create_artifact_job,
    running_artifact_service,
    submitted_artifact_job,
)
from .non_blocking import assert_ended_without_blocking, assert_no_orphaned_holds
from .recipe_operations_support import NOW
from .runtime_identity_support import claim_agent


@pytest.mark.parametrize(
    "corruption",
    (
        "non-object-payload",
        "payload-digest",
        "foreign-owner",
        "malformed-request-id",
        "null-request-id",
    ),
)
@pytest.mark.usefixtures("damaged_json_rows")
def test_artifact_submission_receipt_degrades_on_corrupt_owner(
    tmp_path, corruption: str
) -> None:
    sessions, _operations, _queue, service, run_id, _node_id = running_artifact_service(
        tmp_path
    )
    submitted = submitted_artifact_job(service, run_id, request_suffix=180)
    assert submitted.operation_id is not None

    if corruption == "null-request-id":
        with sessions() as session:
            artifact_job = session.get(ArtifactJob, submitted.id)
            parent = session.get(Job, submitted.operation_id)
            assert artifact_job is not None and parent is not None
            object.__setattr__(parent, "request_id", None)
            # The submission's identity is damaged bookkeeping: the job is shown
            # without it instead of being refused.
            with session.no_autoflush:
                view = service._view_in_session(session, artifact_job)
            assert (view.operation_id, view.submit_request_id) == (None, None)
        return

    with sessions.begin() as session:
        parent = session.get(Job, submitted.operation_id)
        assert parent is not None
        if corruption == "non-object-payload":
            payload: object = ["malformed owner envelope"]
            object.__setattr__(parent, "payload", payload)
            parent.payload_digest = hashlib.sha256(
                canonical_message(payload)
            ).hexdigest()
        elif corruption == "payload-digest":
            parent.payload_digest = "0" * 64
        elif corruption == "foreign-owner":
            payload = {
                **parent.payload,
                "owner_id": "00000000-0000-4000-8000-000000000999",
            }
            parent.payload = payload
            parent.payload_digest = hashlib.sha256(
                canonical_message(payload)
            ).hexdigest()
        elif corruption == "malformed-request-id":
            parent.request_id = "not-a-uuid"
        else:
            raise AssertionError(f"unexpected corruption: {corruption}")

    view = service.get(submitted.id)
    assert (view.operation_id, view.submit_request_id) == (None, None)


def test_artifact_job_rejects_unsafe_names(tmp_path) -> None:
    _sessions, _operations, _queue, service, run_id, _node_id = (
        running_artifact_service(tmp_path)
    )
    request = {
        "run_id": run_id,
        "interface": "image-job",
        "parameters": {},
        "output_limits": {
            "max_files": 1,
            "max_file_bytes": 10,
            "max_total_bytes": 10,
            "allowed_media_types": ["image/png"],
        },
        "actor": "operator",
        "request_id": "00000000-0000-4000-8000-000000000105",
    }
    with pytest.raises(Exception):  # noqa: B017 -- effects and subsequent admission witness rejection
        create_artifact_job(
            service,
            **request,
            inputs=[
                {
                    "slot": "input",
                    "name": "../escape",
                    "size_bytes": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                }
            ],
            timeout_seconds=60,
        )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda value: value["parameters"].update(extra=True), "undeclared"),
        (lambda value: value["parameters"].update(seed="101"), "wrong type"),
        (lambda value: value["inputs"][0].update(slot="other"), "undeclared"),
        (lambda value: value["output_limits"].update(max_files=2), "exceed"),
        (
            lambda value: value["output_limits"].update(
                allowed_media_types=["image/jpeg"]
            ),
            "exceed",
        ),
    ],
)
def test_artifact_job_server_contract_rejects_client_escalation(
    tmp_path, change, message
) -> None:
    _sessions, _operations, _queue, service, run_id, _node_id = (
        running_artifact_service(tmp_path)
    )
    content = b"png"
    request = {
        "run_id": run_id,
        "interface": "image-job",
        "parameters": {"prompt": "fox", "seed": 0},
        "inputs": [
            {
                "slot": "input",
                "name": "input.png",
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
        "output_limits": {
            "max_files": 1,
            "max_file_bytes": 1024,
            "max_total_bytes": 4096,
            "allowed_media_types": ["image/png"],
        },
        "timeout_seconds": 3600,
        "actor": "operator",
        "request_id": "00000000-0000-4000-8000-000000000106",
    }
    change(request)
    with pytest.raises(ArtifactJobError):
        create_artifact_job(service, **request)


def test_artifact_job_refuses_an_input_name_that_fits_no_declared_extension(
    tmp_path,
) -> None:
    def transform(document: dict[str, object]) -> None:
        interface = _mapping(_sequence(document["interfaces"])[0])
        interface["input"] = {
            "required": True,
            "media_types": ["image/png"],
            "max_bytes": 4096,
            "slots": [
                {
                    "id": "input",
                    "label": "Input",
                    "description": "Source image",
                    "media_types": ["image/png"],
                    "extensions": [".png"],
                    "min_files": 1,
                    "max_files": 1,
                    "max_file_bytes": 1024,
                    "max_total_bytes": 4096,
                }
            ],
        }

    _sessions, _operations, _queue, service, run_id, _node_id = (
        running_artifact_service(tmp_path, recipe_transform=transform)
    )
    request = artifact_create_request(run_id, "00000000-0000-4000-8000-000000000107")
    request["inputs"][0]["name"] = "input.exe"
    with pytest.raises(ArtifactJobError, match=r"extensions: \.png"):
        create_artifact_job(service, **request)


@pytest.mark.parametrize(
    ("media_type", "extension"),
    [
        ("application/pdf", ".pdf"),
        ("image/avif", ".avif"),
        ("application/vnd.example.custom", ".vonk"),
    ],
)
def test_artifact_job_dispatches_exact_signed_output_mapping(
    tmp_path, media_type: str, extension: str
) -> None:
    def transform(document: dict[str, object]) -> None:
        interface = _mapping(_sequence(document["interfaces"])[0])
        output = _mapping(interface["output"])
        slot = _mapping(_sequence(output["slots"])[0])
        slot["media_types"] = [media_type]
        slot["extensions"] = [extension]

    sessions, _operations, _queue, service, run_id, _node_id = running_artifact_service(
        tmp_path, recipe_transform=transform
    )
    request = artifact_create_request(run_id, "00000000-0000-4000-8000-000000000130")
    request["output_limits"] = {
        **request["output_limits"],
        "allowed_media_types": [media_type],
    }
    job = create_artifact_job(service, **request)
    content = b"png"
    service.put_input(
        job.id,
        name="input.png",
        media_type="image/png",
        expected_sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )
    service.finalize(job.id)
    submitted = service.submit(
        job.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000131",
    )
    with sessions() as session:
        operation = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == submitted.operation_id
            )
        )
        assert operation is not None
        assert operation.payload["output_mappings"] == [
            {
                "slot": "image",
                "media_type": media_type,
                "extensions": [extension],
            }
        ]


def test_artifact_job_rejects_unrepresentable_output_media_mapping(tmp_path) -> None:
    originals = []

    def transform(document: dict[str, object]) -> None:
        originals.append(copy.deepcopy(document))
        interface = _mapping(_sequence(document["interfaces"])[0])
        output = _mapping(interface["output"])
        slot = _mapping(_sequence(output["slots"])[0])
        slot["media_types"] = ["image/avif", "image/png"]
        slot["extensions"] = [".avif", ".png"]

    sessions, _operations, _queue, service, run_id, _node_id = running_artifact_service(
        tmp_path, recipe_transform=transform
    )

    with pytest.raises(Exception):  # noqa: B017 -- ending witness; effects and fresh admission establish behaviour
        create_artifact_job(
            service,
            **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000132"),
        )
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        installation = session.get(RecipeInstallation, run.installation_id)
        assert installation is not None
        revision = session.get(CatalogDocumentRevision, installation.recipe_revision_id)
        assert revision is not None
        restored = RecipeDefinition.model_validate(originals[0]).model_dump(mode="json")
        original_document = copy.deepcopy(revision.document)
        original_digest = revision.content_digest
        replacement = CatalogDocumentRevision(
            document_id=revision.document_id,
            kind=revision.kind,
            publisher=revision.publisher,
            slug=revision.slug,
            revision_number=revision.revision_number + 1,
            schema_version=revision.schema_version,
            state=revision.state,
            document=restored,
            content_digest=document_sha256(restored),
            projected={},
            created_by=revision.created_by,
            created_at=revision.created_at,
        )
        session.add(replacement)
        session.flush()
        installation.recipe_revision_id = replacement.id
    with sessions() as session:
        original = session.get(CatalogDocumentRevision, revision.id)
        assert original is not None
        assert original.document == original_document
        assert original.content_digest == original_digest
    fresh = create_artifact_job(
        service,
        **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000134"),
    )
    assert fresh.operation_id is None


def test_artifact_job_rejects_cross_slot_output_extension_collision(tmp_path) -> None:
    originals = []

    def transform(document: dict[str, object]) -> None:
        originals.append(copy.deepcopy(document))
        interface = _mapping(_sequence(document["interfaces"])[0])
        output = _mapping(interface["output"])
        slots = _sequence(output["slots"])
        duplicate = copy.deepcopy(_mapping(slots[0]))
        duplicate.update(
            {
                "id": "receipt",
                "label": "Receipt",
                "description": "Generated receipt",
                "media_types": ["application/json"],
            }
        )
        slots.append(duplicate)

    sessions, _operations, _queue, service, run_id, _node_id = running_artifact_service(
        tmp_path, recipe_transform=transform
    )

    with pytest.raises(Exception):  # noqa: B017 -- ending witness; effects and fresh admission establish behaviour
        create_artifact_job(
            service,
            **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000133"),
        )
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        installation = session.get(RecipeInstallation, run.installation_id)
        assert installation is not None
        revision = session.get(CatalogDocumentRevision, installation.recipe_revision_id)
        assert revision is not None
        restored = RecipeDefinition.model_validate(originals[0]).model_dump(mode="json")
        original_document = copy.deepcopy(revision.document)
        original_digest = revision.content_digest
        replacement = CatalogDocumentRevision(
            document_id=revision.document_id,
            kind=revision.kind,
            publisher=revision.publisher,
            slug=revision.slug,
            revision_number=revision.revision_number + 1,
            schema_version=revision.schema_version,
            state=revision.state,
            document=restored,
            content_digest=document_sha256(restored),
            projected={},
            created_by=revision.created_by,
            created_at=revision.created_at,
        )
        session.add(replacement)
        session.flush()
        installation.recipe_revision_id = replacement.id
    with sessions() as session:
        original = session.get(CatalogDocumentRevision, revision.id)
        assert original is not None
        assert original.document == original_document
        assert original.content_digest == original_digest
    fresh = create_artifact_job(
        service,
        **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000135"),
    )
    assert fresh.operation_id is None


def test_artifact_output_uses_longest_signed_suffix_for_same_media_type(
    tmp_path,
) -> None:
    def transform(document: dict[str, object]) -> None:
        media_type = "application/vnd.example.custom"
        interface = _mapping(_sequence(document["interfaces"])[0])
        output = _mapping(interface["output"])
        slots = _sequence(output["slots"])
        short = _mapping(slots[0])
        short.update(
            {
                "id": "binary",
                "media_types": [media_type],
                "extensions": [".bin"],
                "min_files": 0,
            }
        )
        detailed = copy.deepcopy(short)
        detailed.update(
            {
                "id": "detailed",
                "label": "Detailed binary",
                "description": "Generated detailed binary",
                "extensions": [".vonk.bin"],
                "min_files": 1,
            }
        )
        slots.append(detailed)
        validation = _mapping(document["validation"])
        serving = _mapping(validation["serving"])
        check = _mapping(_sequence(serving["checks"])[0])
        request = _mapping(check["request"])
        request["output_slot"] = "detailed"

    sessions, _operations, _queue, service, run_id, node_id = running_artifact_service(
        tmp_path, recipe_transform=transform
    )
    media_type = "application/vnd.example.custom"
    request = artifact_create_request(run_id, "00000000-0000-4000-8000-000000000134")
    request["output_limits"] = {
        **request["output_limits"],
        "max_files": 2,
        "allowed_media_types": [media_type],
    }
    job = create_artifact_job(service, **request)
    content = b"png"
    service.put_input(
        job.id,
        name="input.png",
        media_type="image/png",
        expected_sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )
    service.finalize(job.id)
    submitted = service.submit(
        job.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000135",
    )
    output_content = b"done"
    output_digest = hashlib.sha256(output_content).hexdigest()
    service.put_output(
        job.id,
        node_id=node_id,
        name="artifact.vonk.bin",
        media_type=media_type,
        expected_sha256=output_digest,
        content=output_content,
    )
    produced = RecipeJobFile(
        name="artifact.vonk.bin",
        media_type=media_type,
        size_bytes=len(output_content),
        sha256=output_digest,
    )
    result = {
        "job_id": job.id,
        "run_id": run_id,
        "exit_code": 0,
        "output_manifest": {
            **recipe_job_manifest_document((produced,)),
            "manifest_sha256": recipe_job_manifest_sha256((produced,)),
        },
        "evidence": {"elapsed_milliseconds": 1, "peak_memory_bytes": None},
    }
    with sessions.begin() as session:
        operation = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == submitted.operation_id
            )
        )
        assert operation is not None
        service.consume_agent_result(
            session,
            operation,
            object(),
            SimpleNamespace(state="succeeded", result=result),
        )

    assert service.get(job.id).state == "succeeded"


def test_logical_job_run_blocks_stop_and_serializes_full_model_jobs(tmp_path) -> None:
    sessions, operations, _queue, service, run_id, _node_id = running_artifact_service(
        tmp_path
    )
    content = b"png"

    def create(request_id: str):
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
                    "sha256": hashlib.sha256(content).hexdigest(),
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
            request_id=request_id,
        )
        service.put_input(
            job.id,
            name="input.png",
            media_type="image/png",
            expected_sha256=hashlib.sha256(content).hexdigest(),
            content=content,
        )
        return service.finalize(job.id)

    first = create("00000000-0000-4000-8000-000000000107")
    assert operations.preview_stop(run_id).allowed
    submitted = service.submit(
        first.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000108",
    )
    with sessions.begin() as session:
        operation = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == submitted.operation_id
            )
        )
        assert operation is not None
        operation.state = "running"
    cancelling = service.cancel(
        first.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000113",
        reason="operator requested stop",
    )
    assert _cancelling(cancelling)
    assert _cancelling(
        service.cancel(
            first.id,
            actor="operator",
            request_id="00000000-0000-4000-8000-000000000113",
            reason="operator requested stop",
        )
    )
    assert operations.preview_stop(run_id).allowed
    second = create("00000000-0000-4000-8000-000000000109")
    accepted = service.submit(
        second.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000110",
    )
    assert accepted.operation_id != submitted.operation_id
    assert accepted.state == LifecycleState.QUEUED
    # An unissued order has no physical effect to reconcile: newer intent wins.
    assert service.get(first.id).state in ajs.ENDED
    fresh = create("00000000-0000-4000-8000-000000000111")
    assert fresh.id not in {first.id, second.id}


def test_running_artifact_cancellation_waits_for_agent_ack_and_fences_late_result(
    tmp_path,
) -> None:
    sessions, recipe_operations, _queue, service, run_id, node_id = (
        running_artifact_service(tmp_path)
    )
    agent_jobs = AgentJobService(sessions, clock=MutableClock(NOW))

    def consume(session, operation, attempt, message) -> None:
        service.consume_agent_result(session, operation, attempt, message)
        recipe_operations.consume_agent_result(session, operation, attempt, message)

    agent_jobs.set_result_consumer(consume)
    recipe_operations._agent_jobs = agent_jobs
    assert claim_agent(agent_jobs, node_id, "serial-0") is None
    submitted = submitted_artifact_job(service, run_id, request_suffix=118)
    claim = claim_agent(agent_jobs, node_id, "serial-0")
    assert claim is not None

    cancelling = service.cancel(
        submitted.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000120",
        reason="operator requested stop",
    )
    assert _cancelling(cancelling)
    directive = agent_jobs.heartbeat(claim, OperationProgress(phase="running"), 30)
    assert directive.cancel_requested is True
    stop_plan = recipe_operations.preview_stop(run_id)
    assert stop_plan.allowed
    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        node.workload_intent_ordinal += 1
        ordinal = node.workload_intent_ordinal
        agent_jobs.request_superseded_workload_cancellation_in_session(
            session, (node_id,), ordinal, NOW
        )
    with pytest.raises(Exception):  # noqa: B017 -- effect remains unconfirmed; exact receipt and fresh admission below
        recipe_operations.stop(
            run_id,
            plan_digest=stop_plan.plan_digest,
            actor="operator",
            request_id="00000000-0000-4000-8000-000000000154",
            workload_intent_ordinal=ordinal,
        )
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.state == "running" and run.stopped_at is None

    acknowledged = cancellation_result(
        claim,
        submitted,
        state="cancelled",
        reason="controller cancellation requested",
    )
    agent_jobs.record_result(acknowledged)
    assert service.get(submitted.id).state == "cancelled"
    stopped = recipe_operations.stop(
        run_id,
        plan_digest=stop_plan.plan_digest,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000154",
        workload_intent_ordinal=ordinal,
    )
    assert stopped.state == "succeeded"
    with pytest.raises(Exception):  # noqa: B017 -- ending witness; effects and fresh admission establish behaviour
        agent_jobs.record_result(acknowledged)


@pytest.mark.parametrize("same_content", [True, False])
def test_duplicate_jobrun_stop_replays_the_stop_that_ended_after_its_probe(
    tmp_path, monkeypatch, same_content: bool
) -> None:
    """A duplicate Stop replays its request's Stop in whatever state it is in.

    The duplicate's pending probe reads the accepted Stop as waiting; the first
    copy then finishes it before the duplicate takes its lock. Only different
    content under the same request key is refused, as caller input.
    """

    sessions, recipe_operations, _queue, service, run_id, node_id = (
        running_artifact_service(tmp_path)
    )
    agent_jobs = AgentJobService(sessions, clock=MutableClock(NOW))

    def consume(session, operation, attempt, message) -> None:
        service.consume_agent_result(session, operation, attempt, message)
        recipe_operations.consume_agent_result(session, operation, attempt, message)

    agent_jobs.set_result_consumer(consume)
    recipe_operations._agent_jobs = agent_jobs
    assert claim_agent(agent_jobs, node_id, "serial-0") is None
    submitted = submitted_artifact_job(service, run_id, request_suffix=118)
    claim = claim_agent(agent_jobs, node_id, "serial-0")
    assert claim is not None
    service.cancel(
        submitted.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000120",
        reason="operator requested stop",
    )
    stop_plan = recipe_operations.preview_stop(run_id)
    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        node.workload_intent_ordinal += 1
        ordinal = node.workload_intent_ordinal
        agent_jobs.request_superseded_workload_cancellation_in_session(
            session, (node_id,), ordinal, NOW
        )
    request_id = "00000000-0000-4000-8000-000000000154"

    def stop(workload_intent_ordinal: int):
        return recipe_operations.stop(
            run_id,
            plan_digest=stop_plan.plan_digest,
            actor="operator",
            request_id=request_id,
            workload_intent_ordinal=workload_intent_ordinal,
        )

    with pytest.raises(RecipeArtifactJobCancellationPending):
        stop(ordinal)

    probe = recipe_operations._one_shot_stop_is_pending
    entered: list[bool] = []
    finished: list[RecipeOperationView] = []

    def probe_then_first_copy_finishes(probed_request_id: str) -> bool:
        pending = probe(probed_request_id)
        if not entered:
            entered.append(True)
            # Sync point: the duplicate has read the Stop as pending; the first
            # copy's continuation now finishes it before the duplicate locks.
            assert pending
            agent_jobs.record_result(
                cancellation_result(
                    claim,
                    submitted,
                    state="cancelled",
                    reason="controller cancellation requested",
                )
            )
            finished.append(stop(ordinal))
        return pending

    monkeypatch.setattr(
        recipe_operations,
        "_one_shot_stop_is_pending",
        probe_then_first_copy_finishes,
    )
    if same_content:
        replayed = stop(ordinal)
        assert replayed.id == finished[0].id
        assert replayed.state == finished[0].state == "succeeded"
    else:
        with pytest.raises(RecipeRequestInvalid):
            stop(ordinal + 1)
        assert finished[0].state == "succeeded"
    with sessions() as session:
        jobs = tuple(session.scalars(select(Job).where(Job.request_id == request_id)))
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.state == "stopped"
        installation_id = run.installation_id
    assert [job.id for job in jobs] == [finished[0].id]
    # Nothing is left holding the installation: fresh work is admitted.
    plan = recipe_operations.preview_run(installation_id, "image-job")
    reactivated = recipe_operations.activate_job_run(
        plan,
        plan_digest=plan.plan_digest,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000160",
    )
    assert reactivated.owner_id != run_id


def test_artifact_cancel_stop_failure_ends_cancelled_with_residue(tmp_path) -> None:
    """An unconfirmed stop completes: ``cancelled``, effect unknown, residue kept."""

    sessions, recipe_operations, _queue, service, run_id, node_id = (
        running_artifact_service(tmp_path)
    )
    clock = MutableClock(NOW)
    agent_jobs = AgentJobService(sessions, clock=clock)

    def consume(session, operation, attempt, message) -> None:
        service.consume_agent_result(session, operation, attempt, message)
        recipe_operations.consume_agent_result(session, operation, attempt, message)

    agent_jobs.set_result_consumer(consume)
    recipe_operations._agent_jobs = agent_jobs
    assert claim_agent(agent_jobs, node_id, "serial-0") is None
    submitted = submitted_artifact_job(service, run_id, request_suffix=121)
    claim = claim_agent(agent_jobs, node_id, "serial-0")
    assert claim is not None
    assert service.get(submitted.id).supported_actions == ("stop",)
    service.cancel(
        submitted.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000123",
        reason="operator requested stop",
    )
    waiting = cancellation_result(
        claim,
        submitted,
        state="waiting-for-operator",
        reason="controller cancellation could not stop the active job",
    )
    agent_jobs.record_result(waiting)

    # Not a wait for a person: the cancel is being driven to its end.
    view = service.get(submitted.id)
    assert _cancelling(view)
    assert view.supported_actions == ()
    assert view.result_evidence is not None
    assert view.result_evidence.active_scope_may_remain
    assert view.result_evidence.elapsed_milliseconds == 10
    assert recipe_operations.preview_stop(run_id).allowed

    for _ in range(40):
        if service.get(submitted.id).state == "cancelled":
            break
        clock.advance(seconds=120)
        agent_jobs.reconcile_orders()
    ended = service.get(submitted.id)
    assert ended.state == "cancelled"
    assert ended.supported_actions == ()
    assert ended.result_evidence is not None
    assert ended.result_evidence.active_scope_may_remain is True
    assert ended.result_evidence.cancel_request_id == (
        "00000000-0000-4000-8000-000000000123"
    )
    # An ended job never blocks the run's Stop: its residue is recorded, not awaited.
    with sessions.begin() as session:
        assert (
            recipe_operations._one_shot_stop_prerequisite(session, run_id, clock.now)
            is None
        )


def test_artifact_lease_expiry_is_observed_then_reconciled_under_new_intent(tmp_path):
    from .test_artifact_job_recovery import recover_unknown_job_under_new_intent

    recover_unknown_job_under_new_intent(tmp_path, malformed=False)


def test_artifact_lease_expiry_is_stoppable_and_admits_fresh_exact_stop(
    tmp_path,
) -> None:
    """An uncertain cancellation ends and admits its fresh exact Stop."""

    sessions, recipe_operations, _queue, service, run_id, node_id = (
        running_artifact_service(tmp_path)
    )
    clock = MutableClock(NOW)
    agent_jobs = AgentJobService(sessions, clock=clock)
    recipe_operations._agent_jobs = agent_jobs
    assert claim_agent(agent_jobs, node_id, "serial-0") is None
    submitted = submitted_artifact_job(service, run_id, request_suffix=124)
    claim = claim_agent(agent_jobs, node_id, "serial-0")
    assert claim is not None

    clock.advance(seconds=31)
    assert claim_agent(agent_jobs, node_id, "serial-0") is None
    observed = service.get(submitted.id)
    # The agent can no longer report: the job is observed, and stoppable.
    assert observed.state == ajs.OBSERVING
    assert observed.supported_actions == ("stop",)
    assert observed.result_evidence is not None
    assert observed.result_evidence.failure_kind == "agent-lease-expired"
    assert observed.result_evidence.late_results_accepted is False
    with pytest.raises(Exception):  # noqa: B017 -- ending witness; effects and fresh admission establish behaviour
        agent_jobs.record_result(
            cancellation_result(
                claim,
                submitted,
                state="cancelled",
                reason="controller cancellation requested",
            )
        )
    service.cancel(
        submitted.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000156",
        reason="operator stopped the lost job",
    )
    for _ in range(40):
        if service.get(submitted.id).state == ajs.CANCELLED:
            break
        clock.advance(seconds=120)
        agent_jobs.reconcile_orders()
    ended = service.get(submitted.id)
    assert ended.state == ajs.CANCELLED
    assert ended.result_evidence is not None
    assert ended.result_evidence.active_scope_may_remain is True
    plan = recipe_operations.preview_stop(run_id)
    assert plan.allowed

    def request_key(view: ArtifactJobView | RecipeOperationView) -> str:
        if isinstance(view, ArtifactJobView):
            assert view.submit_request_id is not None
            return view.submit_request_id
        with sessions() as session:
            parent = session.get(Job, view.id)
            assert parent is not None
            return parent.request_id

    def assert_released() -> None:
        with sessions() as session:
            assert_no_orphaned_holds(session)

    def end_job(
        _view: ArtifactJobView | RecipeOperationView,
    ) -> ArtifactJobView | RecipeOperationView:
        return service.get(submitted.id)

    _ended, fresh = assert_ended_without_blocking(
        sessions,
        submitted,
        end=end_job,
        fresh=lambda _: recipe_operations.stop(
            run_id,
            plan_digest=plan.plan_digest,
            actor="operator",
            request_id="00000000-0000-4000-8000-000000000157",
        ),
        request_key=request_key,
        assert_released=assert_released,
    )
    assert fresh.id != submitted.operation_id


def test_draft_artifact_cancel_idempotency_rejects_mismatched_replay(tmp_path) -> None:
    _sessions, _operations, _queue, service, run_id, _node_id = (
        running_artifact_service(tmp_path)
    )
    job = create_artifact_job(
        service,
        **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000127"),
    )
    request = {
        "actor": "operator",
        "request_id": "00000000-0000-4000-8000-000000000128",
        "reason": "operator requested stop",
    }
    assert service.cancel(job.id, **request).state == "cancelled"
    assert service.cancel(job.id, **request).state == "cancelled"
    with pytest.raises(ArtifactJobError):
        service.cancel(job.id, **{**request, "reason": "different reason"})


def test_blob_store_stream_rejects_mismatch_oversize_and_interruption(tmp_path) -> None:
    store = ArtifactBlobStore(tmp_path / "blobs", max_stored_bytes=1024)

    async def chunks(*values: bytes):
        for value in values:
            yield value

    with pytest.raises(ArtifactBlobStoreError):
        asyncio.run(
            store.put_stream(
                "0" * 64, chunks(b"abc"), expected_bytes=3, maximum_bytes=3
            )
        )
    with pytest.raises(ArtifactBlobStoreError):
        asyncio.run(
            store.put_stream(
                hashlib.sha256(b"abcd").hexdigest(),
                chunks(b"abcd"),
                expected_bytes=3,
                maximum_bytes=3,
            )
        )

    async def interrupted():
        yield b"a"
        raise RuntimeError("connection lost")

    with pytest.raises(Exception):  # noqa: B017 -- no partial publication and fresh upload below
        asyncio.run(
            store.put_stream(
                hashlib.sha256(b"ab").hexdigest(),
                interrupted(),
                expected_bytes=2,
                maximum_bytes=2,
            )
        )
    assert not list((tmp_path / "blobs" / ".tmp").glob("*.part"))
    assert not list((tmp_path / "blobs" / ".reservations").glob("*.reserve"))
    fresh = store.put_bytes(hashlib.sha256(b"ok").hexdigest(), b"ok", maximum_bytes=2)
    assert fresh.path.read_bytes() == b"ok"


def test_blob_store_reconcile_unlinks_only_objects_with_positive_evidence(
    tmp_path,
) -> None:
    store = ArtifactBlobStore(tmp_path / "blobs", max_stored_bytes=1024)
    unproven = hashlib.sha256(b"unproven").hexdigest()
    proven = hashlib.sha256(b"proven").hexdigest()
    store.put_bytes(unproven, b"unproven", maximum_bytes=16)
    store.put_bytes(proven, b"proven", maximum_bytes=16)

    # Neither is referenced, but only one is proven unused.
    store.reconcile(set(), orphan_grace_seconds=0, reclaimable_sha256={proven})

    assert store.usage().used_bytes == len(b"unproven")


def test_blob_store_serializes_concurrent_quota_and_reconciles(tmp_path) -> None:
    first_store = ArtifactBlobStore(tmp_path / "blobs", max_stored_bytes=6)
    second_store = ArtifactBlobStore(tmp_path / "blobs", max_stored_bytes=6)

    async def exercise() -> list[StoredArtifactBlob]:
        first_streaming = asyncio.Event()
        release_first = asyncio.Event()
        second_consumed = False

        async def first_source():
            yield b"aaaa"
            first_streaming.set()
            await asyncio.wait_for(release_first.wait(), timeout=30)

        async def second_source():
            nonlocal second_consumed
            second_consumed = True
            yield b"bbbb"

        first = asyncio.create_task(
            first_store.put_stream(
                hashlib.sha256(b"aaaa").hexdigest(),
                first_source(),
                expected_bytes=4,
                maximum_bytes=4,
            )
        )
        await asyncio.wait_for(first_streaming.wait(), timeout=30)
        usage = second_store.usage()
        assert usage.model_dump() == {
            "max_stored_bytes": 6,
            "used_bytes": 0,
            "reserved_bytes": 4,
            "in_flight_uploads": 1,
            "remaining_bytes": 2,
        }
        with pytest.raises(Exception):  # noqa: B017 -- ending witness; unconsumed stream and subsequent upload below
            await second_store.put_stream(
                hashlib.sha256(b"bbbb").hexdigest(),
                second_source(),
                expected_bytes=4,
                maximum_bytes=4,
            )
        assert not second_consumed
        assert (
            sum(
                path.stat().st_size
                for path in (tmp_path / "blobs" / ".tmp").glob("*.part")
            )
            <= 4
        )
        release_first.set()
        return [await first]

    results = asyncio.run(exercise())
    assert first_store.usage().in_flight_uploads == 0
    fresh = second_store.put_bytes(
        hashlib.sha256(b"bb").hexdigest(), b"bb", maximum_bytes=2
    )
    assert fresh.path.read_bytes() == b"bb"
    assert first_store.usage().used_bytes <= 6
    survivor = next((item for item in results if not isinstance(item, Exception)), None)
    referenced = {survivor.sha256} if survivor is not None else set()
    orphan = b"x"
    orphan_digest = hashlib.sha256(orphan).hexdigest()
    if first_store.usage().remaining_bytes:
        first_store.put_bytes(orphan_digest, orphan, maximum_bytes=1)
    report = first_store.reconcile(
        referenced, orphan_grace_seconds=0, reclaimable_sha256={orphan_digest}
    )
    assert report.missing_referenced_blobs == []
    assert report.removed_orphan_blobs in {0, 1}
