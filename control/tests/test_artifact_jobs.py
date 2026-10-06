from __future__ import annotations

import asyncio
import copy
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from threading import Event
from types import SimpleNamespace
from typing import TypedDict

import pytest
from sqlalchemy import select
from vonk_agent_protocol import (
    AgentResult,
    AgentResultState,
    RecipeJobFile,
    RecipeJobInputFile,
    RecipeJobOutputLimits,
    RecipeJobRunResult,
    canonical_message,
    recipe_job_manifest_document,
    recipe_job_manifest_sha256,
)
from vonk_control import artifact_job_states as ajs
from vonk_control import recipe_operations as recipe_operations_module
from vonk_control.agent_jobs import AgentJobService, StaleAgentAttempt
from vonk_control.artifact_blob_store import (
    ArtifactBlobStore,
    ArtifactBlobStoreError,
    StoredArtifactBlob,
)
from vonk_control.artifact_jobs import (
    ArtifactJobError,
    ArtifactJobService,
    CompiledArtifactContract,
    _effective_parameters,
)
from vonk_control.bounded_json import require_mapping
from vonk_control.categorized_errors import MissingRecord
from vonk_control.compiled_artifact_contract import (
    ParameterScalar,
    validate_parameter_definition,
)
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    ArtifactJob,
    ArtifactJobBlob,
    CatalogDocumentRevision,
    Job,
    RecipeInstallation,
    RecipeRun,
)
from vonk_control.recipe_execution_contract import parse_stored_run_plan
from vonk_control.recipe_operations import (
    RecipeArtifactJobCancellationPending,
    RecipeOperationConflict,
)
from vonk_control.resource_planning import PLATFORM_MEMORY_FLOOR_BYTES
from vonk_forge_contracts import RecipeDefinition, document_sha256

from .runtime_identity_support import claim_agent
from .test_recipe_operations import (
    NOW,
    installed_recipe,
    setup_services,
)


def _cancelling(view) -> bool:
    """A cancel was requested and the job has not ended: it completes by itself."""

    return view.cancel_requested_at is not None and view.state not in ajs.ENDED


def test_one_shot_job_inherits_activation_intent(
    tmp_path,
) -> None:
    sessions, _operations, _queue, service, run_id, node_id = running_artifact_service(
        tmp_path
    )
    submitted = submitted_artifact_job(service, run_id, request_suffix=150)
    with sessions() as session:
        activation = session.scalar(
            select(Job).where(
                Job.kind == "recipe.job.activate.v1",
                Job.payload["owner_id"].as_string() == run_id,
            )
        )
        parent = session.get(Job, submitted.operation_id)
        child = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == submitted.operation_id
            )
        )
        node = session.get(AgentNode, node_id)
        assert activation is not None and parent is not None
        assert child is not None and node is not None
        ordinal = activation.payload["workload_intent_ordinal"]
        assert type(ordinal) is int and ordinal > 0
        assert parent.payload["workload_intent_ordinal"] == ordinal
        assert child.workload_intent_ordinal == ordinal
        assert node.workload_intent_ordinal == ordinal


def test_one_shot_job_rejects_submission_after_newer_workload_intent(tmp_path) -> None:
    sessions, _operations, _queue, service, run_id, node_id = running_artifact_service(
        tmp_path
    )
    with sessions.begin() as session:
        node = session.get(AgentNode, node_id)
        assert node is not None
        node.workload_intent_ordinal += 1
    with pytest.raises(RecipeOperationConflict, match="superseded"):
        submitted_artifact_job(service, run_id, request_suffix=152)


class _ArtifactCreateRequest(TypedDict):
    """The keyword arguments :meth:`ArtifactJobService.create` accepts."""

    run_id: str
    interface: str
    parameters: dict[str, ParameterScalar]
    inputs: list[dict[str, object]]
    output_limits: dict[str, object]
    timeout_seconds: int
    actor: str
    request_id: str


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _sequence(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


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
    with pytest.raises(ArtifactJobError, match="wrong type"):
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


def _configure_artifact_recipe(document: dict[str, object]) -> None:
    document["settings"] = {
        "kind": "job",
        "concurrency": None,
        "knobs": {},
    }
    document["validation"] = {
        "serving": {
            "interface": "image-job",
            "checks": [
                {
                    "name": "image-job-output",
                    "kind": "image-job.output",
                    "assertions": ["inference.completed", "artifact.output"],
                    "request": {
                        "transport": "job",
                        "fixture": "fixtures/input.png",
                        "input_slots": {},
                        "output_slot": "image",
                    },
                }
            ],
        },
    }
    document["interfaces"] = [
        {
            "adapter": "image-job",
            "input": {
                "required": True,
                "media_types": ["image/png"],
                "max_bytes": 32 * 1024**2,
            },
            "output": {
                "max_total_bytes": 4096,
                "slots": [
                    {
                        "id": "image",
                        "label": "Image",
                        "description": "Generated image",
                        "media_types": ["image/png"],
                        "extensions": [".png"],
                        "min_files": 1,
                        "max_files": 1,
                        "max_file_bytes": 1024,
                        "max_total_bytes": 4096,
                    }
                ],
            },
        }
    ]
    runtime = _mapping(document["runtime"])
    arguments = _sequence(runtime["arguments"])
    arguments.extend(
        [
            {"name": "prompt", "value": None, "setting": "prompt"},
            {"name": "seed", "value": None, "setting": "seed"},
        ]
    )
    document["settings"]["knobs"] = {
        "prompt": {"value": "", "change_effect": "restart"},
        "seed": {"value": 0, "change_effect": "restart"},
    }


def running_artifact_service(tmp_path, *, recipe_transform=None, engine=None):
    def transform(document: dict[str, object]) -> None:
        _configure_artifact_recipe(document)
        if recipe_transform is not None:
            recipe_transform(document)

    sessions, recipe_operations, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, recipe_transform=transform, engine=engine
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
    return (
        sessions,
        recipe_operations,
        queue,
        ArtifactJobService(
            sessions,
            recipe_operations=recipe_operations,
            blob_store=ArtifactBlobStore(tmp_path / "artifact-blobs"),
            clock=lambda: NOW,
        ),
        activated.owner_id,
        nodes[0],
    )


def test_activate_replay_serializes_request_key_lookup(tmp_path, monkeypatch) -> None:
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
    acquired = []
    acquire = recipe_operations_module.acquire_admission_keys

    def record_keys(session, keys):
        acquired.extend(keys)
        return acquire(session, keys)

    monkeypatch.setattr(recipe_operations_module, "acquire_admission_keys", record_keys)
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
    assert any(
        key.namespace == "job-request" and key.identity == request_id
        for key in acquired
    )
    with sessions() as session:
        assert (
            len(tuple(session.scalars(select(Job).where(Job.request_id == request_id))))
            == 1
        )


def artifact_create_request(run_id: str, request_id: str) -> _ArtifactCreateRequest:
    content = b"png"
    return {
        "run_id": run_id,
        "interface": "image-job",
        "parameters": {"prompt": "fox", "seed": 0},
        "inputs": [
            {
                "slot": "input",
                "name": "input.png",
                "media_type": "image/png",
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
        "request_id": request_id,
    }


def create_artifact_job(service: ArtifactJobService, *args, **request):
    """Create a job from plain request documents, as the HTTP route does.

    The service takes typed inputs and limits; a test describes them as plain
    documents, so the contract models are built (and validated) here.
    """

    if "inputs" in request:
        request["inputs"] = [
            item
            if isinstance(item, RecipeJobInputFile)
            else RecipeJobInputFile.model_validate(item)
            for item in request["inputs"]
        ]
    if "output_limits" in request and not isinstance(
        request["output_limits"], RecipeJobOutputLimits
    ):
        request["output_limits"] = RecipeJobOutputLimits.model_validate(
            request["output_limits"]
        )
    return service.create(*args, **request)


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def submitted_artifact_job(
    service: ArtifactJobService, run_id: str, *, request_suffix: int
):
    request = artifact_create_request(
        run_id, f"00000000-0000-4000-8000-{request_suffix:012d}"
    )
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
    return service.submit(
        job.id,
        actor="operator",
        request_id=f"00000000-0000-4000-8000-{request_suffix + 1:012d}",
    )


def cancellation_result(
    claim,
    artifact_job,
    *,
    state: str,
    reason: str,
) -> AgentResult:
    empty: tuple[RecipeJobFile, ...] = ()
    return AgentResult(
        fence=claim.fence,
        state=AgentResultState(state),
        result=RecipeJobRunResult.model_validate(
            {
                "job_id": artifact_job.id,
                "run_id": artifact_job.run_id,
                "exit_code": 130,
                "output_manifest": {
                    **recipe_job_manifest_document(empty),
                    "manifest_sha256": recipe_job_manifest_sha256(empty),
                },
                "evidence": {
                    "elapsed_milliseconds": 10,
                    "peak_memory_bytes": None,
                },
                "reason": reason,
            }
        ),
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
    with pytest.raises(KeyError):
        service.get_by_request_id("00000000-0000-4000-8000-000000000155")


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
    create_artifact_job(service, **request)
    replay = copy.deepcopy(request)
    change(replay)

    with pytest.raises(ArtifactJobError, match="request key"):
        create_artifact_job(service, **replay)


def test_artifact_job_create_rejects_replay_after_compiled_contract_drift(
    tmp_path,
) -> None:
    sessions, _operations, _queue, service, run_id, _node_id = running_artifact_service(
        tmp_path
    )
    request = artifact_create_request(run_id, "00000000-0000-4000-8000-000000000116")
    create_artifact_job(service, **request)
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

    with pytest.raises(ArtifactJobError, match="request key"):
        create_artifact_job(service, **request)


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

    with pytest.raises(ArtifactJobError, match="recipe run is not accepting jobs"):
        service.submit(
            created.id,
            actor="operator",
            request_id="00000000-0000-4000-8000-000000000120",
        )
    with sessions() as session:
        row = session.get(ArtifactJob, created.id)
        assert row is not None
        assert row.preparation == "ready"
        assert row.operation_id is None


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
                "media_type": "image/png",
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
    with pytest.raises(ArtifactJobError, match="SHA-256"):
        service.put_input(
            job.id,
            name="input.png",
            media_type="image/png",
            expected_sha256="0" * 64,
            content=input_content,
        )
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
    with pytest.raises(ArtifactJobError, match="request identity"):
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
    with pytest.raises(ArtifactJobError, match="authorized"):
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
    from vonk_control.artifact_job_api import _view

    response = _view(completed)
    assert response.result_evidence is not None
    from pydantic import ValidationError
    from vonk_control.artifact_jobs import ArtifactJobResponse

    with pytest.raises(ValidationError, match="requires output manifest"):
        ArtifactJobResponse.model_validate(
            response.model_dump() | {"output_manifest_sha256": None}
        )
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
    with pytest.raises(KeyError):
        service.result_blob(job.id, "missing.json", output_digest)
    with pytest.raises(KeyError):
        service.result_blob(job.id, "output.png", "0" * 64)
    with sessions() as session:
        run_row = session.get(RecipeRun, run_id)
        assert run_row is not None
        assert run_row.state == "running"
    with sessions.begin() as session:
        row = session.get(ArtifactJob, job.id)
        assert row is not None
        row.output_manifest_sha256 = None
    with pytest.raises(ValidationError, match="requires output manifest"):
        service.get(job.id)


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


def test_artifact_job_rejects_unsafe_names_and_timeout(tmp_path) -> None:
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
    with pytest.raises(Exception, match="name"):
        create_artifact_job(
            service,
            **request,
            inputs=[
                {
                    "slot": "input",
                    "name": "../escape",
                    "media_type": "image/png",
                    "size_bytes": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                }
            ],
            timeout_seconds=60,
        )
    with pytest.raises(ArtifactJobError, match="timeout"):
        create_artifact_job(service, **request, inputs=[], timeout_seconds=3601)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda value: value["parameters"].update(extra=True), "undeclared"),
        (lambda value: value["parameters"].update(seed="101"), "wrong type"),
        (lambda value: value["inputs"][0].update(slot="other"), "undeclared"),
        (
            lambda value: value["inputs"][0].update(media_type="image/jpeg"),
            "media type",
        ),
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
                "media_type": "image/png",
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
    with pytest.raises(ArtifactJobError, match=message):
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
    def transform(document: dict[str, object]) -> None:
        interface = _mapping(_sequence(document["interfaces"])[0])
        output = _mapping(interface["output"])
        slot = _mapping(_sequence(output["slots"])[0])
        slot["media_types"] = ["image/avif", "image/png"]
        slot["extensions"] = [".avif", ".png"]

    _sessions, _operations, _queue, service, run_id, _node_id = (
        running_artifact_service(tmp_path, recipe_transform=transform)
    )

    with pytest.raises(ArtifactJobError, match="output slot contract"):
        create_artifact_job(
            service,
            **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000132"),
        )


def test_artifact_job_rejects_cross_slot_output_extension_collision(tmp_path) -> None:
    def transform(document: dict[str, object]) -> None:
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

    _sessions, _operations, _queue, service, run_id, _node_id = (
        running_artifact_service(tmp_path, recipe_transform=transform)
    )

    with pytest.raises(ArtifactJobError, match="extensions"):
        create_artifact_job(
            service,
            **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000133"),
        )


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
                    "media_type": "image/png",
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
    with pytest.raises(ArtifactJobError, match="owns this run reservation"):
        service.submit(
            second.id,
            actor="operator",
            request_id="00000000-0000-4000-8000-000000000110",
        )


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
    directive = agent_jobs.heartbeat(claim, {"phase": "running"}, 30)
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
    with pytest.raises(RecipeArtifactJobCancellationPending) as pending:
        recipe_operations.stop(
            run_id,
            plan_digest=stop_plan.plan_digest,
            actor="operator",
            request_id="00000000-0000-4000-8000-000000000154",
            workload_intent_ordinal=ordinal,
        )
    assert pending.value.job_id == submitted.operation_id
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
    with pytest.raises(StaleAgentAttempt):
        agent_jobs.record_result(acknowledged)


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
    assert {
        "failure_kind": "cancellation-stop-uncertain",
        "recoverable": True,
        "active_scope_may_remain": True,
        "elapsed_milliseconds": 10,
    }.items() <= view.result_evidence.model_dump(exclude_none=True).items()
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
    assert ended.result_evidence.failure_kind == "cancellation-stop-uncertain"
    assert ended.result_evidence.cancel_request_id == (
        "00000000-0000-4000-8000-000000000123"
    )
    # An ended job never blocks the run's Stop: its residue is recorded, not awaited.
    with sessions.begin() as session:
        assert (
            recipe_operations._one_shot_stop_prerequisite(session, run_id, clock.now)
            is None
        )


def test_artifact_lease_expiry_is_observed_then_stoppable(tmp_path) -> None:
    """A lapsed one-shot job is observed, then waits only with Stop to offer."""

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
    with pytest.raises(StaleAgentAttempt):
        agent_jobs.record_result(
            cancellation_result(
                claim,
                submitted,
                state="cancelled",
                reason="controller cancellation requested",
            )
        )
    for _ in range(40):
        if service.get(submitted.id).state == ajs.NEEDS_OPERATOR:
            break
        clock.advance(seconds=120)
        agent_jobs.reconcile_orders()
    waiting = service.get(submitted.id)
    assert waiting.state == ajs.NEEDS_OPERATOR
    assert waiting.supported_actions == ("stop",)

    # Stop completes it: cancelled, the effect unknown, the residue recorded.
    service.cancel(
        submitted.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000156",
        reason="operator stopped the lost job",
    )
    for _ in range(40):
        if service.get(submitted.id).state == "cancelled":
            break
        clock.advance(seconds=120)
        agent_jobs.reconcile_orders()
    ended = service.get(submitted.id)
    assert ended.state == "cancelled"
    assert ended.result_evidence is not None
    assert ended.result_evidence.active_scope_may_remain is True


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
    with pytest.raises(ArtifactJobError, match="request key"):
        service.cancel(job.id, **{**request, "reason": "different reason"})


def test_blob_store_stream_rejects_mismatch_oversize_and_interruption(tmp_path) -> None:
    store = ArtifactBlobStore(tmp_path / "blobs", max_stored_bytes=1024)

    async def chunks(*values: bytes):
        for value in values:
            yield value

    with pytest.raises(ArtifactBlobStoreError, match="SHA-256"):
        asyncio.run(
            store.put_stream(
                "0" * 64, chunks(b"abc"), expected_bytes=3, maximum_bytes=3
            )
        )
    with pytest.raises(ArtifactBlobStoreError, match="declared size"):
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

    with pytest.raises(RuntimeError, match="connection lost"):
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
            await release_first.wait()

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
        await first_streaming.wait()
        usage = second_store.usage()
        assert usage.model_dump() == {
            "max_stored_bytes": 6,
            "used_bytes": 0,
            "reserved_bytes": 4,
            "in_flight_uploads": 1,
            "remaining_bytes": 2,
        }
        with pytest.raises(ArtifactBlobStoreError, match="quota"):
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
                "media_type": "image/png",
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
        stored.completed_at = NOW - timedelta(days=8)
    report = service.reconcile_storage()
    assert report.expired_jobs == 1
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


def test_reconcile_reclaims_only_the_expired_jobs_own_blobs(tmp_path) -> None:
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
    service.cancel(
        job.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000151",
        reason="test complete",
    )
    with sessions.begin() as session:
        stored = session.get(ArtifactJob, job.id)
        assert stored is not None
        stored.completed_at = NOW - timedelta(days=8)
    # Remove the orphan grace so an unguarded store would unlink immediately.
    expired_path = next(root.glob(f"*/{expired_digest}"))
    os.utime(expired_path, (0, 0))
    os.utime(seeded.path, (0, 0))

    report = service.reconcile_storage()

    assert report.expired_jobs == 1
    assert report.removed_blob_records == 1
    assert not expired_path.exists()
    assert seeded.path.is_file()
    with sessions() as session:
        assert session.get(ArtifactJobBlob, expired_digest) is None
        assert session.get(ArtifactJobBlob, unproven_digest) is not None


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
        assert release_attach.wait(timeout=2)
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
        assert attach_entered.wait(timeout=2)
        gc = workers.submit(gc_service.reconcile_storage)
        assert gc_started.wait(timeout=2)
        assert gc.done() is False
        assert seeded.path.is_file()
        release_attach.set()
        assert upload.result(timeout=2).id == job.id
        report = gc.result(timeout=2)

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
def test_artifact_input_manifest_round_trip_rejects_corrupt_stored_record(
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
    # Nothing re-derives the declared inputs of a draft with no uploads: the job
    # reads as not found and cannot be finalized, with the damage recorded.
    with pytest.raises(MissingRecord):
        service.get(created.id)
    with pytest.raises(ArtifactJobError, match="inputs are incomplete"):
        service.finalize(created.id)


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
