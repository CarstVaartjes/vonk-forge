"""Artifact jobs: create contracts."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from vonk_forge_contracts import RecipeDefinition, document_sha256
from vonk_forge_contracts.memory_admission import PLATFORM_MEMORY_FLOOR_BYTES

from vonk_agent_protocol.contracts import AgentOperation as AgentOperationKind
from vonk_agent_protocol.contracts import canonical_message
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_agent_protocol.recipe_jobs import (
    RecipeJobFile,
    RecipeJobOutputLimits,
)
from vonk_agent_protocol.recipe_jobs import (
    manifest_document as recipe_job_manifest_document,
)
from vonk_agent_protocol.recipe_jobs import (
    manifest_sha256 as recipe_job_manifest_sha256,
)
from vonk_agent_protocol.wire_model import OperationProgress
from vonk_control.agent_jobs.contracts import (
    _WORKLOAD_INTENT_OPERATIONS,
    fencing_intent,
)
from vonk_control.artifact_jobs import states as ajs
from vonk_control.artifact_jobs.contracts import (
    ArtifactJobError,
    _effective_parameters,
)
from vonk_control.bounded_json import require_mapping
from vonk_control.compiled_artifact_contract import (
    CompiledArtifactContract,
    validate_parameter_definition,
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
from vonk_control.recipe_operations.constants import _WORKLOAD_INTENT_KINDS
from vonk_control.recipe_operations.errors import (
    RecipeArtifactJobCancellationPending,
    RecipeRequestInvalid,
)
from vonk_control.recipe_operations.execution_contract import parse_stored_run_plan
from vonk_control.workload_intent import allocate_workload_intent

from .artifact_jobs_support import (
    _agent_queue,
    _cancelling,
    _configure_artifact_recipe,
    _mapping,
    _sequence,
    artifact_create_request,
    cancellation_result,
    create_artifact_job,
    finalized_artifact_job,
    running_artifact_service,
    submitted_artifact_job,
)
from .recipe_operations_support import NOW, installed_recipe, setup_services
from .runtime_identity_support import claim_agent


def test_a_jobrun_is_never_fenced_by_its_sparks_workload_intent() -> None:
    """Guard: a JobRun belongs to its run, never to its Spark's intent.

    Wrong implementation caught: a JobRun kind back in either intent set, or
    an intent fence decided by the stored ordinal instead of the kind (which
    would cancel a JobRun an older Controller stamped with an ordinal).
    """

    job_run = AgentOperationKind.RECIPE_JOB_RUN.value
    assert job_run not in _WORKLOAD_INTENT_OPERATIONS
    assert job_run not in _WORKLOAD_INTENT_KINDS
    assert (
        fencing_intent(AgentOperation(kind=job_run, workload_intent_ordinal=3)) is None
    )


def _second_job_run(recipe_operations, run_id: str) -> str:
    """Activate another job run of the same installation on the same Spark."""

    with recipe_operations._sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        installation_id = run.installation_id
    plan = recipe_operations.preview_run(installation_id, "image-job")
    return recipe_operations.activate_job_run(
        plan,
        plan_digest=plan.plan_digest,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000190",
    ).owner_id


@pytest.mark.parametrize("stamped_by_older_controller", [False, True])
def test_replacing_a_run_never_cancels_another_runs_job_on_its_spark(
    tmp_path, stamped_by_older_controller: bool
) -> None:
    """A load that replaces run A leaves run B's running job and new submits alone.

    Before: the load's new intent on the Spark cancelled B's job and refused
    every later submit on B as superseded until B was restarted.
    """

    sessions, recipe_operations, _queue, service, run_a, node_id = (
        running_artifact_service(tmp_path)
    )
    run_b = _second_job_run(recipe_operations, run_a)
    agent_jobs = _agent_queue(sessions, recipe_operations, service)
    assert claim_agent(agent_jobs, node_id, "serial-0") is None
    job_b = submitted_artifact_job(service, run_b, request_suffix=170)
    if stamped_by_older_controller:
        # Enqueued before the upgrade: the order carries the Spark's ordinal.
        with sessions.begin() as session:
            node = session.get(AgentNode, node_id)
            order = session.scalar(
                select(AgentOperation).where(
                    AgentOperation.parent_job_id == job_b.operation_id
                )
            )
            assert node is not None
            assert order is not None
            order.workload_intent_ordinal = node.workload_intent_ordinal
    claim = claim_agent(agent_jobs, node_id, "serial-0")
    assert claim is not None

    # The load replacing A: a new intent on the Spark, then A's Stop under it.
    with sessions.begin() as session:
        ordinal = allocate_workload_intent(session, (node_id,))
        agent_jobs.request_superseded_workload_cancellation_in_session(
            session, (node_id,), ordinal, NOW
        )
    stop_plan = recipe_operations.preview_stop(run_a)
    stopped = recipe_operations.stop(
        run_a,
        plan_digest=stop_plan.plan_digest,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000172",
        workload_intent_ordinal=ordinal,
    )
    assert stopped.state == "succeeded"

    directive = agent_jobs.heartbeat(claim, OperationProgress(phase="running"), 30)
    assert directive.cancel_requested is False
    assert service.get(job_b.id).state == ajs.RUNNING
    later = service.submit(
        finalized_artifact_job(service, run_b, request_suffix=173).id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000174",
    )
    assert later.operation_id is not None
    assert later.state == LifecycleState.QUEUED


def test_a_run_stop_cancels_exactly_its_own_jobs(tmp_path) -> None:
    """Stopping run A cancels A's live job directly and leaves run B's alone."""

    sessions, recipe_operations, _queue, service, run_a, node_id = (
        running_artifact_service(tmp_path)
    )
    run_b = _second_job_run(recipe_operations, run_a)
    agent_jobs = _agent_queue(sessions, recipe_operations, service)
    assert claim_agent(agent_jobs, node_id, "serial-0") is None
    job_a = submitted_artifact_job(service, run_a, request_suffix=176)
    claim_a = claim_agent(agent_jobs, node_id, "serial-0")
    assert claim_a is not None
    job_b = submitted_artifact_job(service, run_b, request_suffix=178)

    stop_plan = recipe_operations.preview_stop(run_a)

    def stop():
        return recipe_operations.stop(
            run_a,
            plan_digest=stop_plan.plan_digest,
            actor="operator",
            request_id="00000000-0000-4000-8000-000000000180",
        )

    with pytest.raises(RecipeArtifactJobCancellationPending):
        stop()
    directive = agent_jobs.heartbeat(claim_a, OperationProgress(phase="running"), 30)
    assert directive.cancel_requested is True
    assert _cancelling(service.get(job_a.id))
    # A's accepted Stop refuses A's later submits; B is untouched.
    with pytest.raises(RecipeRequestInvalid):
        submitted_artifact_job(service, run_a, request_suffix=181)
    assert service.get(job_b.id).state == ajs.QUEUED
    assert submitted_artifact_job(service, run_b, request_suffix=183).operation_id

    agent_jobs.record_result(
        cancellation_result(
            claim_a,
            job_a,
            state="cancelled",
            reason="controller cancellation requested",
        )
    )
    assert service.get(job_a.id).state == ajs.CANCELLED
    assert stop().state == "succeeded"
    claim_b = claim_agent(agent_jobs, node_id, "serial-0")
    assert claim_b is not None
    later = service.submit(
        finalized_artifact_job(service, run_b, request_suffix=186).id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000187",
    )
    assert later.state == LifecycleState.QUEUED
    assert json.loads(canonical_message(claim_b.payload))["run_id"] == run_b


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_artifact_float_settings_require_finite_values(value: float) -> None:
    with pytest.raises((TypeError, ValueError)):
        validate_parameter_definition(
            {
                "name": "guidance",
                "type": "float",
                "default": value,
                "minimum": 0.0,
                "maximum": 64.0,
            }
        )


def test_artifact_settings_preserve_strict_float_and_max64_name_contract() -> None:
    definition = validate_parameter_definition(
        {
            "name": "guidance_scale",
            "type": "float",
            "default": 1.5,
            "minimum": 0.0,
            "maximum": 64.0,
        }
    )
    assert _effective_parameters([definition], {"guidance_scale": 2.25}) == {
        "guidance_scale": 2.25
    }
    with pytest.raises(ArtifactJobError):
        _effective_parameters([definition], {"guidance_scale": float("nan")})

    with pytest.raises((TypeError, ValueError)):
        validate_parameter_definition(
            {
                "name": "a" * 65,
                "type": "float",
                "default": 1.0,
                "minimum": 0.0,
                "maximum": 64.0,
            }
        )
    for name in (
        "apiKey",
        "accessToken",
        "privateKey",
        "passwordHash",
        "hf_token",
        "github_token",
    ):
        with pytest.raises((TypeError, ValueError)):
            validate_parameter_definition(
                {"name": name, "type": "string", "default": "secret"}
            )
    for name in ("max_tokens", "token_budget", "tokenizer"):
        definition = validate_parameter_definition(
            {"name": name, "type": "integer", "default": 1}
        )
        assert definition.name == name


def test_activation_parent_carries_the_saved_runs_plan_digest_when_admission_replans(
    tmp_path,
) -> None:
    sessions, recipe_operations, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, recipe_transform=_configure_artifact_recipe
    )
    installation = installed_recipe(
        recipe_operations,
        mapping_id,
        build_id,
        nodes,
        request_id="00000000-0000-4000-8000-000000000101",
    )
    plan = recipe_operations.preview_run(installation.owner_id, "image-job")
    real_plan_run = recipe_operations._run_admission.plan_run
    calls: list[str] = []

    def replanning(*args, **kwargs):
        fresh = real_plan_run(*args, **kwargs)
        calls.append(fresh.plan_digest)
        if len(calls) == 1:
            return fresh
        return dataclasses.replace(fresh, plan_digest="d" * 64)

    recipe_operations._run_admission.plan_run = replanning
    activated = recipe_operations.activate_job_run(
        plan,
        plan_digest=plan.plan_digest,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000102",
    )

    with sessions() as session:
        run = session.get(RecipeRun, activated.owner_id)
        job = session.get(Job, activated.id)
        assert run is not None
        assert job is not None
        assert run.plan_digest == "d" * 64
        assert job.payload["plan_digest"] == run.plan_digest


def test_activate_replay_uses_no_request_advisory_key(tmp_path, monkeypatch) -> None:
    sessions, recipe_operations, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, recipe_transform=_configure_artifact_recipe
    )
    installation = installed_recipe(
        recipe_operations,
        mapping_id,
        build_id,
        nodes,
        request_id="00000000-0000-4000-8000-000000000101",
    )
    plan = recipe_operations.preview_run(installation.owner_id, "image-job")

    request_id = "00000000-0000-4000-8000-000000000102"
    statements = []

    def record_sql(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(sessions.kw["bind"], "before_cursor_execute", record_sql)
    # Force the race window's fast read to miss, as it can for concurrent callers.
    monkeypatch.setattr(
        recipe_operations, "_idempotent", lambda *_args, **_kwargs: None
    )
    activated = recipe_operations.activate_job_run(
        plan,
        plan_digest=plan.plan_digest,
        actor="operator",
        request_id=request_id,
    )
    replay = recipe_operations.activate_job_run(
        plan,
        plan_digest=plan.plan_digest,
        actor="operator",
        request_id=request_id,
    )

    assert replay == activated
    assert not any("job-request" in statement for statement in statements)
    assert any(
        "ON CONFLICT" in statement and "jobs" in statement for statement in statements
    )
    event.remove(sessions.kw["bind"], "before_cursor_execute", record_sql)
    with sessions() as session:
        assert (
            len(tuple(session.scalars(select(Job).where(Job.request_id == request_id))))
            == 1
        )


def test_artifact_job_create_idempotency_compares_canonical_semantics(
    tmp_path,
) -> None:
    _sessions, _operations, _queue, service, run_id, _node_id = (
        running_artifact_service(tmp_path)
    )
    request = artifact_create_request(run_id, "00000000-0000-4000-8000-000000000114")
    first = create_artifact_job(service, **request)

    replay = copy.deepcopy(request)
    replay["parameters"] = {"seed": 0, "prompt": "fox"}
    replayed = create_artifact_job(service, **replay)

    assert replayed.id == first.id


def test_artifact_job_create_request_lookup_recovers_the_original_draft(
    tmp_path,
) -> None:
    _sessions, _operations, _queue, service, run_id, _node_id = (
        running_artifact_service(tmp_path)
    )
    request = artifact_create_request(run_id, "00000000-0000-4000-8000-000000000154")
    created = create_artifact_job(service, **request)

    recovered = service.get_by_request_id(request["request_id"])

    assert recovered.id == created.id
    assert recovered.preparation == "draft"
    with pytest.raises(Exception):  # noqa: B017 -- ending witness; effects and fresh admission establish behaviour
        service.get_by_request_id("00000000-0000-4000-8000-000000000155")
    fresh = create_artifact_job(
        service,
        **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000155"),
    )
    assert fresh.id != created.id
    assert service.get_by_request_id(request["request_id"]).id == created.id


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.update(interface="audio-job"),
        lambda value: value.update(parameters={}),
        lambda value: value.update(inputs=[]),
        lambda value: value["inputs"][0].update(name="other.png"),
        lambda value: value["inputs"][0].update(sha256="0" * 64),
        lambda value: value.update(
            output_limits={
                "max_files": 1,
                "max_file_bytes": 1024,
                "max_total_bytes": 2048,
                "allowed_media_types": ["image/png"],
            }
        ),
        lambda value: value.update(timeout_seconds=3599),
    ],
)
def test_artifact_job_create_rejects_semantically_different_replay(
    tmp_path, change
) -> None:
    _sessions, _operations, _queue, service, run_id, _node_id = (
        running_artifact_service(tmp_path)
    )
    request = artifact_create_request(run_id, "00000000-0000-4000-8000-000000000115")
    original = create_artifact_job(service, **request)
    replay = copy.deepcopy(request)
    change(replay)

    with pytest.raises(Exception):  # noqa: B017 -- ending witness; effects and fresh admission establish behaviour
        create_artifact_job(service, **replay)
    assert create_artifact_job(service, **request).id == original.id
    fresh = create_artifact_job(
        service,
        **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000124"),
    )
    assert fresh.id != original.id


def test_artifact_job_contract_drift_ends_preparation_and_admits_fresh_intent(
    tmp_path,
) -> None:
    sessions, _operations, _queue, service, run_id, _node_id = running_artifact_service(
        tmp_path
    )
    request = artifact_create_request(run_id, "00000000-0000-4000-8000-000000000116")
    original = create_artifact_job(service, **request)
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        installation = session.get(RecipeInstallation, run.installation_id)
        assert installation is not None
        revision = session.get(CatalogDocumentRevision, installation.recipe_revision_id)
        assert revision is not None
        document = copy.deepcopy(revision.document)
        settings = _mapping(document["settings"])
        knobs = _mapping(settings["knobs"])
        seed = _mapping(knobs["seed"])
        seed["value"] = 99
        parsed = RecipeDefinition.model_validate(document)
        replacement = CatalogDocumentRevision(
            document_id=revision.document_id,
            kind=revision.kind,
            publisher=revision.publisher,
            slug=revision.slug,
            revision_number=revision.revision_number + 1,
            schema_version=2,
            state="active",
            document=parsed.model_dump(mode="json"),
            content_digest=document_sha256(parsed.model_dump(mode="json")),
            projected={},
            created_by="admin",
            created_at=revision.created_at,
        )
        session.add(replacement)
        session.flush()
        installation.recipe_revision_id = replacement.id

    ended = create_artifact_job(service, **request)
    assert ended.id == original.id
    assert ended.state in ajs.ENDED
    assert ended.operation_id is None
    # A changed local revision pointer does not authorize different content on
    # the running installation. Restore its accepted recipe evidence before
    # proving that the ended draft leaves fresh submission admissible.
    with sessions.begin() as session:
        installation = session.get(RecipeInstallation, installation.id)
        assert installation is not None
        installation.recipe_revision_id = revision.id
    fresh = submitted_artifact_job(service, run_id, request_suffix=160)
    assert fresh.id != original.id
    assert fresh.operation_id is not None


@pytest.mark.usefixtures("damaged_json_rows")
def test_artifact_job_damaged_contract_is_rebuilt_from_its_recipe_before_projection(
    tmp_path,
) -> None:
    sessions, _operations, _queue, service, run_id, _node_id = running_artifact_service(
        tmp_path
    )
    request = artifact_create_request(run_id, "00000000-0000-4000-8000-000000000118")
    created = create_artifact_job(service, **request)
    assert isinstance(created.compiled_contract, CompiledArtifactContract)

    with sessions.begin() as session:
        row = session.get(ArtifactJob, created.id)
        assert row is not None
        row.compiled_contract = {"schema_version": 1}

    # A damaged stored contract is rebuilt from the recipe revision it was
    # compiled from, and only accepted when it has the digest the job was
    # created under.
    rebuilt = service.get(created.id)
    assert rebuilt.compiled_contract == created.compiled_contract
    assert rebuilt.contract_sha256 == created.contract_sha256


def test_artifact_job_persisted_parameters_are_validated_before_compilation(
    tmp_path,
) -> None:
    sessions, _operations, _queue, service, run_id, _node_id = running_artifact_service(
        tmp_path
    )
    request = artifact_create_request(run_id, "00000000-0000-4000-8000-000000000119")
    created = create_artifact_job(service, **request)
    content = b"png"
    digest = hashlib.sha256(content).hexdigest()
    service.put_input(
        created.id,
        name="input.png",
        media_type="image/png",
        expected_sha256=digest,
        content=content,
    )
    service.finalize(created.id)
    with sessions.begin() as session:
        row = session.get(ArtifactJob, created.id)
        assert row is not None
        row.parameters = {"prompt": "fox", "seed": "0"}

    ended = service.submit(
        created.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000120",
    )
    assert ended.state in ajs.ENDED
    assert ended.operation_id is None
    with sessions() as session:
        row = session.get(ArtifactJob, created.id)
        assert row is not None
        assert row.state in ajs.ENDED
        assert row.operation_id is None
    fresh = submitted_artifact_job(service, run_id, request_suffix=122)
    assert fresh.operation_id is not None


def test_artifact_job_create_exact_concurrent_replay_has_one_identity(
    tmp_path,
) -> None:
    _sessions, _operations, _queue, service, run_id, _node_id = (
        running_artifact_service(tmp_path)
    )
    request = artifact_create_request(run_id, "00000000-0000-4000-8000-000000000117")

    with ThreadPoolExecutor(max_workers=2) as workers:
        identifiers = tuple(
            workers.map(
                lambda _index: create_artifact_job(service, **request).id, range(2)
            )
        )

    assert len(set(identifiers)) == 1


def test_artifact_job_persists_and_selects_outputs_by_name_and_digest(tmp_path) -> None:
    def configure_recipe(document: dict[str, object]) -> None:
        topology = _mapping(document["topology"])
        roles = _sequence(topology["roles"])
        entrypoint = _mapping(roles[0])
        resources = _mapping(entrypoint["resources"])
        memory = _mapping(resources["memory"])
        memory["reserve_bytes"] = 107
        interfaces = _sequence(document["interfaces"])
        image_interface = _mapping(interfaces[0])
        output = _mapping(image_interface["output"])
        slots = _sequence(output["slots"])
        slots.append(
            {
                "id": "metadata",
                "label": "Metadata",
                "description": "Generated metadata",
                "media_types": ["application/json"],
                "extensions": [".json"],
                "min_files": 1,
                "max_files": 1,
                "max_file_bytes": 1024,
                "max_total_bytes": 4096,
            }
        )

    sessions, _recipe_operations, queue, service, run_id, node_id = (
        running_artifact_service(tmp_path, recipe_transform=configure_recipe)
    )
    input_content = b"png"
    input_digest = hashlib.sha256(input_content).hexdigest()
    job = create_artifact_job(
        service,
        run_id,
        interface="image-job",
        parameters={"prompt": "fox / meadow", "seed": 0},
        inputs=[
            {
                "slot": "input",
                "name": "input.png",
                "size_bytes": len(input_content),
                "sha256": input_digest,
            }
        ],
        output_limits={
            "max_files": 2,
            "max_file_bytes": 1024,
            "max_total_bytes": 4096,
            "allowed_media_types": ["application/json", "image/png"],
        },
        timeout_seconds=3600,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000103",
    )
    assert job.preparation == "draft"
    with pytest.raises(ArtifactJobError):
        service.put_input(
            job.id,
            name="input.png",
            media_type="image/png",
            expected_sha256="0" * 64,
            content=input_content,
        )
    assert service.get(job.id).input_files == ()
    service.put_input(
        job.id,
        name="input.png",
        media_type="image/png",
        expected_sha256=input_digest,
        content=input_content,
    )
    assert service.finalize(job.id).preparation == "ready"
    submitted = service.submit(
        job.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000104",
    )
    assert submitted.state == "queued"
    notifications_after_submit = queue.available
    replayed = service.submit(
        job.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000104",
    )
    assert replayed.operation_id == submitted.operation_id
    with pytest.raises(Exception):  # noqa: B017 -- ending witness; effects and fresh admission establish behaviour
        service.submit(
            job.id,
            actor="operator",
            request_id="00000000-0000-4000-8000-000000000105",
        )
    assert replayed.submit_request_id == "00000000-0000-4000-8000-000000000104"
    assert queue.available == notifications_after_submit
    assert queue.available > 0
    with sessions() as session:
        operation = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == submitted.operation_id
            )
        )
        assert operation is not None
        assert operation.kind == "recipe.job.run.v1"
        placement = require_mapping(
            require_mapping(
                require_mapping(
                    operation.payload["compiled_execution_plan"], "compiled plan"
                )["runtime"],
                "compiled runtime",
            )["placement"],
            "compiled placement",
        )
        assert placement["reserved_memory_bytes"] == 225
        run = session.get(RecipeRun, run_id)
        assert run is not None
        planned_node = next(
            item
            for item in parse_stored_run_plan(run.plan).nodes
            if item.node_id == node_id
        )
        assert placement["reserved_memory_bytes"] == planned_node.required_memory_bytes
        planned_floor = planned_node.memory_floor_bytes
        # The recipe's 107-byte reserve is informational; the platform floor applies.
        assert planned_floor == 50
        # The compiled launch carries the platform floor, not the admission
        # service's test override.
        assert placement["memory_floor_bytes"] == PLATFORM_MEMORY_FLOOR_BYTES
        assert operation.payload["input_manifest_sha256"] == job.input_manifest_sha256
        compiled_plan = _mapping(operation.payload["compiled_execution_plan"])
        plan_runtime = _mapping(compiled_plan["runtime"])
        plan_placement = _mapping(plan_runtime["placement"])
        assert plan_placement["memory_floor_bytes"] == PLATFORM_MEMORY_FLOOR_BYTES
        assert "fox / meadow" in _sequence(plan_runtime["argv"])
        assert operation.payload["output_mappings"] == [
            {
                "slot": "image",
                "media_type": "image/png",
                "extensions": [".png"],
            },
            {
                "slot": "metadata",
                "media_type": "application/json",
                "extensions": [".json"],
            },
        ]
    input_path, input_media_type, input_size = service.input_blob(
        job.id, input_digest, node_id=node_id
    )
    assert (input_path.read_bytes(), input_media_type, input_size) == (
        input_content,
        "image/png",
        3,
    )
    with pytest.raises(Exception):  # noqa: B017 -- ending witness; effects and fresh admission establish behaviour
        service.input_blob(job.id, input_digest, node_id="spk_" + "f" * 32)

    output_content = b"{}"
    output_digest = hashlib.sha256(output_content).hexdigest()
    service.put_output(
        job.id,
        node_id=node_id,
        name="output.png",
        media_type="image/png",
        expected_sha256=output_digest,
        content=output_content,
    )
    service.put_output(
        job.id,
        node_id=node_id,
        name="metadata.json",
        media_type="application/json",
        expected_sha256=output_digest,
        content=output_content,
    )
    image_output = RecipeJobFile(
        name="output.png",
        media_type="image/png",
        size_bytes=len(output_content),
        sha256=output_digest,
    )
    metadata_output = RecipeJobFile(
        name="metadata.json",
        media_type="application/json",
        size_bytes=len(output_content),
        sha256=output_digest,
    )
    outputs = (metadata_output, image_output)
    result = {
        "job_id": job.id,
        "run_id": run_id,
        "exit_code": 0,
        "output_manifest": {
            **recipe_job_manifest_document(outputs),
            "manifest_sha256": recipe_job_manifest_sha256(outputs),
        },
        "evidence": {"elapsed_milliseconds": 1234, "peak_memory_bytes": None},
    }
    with sessions.begin() as session:
        stored_operation = session.get(AgentOperation, operation.id)
        assert stored_operation is not None
        service.consume_agent_result(
            session,
            stored_operation,
            object(),
            SimpleNamespace(state="succeeded", result=result),
        )
    completed = service.get(job.id)
    assert completed.state == "succeeded"
    assert completed.output_manifest_sha256 == recipe_job_manifest_sha256(outputs)
    from vonk_control.artifact_jobs.api import _view

    response = _view(completed)
    assert response.result_evidence is not None
    from vonk_control.artifact_jobs.contracts import ArtifactJobResponse

    degraded = ArtifactJobResponse.model_validate(
        response.model_dump() | {"output_manifest_sha256": None}
    )
    assert degraded.id == response.id
    assert degraded.output_files == response.output_files
    image_path, image_media_type, image_name, image_size = service.result_blob(
        job.id, "output.png", output_digest
    )
    metadata_path, metadata_media_type, metadata_name, metadata_size = (
        service.result_blob(job.id, "metadata.json", output_digest)
    )
    assert (image_path.read_bytes(), image_media_type, image_name, image_size) == (
        output_content,
        "image/png",
        "output.png",
        len(output_content),
    )
    assert (
        metadata_path.read_bytes(),
        metadata_media_type,
        metadata_name,
        metadata_size,
    ) == (output_content, "application/json", "metadata.json", len(output_content))
    assert image_path == metadata_path
    with pytest.raises(Exception):  # noqa: B017 -- ending witness; effects and fresh admission establish behaviour
        service.result_blob(job.id, "missing.json", output_digest)
    with pytest.raises(Exception):  # noqa: B017 -- ending witness; effects and fresh admission establish behaviour
        service.result_blob(job.id, "output.png", "0" * 64)
    with sessions() as session:
        run_row = session.get(RecipeRun, run_id)
        assert run_row is not None
        assert run_row.state == "running"
    with sessions.begin() as session:
        row = session.get(ArtifactJob, job.id)
        assert row is not None
        row.output_manifest_sha256 = None
    recovered = service.get(job.id)
    assert recovered.id == job.id
    assert recovered.output_files == completed.output_files
    fresh = submitted_artifact_job(
        service,
        run_id,
        request_suffix=160,
        output_limits=RecipeJobOutputLimits(
            max_files=2,
            max_file_bytes=1024,
            max_total_bytes=4096,
            allowed_media_types=("application/json", "image/png"),
        ),
    )
    assert fresh.operation_id is not None
