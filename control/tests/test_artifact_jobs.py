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

import pytest
from sqlalchemy import select
from vonk_agent_protocol import (
    AgentResult,
    RecipeJobFile,
    recipe_job_manifest_document,
    recipe_job_manifest_sha256,
)
from vonk_control.agent_jobs import AgentJobService, StaleAgentAttempt
from vonk_control.artifact_blob_store import ArtifactBlobStore, ArtifactBlobStoreError
from vonk_control.artifact_jobs import (
    ArtifactJobError,
    ArtifactJobService,
    _effective_parameters,
    _validate_parameter_definition,
)
from vonk_control.models import (
    AgentOperation,
    ArtifactJob,
    ArtifactJobBlob,
    CatalogDocumentRevision,
    RecipeInstallation,
    RecipeRun,
)
from vonk_forge_contracts import RecipeDefinition, content_sha256

from .runtime_identity_support import claim_agent
from .test_recipe_operations import (
    NOW,
    installed_recipe,
    setup_services,
)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_artifact_float_settings_require_finite_values(value: float) -> None:
    with pytest.raises(ArtifactJobError, match="contract"):
        _validate_parameter_definition(
            {
                "name": "guidance",
                "type": "float",
                "default": value,
                "minimum": 0.0,
                "maximum": 64.0,
            }
        )


def test_artifact_settings_preserve_strict_float_and_max64_name_contract() -> None:
    definition = _validate_parameter_definition(
        {
            "name": "guidance_scale",
            "type": "float",
            "default": 1.5,
            "minimum": 0.0,
            "maximum": 64.0,
        }
    )
    assert _effective_parameters(
        {"parameters": [definition]}, {"guidance_scale": 2.25}
    ) == {"guidance_scale": 2.25}
    with pytest.raises(ArtifactJobError, match="wrong type"):
        _effective_parameters(
            {"parameters": [definition]}, {"guidance_scale": float("nan")}
        )

    with pytest.raises(ArtifactJobError, match="contract"):
        _validate_parameter_definition(
            {
                "name": "a" * 65,
                "type": "float",
                "default": 1.0,
                "minimum": 0.0,
                "maximum": 64.0,
            }
        )
    for name in ("apiKey", "accessToken", "privateKey", "passwordHash", "hf_token"):
        with pytest.raises(ArtifactJobError, match="contract"):
            _validate_parameter_definition(
                {"name": name, "type": "string", "default": "secret"}
            )
    for name in ("max_tokens", "token_budget", "tokenizer"):
        definition = _validate_parameter_definition(
            {"name": name, "type": "integer", "default": 1}
        )
        assert definition["name"] == name


def _configure_artifact_recipe(document: dict[str, object]) -> None:
    document["settings"] = {
        "kind": "job",
        "concurrency": None,
        "knobs": {},
    }
    document["validation"] = {
        "benchmarks": [],
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
                        "input_path": "/inputs",
                        "input_slots": {},
                        "output_path": "/outputs",
                        "output_slot": "image",
                    },
                }
            ],
        },
    }
    document["interfaces"] = [
        {
            "adapter": "image-job",
            "path": "/outputs",
            "input": {
                "path": "/inputs",
                "required": True,
                "media_types": ["image/png"],
                "max_bytes": 32 * 1024**2,
            },
            "output": {
                "path": "/outputs",
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
    document["settings"]["knobs"] = {
        "prompt": {"value": "", "change_effect": "restart"},
        "seed": {"value": 0, "change_effect": "restart"},
    }


def running_artifact_service(tmp_path, *, recipe_transform=None):
    def transform(document: dict[str, object]) -> None:
        _configure_artifact_recipe(document)
        if recipe_transform is not None:
            recipe_transform(document)

    sessions, recipe_operations, queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, recipe_transform=transform
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


def artifact_create_request(run_id: str, request_id: str) -> dict[str, object]:
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
    job = service.create(**request)
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


def cancellation_result(claim, artifact_job, *, state: str, reason: str) -> AgentResult:
    empty: tuple[RecipeJobFile, ...] = ()
    return AgentResult(
        schema_version=1,
        job_id=claim.job_id,
        operation_id=claim.operation_id,
        attempt=claim.attempt,
        fence=claim.fence,
        node_id=claim.node_id,
        deadline=claim.deadline,
        state=state,
        result={
            "schema_version": 1,
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
        },
    )


def test_artifact_job_create_idempotency_compares_canonical_semantics(
    tmp_path,
) -> None:
    _sessions, _operations, _queue, service, run_id, _node_id = (
        running_artifact_service(tmp_path)
    )
    request = artifact_create_request(run_id, "00000000-0000-4000-8000-000000000114")
    first = service.create(**request)

    replay = dict(request)
    replay["parameters"] = {"seed": 0, "prompt": "fox"}
    replayed = service.create(**replay)

    assert replayed.id == first.id


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
    service.create(**request)
    replay = copy.deepcopy(request)
    change(replay)

    with pytest.raises(ArtifactJobError, match="request key"):
        service.create(**replay)


def test_artifact_job_create_rejects_replay_after_compiled_contract_drift(
    tmp_path,
) -> None:
    sessions, _operations, _queue, service, run_id, _node_id = running_artifact_service(
        tmp_path
    )
    request = artifact_create_request(run_id, "00000000-0000-4000-8000-000000000116")
    service.create(**request)
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        installation = session.get(RecipeInstallation, run.installation_id)
        revision = session.get(CatalogDocumentRevision, installation.recipe_revision_id)
        document = copy.deepcopy(revision.document)
        document["settings"]["knobs"]["seed"]["value"] = 99
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
            content_digest=content_sha256(parsed),
            projected={},
            created_by="admin",
            created_at=revision.created_at,
        )
        session.add(replacement)
        session.flush()
        installation.recipe_revision_id = replacement.id

    with pytest.raises(ArtifactJobError, match="request key"):
        service.create(**request)


def test_artifact_job_create_exact_concurrent_replay_has_one_identity(
    tmp_path,
) -> None:
    _sessions, _operations, _queue, service, run_id, _node_id = (
        running_artifact_service(tmp_path)
    )
    request = artifact_create_request(run_id, "00000000-0000-4000-8000-000000000117")

    with ThreadPoolExecutor(max_workers=2) as workers:
        identifiers = tuple(
            workers.map(lambda _index: service.create(**request).id, range(2))
        )

    assert len(set(identifiers)) == 1


def test_artifact_job_stages_exact_inputs_enqueues_and_persists_result(
    tmp_path,
) -> None:
    sessions, _recipe_operations, queue, service, run_id, node_id = (
        running_artifact_service(tmp_path)
    )
    input_content = b"png"
    input_digest = hashlib.sha256(input_content).hexdigest()
    job = service.create(
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
            "max_files": 1,
            "max_file_bytes": 1024,
            "max_total_bytes": 4096,
            "allowed_media_types": ["image/png"],
        },
        timeout_seconds=3600,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000103",
    )
    assert job.state == "draft"
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
    assert service.finalize(job.id).state == "ready"
    submitted = service.submit(
        job.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000104",
    )
    assert submitted.state == "queued"
    assert queue.available > 0
    with sessions() as session:
        operation = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == submitted.operation_id
            )
        )
        assert operation.kind == "recipe.job.run.v1"
        assert operation.payload["reserved_memory_bytes"] == 225
        assert operation.payload["input_manifest_sha256"] == job.input_manifest_sha256
        assert operation.payload["contract_sha256"] == job.contract_sha256
        assert operation.payload["parameters"]["prompt"] == "fox / meadow"
        assert operation.payload["output_mappings"] == [
            {
                "slot": "image",
                "media_type": "image/png",
                "extensions": [".png"],
            }
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

    output_content = b"done"
    output_digest = hashlib.sha256(output_content).hexdigest()
    service.put_output(
        job.id,
        node_id=node_id,
        name="output.png",
        media_type="image/png",
        expected_sha256=output_digest,
        content=output_content,
    )
    output = RecipeJobFile("output.png", "image/png", 4, output_digest)
    result = {
        "schema_version": 1,
        "job_id": job.id,
        "run_id": run_id,
        "exit_code": 0,
        "output_manifest": {
            **recipe_job_manifest_document((output,)),
            "manifest_sha256": recipe_job_manifest_sha256((output,)),
        },
        "evidence": {"elapsed_milliseconds": 1234, "peak_memory_bytes": None},
    }
    with sessions.begin() as session:
        stored_operation = session.get(AgentOperation, operation.id)
        service.consume_agent_result(
            session,
            stored_operation,
            object(),
            SimpleNamespace(state="succeeded", result=result),
        )
    completed = service.result_metadata(job.id)
    assert completed.state == "succeeded"
    assert completed.output_manifest_sha256 == recipe_job_manifest_sha256((output,))
    output_path, output_media_type, output_name, output_size = service.result_blob(
        job.id, output_digest
    )
    assert (output_path.read_bytes(), output_media_type, output_name, output_size) == (
        output_content,
        "image/png",
        "output.png",
        4,
    )
    with sessions() as session:
        assert session.get(RecipeRun, run_id).state == "running"


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
        service.create(
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
        service.create(**request, inputs=[], timeout_seconds=3601)


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
        service.create(**request)


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
        slot = document["interfaces"][0]["output"]["slots"][0]
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
    job = service.create(**request)
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
        assert operation.payload["output_mappings"] == [
            {
                "slot": "image",
                "media_type": media_type,
                "extensions": [extension],
            }
        ]


def test_artifact_job_rejects_unrepresentable_output_media_mapping(tmp_path) -> None:
    def transform(document: dict[str, object]) -> None:
        slot = document["interfaces"][0]["output"]["slots"][0]
        slot["media_types"] = ["image/avif", "image/png"]
        slot["extensions"] = [".avif", ".png"]

    _sessions, _operations, _queue, service, run_id, _node_id = running_artifact_service(
        tmp_path, recipe_transform=transform
    )

    with pytest.raises(ArtifactJobError, match="output slot contract"):
        service.create(
            **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000132")
        )


def test_artifact_job_rejects_cross_slot_output_extension_collision(tmp_path) -> None:
    def transform(document: dict[str, object]) -> None:
        duplicate = copy.deepcopy(document["interfaces"][0]["output"]["slots"][0])
        duplicate.update(
            {
                "id": "receipt",
                "label": "Receipt",
                "description": "Generated receipt",
                "media_types": ["application/json"],
            }
        )
        document["interfaces"][0]["output"]["slots"].append(duplicate)

    _sessions, _operations, _queue, service, run_id, _node_id = running_artifact_service(
        tmp_path, recipe_transform=transform
    )

    with pytest.raises(ArtifactJobError, match="extensions"):
        service.create(
            **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000133")
        )


def test_artifact_output_uses_longest_signed_suffix_for_same_media_type(
    tmp_path,
) -> None:
    def transform(document: dict[str, object]) -> None:
        media_type = "application/vnd.example.custom"
        output = document["interfaces"][0]["output"]
        short = output["slots"][0]
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
        output["slots"].append(detailed)
        document["validation"]["serving"]["checks"][0]["request"][
            "output_slot"
        ] = "detailed"

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
    job = service.create(**request)
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
        "artifact.vonk.bin", media_type, len(output_content), output_digest
    )
    result = {
        "schema_version": 1,
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
        job = service.create(
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
    with pytest.raises(Exception, match="active job"):
        operations.preview_stop(run_id)
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
        operation.state = "running"
    cancelling = service.cancel(
        first.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000113",
        reason="operator requested stop",
    )
    assert cancelling.state == "cancelling"
    assert (
        service.cancel(
            first.id,
            actor="operator",
            request_id="00000000-0000-4000-8000-000000000113",
            reason="operator requested stop",
        ).state
        == "cancelling"
    )
    with pytest.raises(Exception, match="active job"):
        operations.preview_stop(run_id)
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
    submitted = submitted_artifact_job(service, run_id, request_suffix=118)
    agent_jobs = AgentJobService(sessions, clock=MutableClock(NOW))

    def consume(session, operation, attempt, message) -> None:
        service.consume_agent_result(session, operation, attempt, message)
        recipe_operations.consume_agent_result(session, operation, attempt, message)

    agent_jobs.set_result_consumer(consume)
    claim = claim_agent(agent_jobs, node_id, "serial-0", 30)
    assert claim is not None

    cancelling = service.cancel(
        submitted.id,
        actor="operator",
        request_id="00000000-0000-4000-8000-000000000120",
        reason="operator requested stop",
    )
    assert cancelling.state == "cancelling"
    directive = agent_jobs.heartbeat(claim, {"phase": "running"}, 30)
    assert directive.cancel_requested is True
    with pytest.raises(Exception, match="active job"):
        recipe_operations.preview_stop(run_id)

    acknowledged = cancellation_result(
        claim,
        submitted,
        state="cancelled",
        reason="controller cancellation requested",
    )
    agent_jobs.record_result(acknowledged)
    assert service.get(submitted.id).state == "cancelled"
    recipe_operations.preview_stop(run_id)
    with pytest.raises(StaleAgentAttempt):
        agent_jobs.record_result(acknowledged)


def test_artifact_cancel_stop_failure_remains_recoverable_and_blocks_release(
    tmp_path,
) -> None:
    sessions, recipe_operations, _queue, service, run_id, node_id = (
        running_artifact_service(tmp_path)
    )
    submitted = submitted_artifact_job(service, run_id, request_suffix=121)
    agent_jobs = AgentJobService(sessions, clock=MutableClock(NOW))

    def consume(session, operation, attempt, message) -> None:
        service.consume_agent_result(session, operation, attempt, message)
        recipe_operations.consume_agent_result(session, operation, attempt, message)

    agent_jobs.set_result_consumer(consume)
    claim = claim_agent(agent_jobs, node_id, "serial-0", 30)
    assert claim is not None
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

    view = service.get(submitted.id)
    assert view.state == "waiting-for-operator"
    assert view.result_evidence == {
        "failure_kind": "cancellation-stop-uncertain",
        "recoverable": True,
        "active_scope_may_remain": True,
        "elapsed_milliseconds": 10,
        "peak_memory_bytes": None,
    }
    with pytest.raises(Exception, match="active job"):
        recipe_operations.preview_stop(run_id)


def test_unsafe_artifact_lease_expiry_is_terminal_recoverable_and_fences_result(
    tmp_path,
) -> None:
    sessions, recipe_operations, _queue, service, run_id, node_id = (
        running_artifact_service(tmp_path)
    )
    submitted = submitted_artifact_job(service, run_id, request_suffix=124)
    clock = MutableClock(NOW)
    agent_jobs = AgentJobService(sessions, clock=clock)
    claim = claim_agent(agent_jobs, node_id, "serial-0", 30)
    assert claim is not None

    clock.advance(seconds=31)
    assert claim_agent(agent_jobs, node_id, "serial-0", 30) is None
    expired = service.get(submitted.id)
    assert expired.state == "failed"
    assert expired.result_evidence == {
        "failure_kind": "agent-lease-expired",
        "recoverable": True,
        "late_results_accepted": False,
    }
    recipe_operations.preview_stop(run_id)
    with pytest.raises(StaleAgentAttempt):
        agent_jobs.record_result(
            cancellation_result(
                claim,
                submitted,
                state="cancelled",
                reason="controller cancellation requested",
            )
        )


def test_draft_artifact_cancel_idempotency_rejects_mismatched_replay(tmp_path) -> None:
    _sessions, _operations, _queue, service, run_id, _node_id = (
        running_artifact_service(tmp_path)
    )
    job = service.create(
        **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000127")
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


def test_blob_store_serializes_concurrent_quota_and_reconciles(tmp_path) -> None:
    first_store = ArtifactBlobStore(tmp_path / "blobs", max_stored_bytes=6)
    second_store = ArtifactBlobStore(tmp_path / "blobs", max_stored_bytes=6)

    async def exercise() -> list[object]:
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
        assert usage == {
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
    assert first_store.usage()["used_bytes"] <= 6
    survivor = next((item for item in results if not isinstance(item, Exception)), None)
    referenced = {survivor.sha256} if survivor is not None else set()
    orphan = b"x"
    orphan_digest = hashlib.sha256(orphan).hexdigest()
    if first_store.usage()["remaining_bytes"]:
        first_store.put_bytes(orphan_digest, orphan, maximum_bytes=1)
    report = first_store.reconcile(referenced, orphan_grace_seconds=0)
    assert report["missing_referenced_blobs"] == []
    assert report["removed_orphan_blobs"] in {0, 1}


def test_terminal_job_retention_removes_only_unreferenced_cas_bytes(tmp_path) -> None:
    sessions, _operations, _queue, service, run_id, _node_id = running_artifact_service(
        tmp_path
    )
    content = b"png"
    digest = hashlib.sha256(content).hexdigest()
    job = service.create(
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
        stored.completed_at = NOW - timedelta(days=8)
    report = service.reconcile_storage()
    assert report["expired_jobs"] == 1
    with sessions() as session:
        assert session.get(ArtifactJob, job.id) is None
        assert session.get(ArtifactJobBlob, digest) is None


def test_gc_cannot_delete_old_dedup_blob_during_database_attachment(
    tmp_path, monkeypatch
) -> None:
    sessions, recipe_operations, _queue, service, run_id, _node_id = (
        running_artifact_service(tmp_path)
    )
    content = b"png"
    digest = hashlib.sha256(content).hexdigest()
    job = service.create(
        **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000129")
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

    assert report["removed_orphan_blobs"] == 0
    assert seeded.path.is_file()
    with sessions() as session:
        attached = session.scalar(select(ArtifactJob).where(ArtifactJob.id == job.id))
        assert attached is not None
        assert service.get(job.id).input_files[0]["sha256"] == digest
