"""A client reads the job contract of a running recipe before it creates a job.

The read is built on the real service and database that ``create`` uses, so the
contract it returns is proven to be the one ``create`` validates against.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from vonk_forge_contracts import read_recipe

from cluster_profiles.control_client.schema import validate_control_document
from cluster_profiles.generated_control.models.artifact_job_contract_response import (
    ArtifactJobContractResponse as ClientArtifactJobContractResponse,
)
from vonk_agent_protocol.state_machines import RunState
from vonk_control.api.application import create_app
from vonk_control.artifact_jobs.api import install_artifact_job_routes
from vonk_control.artifact_jobs.blob_store import ArtifactBlobStore
from vonk_control.artifact_jobs.contracts import (
    ArtifactJobInvalid,
    _effective_parameters,
    slot_input_media_type,
)
from vonk_control.artifact_jobs.job_contract import (
    ArtifactJobContractResponse,
    JobContractNotFound,
    JobContractState,
)
from vonk_control.artifact_jobs.service import ArtifactJobService
from vonk_control.auth import Actor, TokenCodec
from vonk_control.compiled_artifact_contract import CompiledArtifactContract
from vonk_control.models.catalog import CatalogDocument, CatalogDocumentRevision
from vonk_control.models.execution import RecipeInstallation, RecipeRun
from vonk_control.operator_acceptance.resolution import ControllerAPIRoute
from vonk_control.recipe_operations.runtime_specs import _compiled_arguments

from .agent_api_support import Jobs
from .artifact_jobs_support import (
    artifact_create_request,
    create_artifact_job,
    running_artifact_service,
)
from .recipe_operations_support import NOW, installed_recipe, setup_services
from .test_artifact_job_uncompilable_contract import (
    _required_input_without_required_slot,
)

UNKNOWN = "00000000-0000-4000-8000-0000000000ff"


def _run(sessions, run_id: str) -> tuple[str, str]:
    """The alias and recipe revision of a run, read from the database."""
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        installation = session.get(RecipeInstallation, run.installation_id)
        return run.alias, installation.recipe_revision_id


def _set_run_state(sessions, run_id: str, state: RunState) -> None:
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.state = state


def _client(service) -> TestClient:
    app = FastAPI()
    app.router.route_class = ControllerAPIRoute
    install_artifact_job_routes(
        app,
        actor_dependency=Depends(lambda: Actor("reader", "viewer")),
        service=service,
    )
    return TestClient(app)


def test_contract_read_by_run_is_the_contract_create_validates_against(
    tmp_path,
) -> None:
    sessions, _operations, _queue, service, run_id, _node = running_artifact_service(
        tmp_path
    )
    alias, revision_id = _run(sessions, run_id)

    answer = service.job_contract(run_id=run_id)
    job = create_artifact_job(
        service,
        **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000401"),
    )

    assert answer.state is JobContractState.AVAILABLE
    assert answer.detail is None
    assert answer.contract is not None
    assert answer.run_id == run_id
    assert answer.alias == alias
    assert answer.run_state == RunState.RUNNING
    assert answer.recipe_revision_id == revision_id
    assert answer.contract_sha256 == job.contract_sha256
    assert answer.contract == job.compiled_contract
    # The client needs the slots, their limits and the knobs, without a job.
    assert answer.contract.interface == "image-job"
    assert [slot.id for slot in answer.contract.output.slots] == ["image"]
    assert answer.contract.output.slots[0].max_file_bytes == 1024
    assert [item.name for item in answer.contract.parameters] == ["prompt", "seed"]
    assert answer.contract.max_timeout_seconds == 3_600


def test_contract_read_by_revision_agrees_with_the_run(tmp_path) -> None:
    sessions, _operations, _queue, service, run_id, _node = running_artifact_service(
        tmp_path
    )
    _alias, revision_id = _run(sessions, run_id)

    by_run = service.job_contract(run_id=run_id)
    by_revision = service.job_contract(recipe_revision_id=revision_id)

    assert by_revision.contract == by_run.contract
    assert by_revision.contract_sha256 == by_run.contract_sha256
    assert (by_revision.run_id, by_revision.alias, by_revision.run_state) == (
        None,
        None,
        None,
    )


@pytest.mark.parametrize("key", ["run_id", "recipe_revision_id"])
def test_contract_read_of_an_unknown_key_is_not_found(tmp_path, key) -> None:
    _sessions, _operations, _queue, service, _run_id, _node = running_artifact_service(
        tmp_path
    )

    with pytest.raises(JobContractNotFound):
        service.job_contract(**{key: UNKNOWN})


def test_contract_read_of_a_run_that_is_not_running_answers_not_running(
    tmp_path,
) -> None:
    sessions, _operations, _queue, service, run_id, _node = running_artifact_service(
        tmp_path
    )
    _alias, revision_id = _run(sessions, run_id)
    _set_run_state(sessions, run_id, RunState.STOPPING)

    for answer in (service.job_contract(run_id=run_id),):
        assert answer.state is JobContractState.NOT_RUNNING
        assert answer.run_state == RunState.STOPPING
        assert answer.run_id == run_id
        assert answer.contract is None
        assert answer.detail is not None
        assert "stopping" in answer.detail
    # The exact revision still has a contract: it needs no run.
    by_revision = service.job_contract(recipe_revision_id=revision_id)
    assert by_revision.state is JobContractState.AVAILABLE


def test_contract_read_of_a_chat_recipe_is_not_a_job_recipe(tmp_path) -> None:
    sessions, operations, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed = installed_recipe(
        operations,
        mapping_id,
        build_id,
        nodes,
        request_id="00000000-0000-4000-8000-000000000402",
    )
    with sessions() as session:
        revision_id = session.scalar(
            select(RecipeInstallation.recipe_revision_id).where(
                RecipeInstallation.id == installed.owner_id
            )
        )
    service = ArtifactJobService(
        sessions,
        recipe_operations=operations,
        blob_store=ArtifactBlobStore(tmp_path / "artifact-blobs"),
        clock=lambda: NOW,
    )

    answer = service.job_contract(recipe_revision_id=revision_id)

    assert answer.state is JobContractState.NOT_A_JOB_RECIPE
    assert answer.contract is None
    assert answer.recipe_revision_id == revision_id


def test_contract_read_of_an_uncompilable_recipe_names_the_cause(tmp_path) -> None:
    _sessions, _operations, _queue, service, run_id, _node = running_artifact_service(
        tmp_path, recipe_transform=_required_input_without_required_slot
    )

    answer = service.job_contract(run_id=run_id)

    assert answer.state is JobContractState.CONTRACT_INVALID
    assert answer.contract is None
    assert answer.detail is not None
    assert "does not compile" in answer.detail


def test_http_read_returns_the_typed_contract_and_typed_states(tmp_path) -> None:
    sessions, _operations, _queue, service, run_id, _node = running_artifact_service(
        tmp_path
    )
    _alias, revision_id = _run(sessions, run_id)
    client = _client(service)

    read = client.get(f"/api/artifact-jobs/contract/run/{run_id}")

    assert read.status_code == 200
    document = read.json()
    # The generated client model parses what the Controller emits.
    parsed = ClientArtifactJobContractResponse.from_dict(document)
    assert parsed.run_id == run_id
    assert (
        ArtifactJobContractResponse.model_validate_json(read.content).contract
        == service.job_contract(run_id=run_id).contract
    )
    assert document["state"] == "available"
    assert document["contract"]["schema_version"] == 1
    # vonkctl validates the same emitted document against the generated schema.
    assert validate_control_document("ArtifactJobContractResponse", document)
    by_revision = client.get(f"/api/artifact-jobs/contract/revision/{revision_id}")
    assert by_revision.status_code == 200
    assert "run_id" not in by_revision.json()

    missing = client.get(f"/api/artifact-jobs/contract/run/{UNKNOWN}")
    assert missing.status_code == 404
    assert missing.headers["x-vonk-error-code"] == "job_contract.not_found"
    assert client.get("/api/artifact-jobs/contract/run/not-a-uuid").status_code == 404

    _set_run_state(sessions, run_id, RunState.STOPPING)
    stopping = client.get(f"/api/artifact-jobs/contract/run/{run_id}")
    # A read answers; the state says it is not running.
    assert stopping.status_code == 200
    assert stopping.json()["state"] == "not-running"
    assert validate_control_document("ArtifactJobContractResponse", stopping.json())


def test_a_viewer_bearer_token_reads_the_contract_through_create_app(tmp_path) -> None:
    _sessions, _operations, _queue, service, run_id, _node = running_artifact_service(
        tmp_path
    )
    now = int(NOW.timestamp())
    tokens = TokenCodec(b"artifact-job-contract-read-key-0001")
    client = TestClient(
        create_app(jobs=Jobs(), tokens=tokens, artifact_jobs=service, now=lambda: now)
    )
    path = f"/api/artifact-jobs/contract/run/{run_id}"
    viewer = tokens.issue(Actor("read-only", "viewer"), ttl_seconds=600, now=now)

    assert client.get(path).status_code == 401
    read = client.get(path, headers={"Authorization": "Bearer " + viewer})

    assert read.status_code == 200, read.text
    assert read.json()["state"] == "available"
    assert read.json()["run_id"] == run_id


def _typed_knobs(document: Any) -> None:
    knobs = document["settings"]["knobs"]
    knobs["num-frames"] = {
        "value": 124,
        "change_effect": "none",
        "minimum": 124,
        "maximum": 345,
        "step": 17,
    }
    knobs["flow-shift"] = {
        "value": 12.0,
        "change_effect": "none",
        "minimum": 1.0,
        "maximum": 20.0,
    }
    knobs["style"] = {
        "value": 1,
        "change_effect": "none",
        "allowed_values": [1, 2],
    }
    document["runtime"]["arguments"].extend(
        {"name": name, "value": None, "setting": name}
        for name in ("num-frames", "flow-shift", "style")
    )


def _show_wording(sessions, run_id: str) -> None:
    """Publish a display for the run's recipe, as a catalog sync stores it."""
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        installation = session.get(RecipeInstallation, run.installation_id)
        assert installation is not None
        revision = session.get(CatalogDocumentRevision, installation.recipe_revision_id)
        assert revision is not None
        document = session.get(CatalogDocument, revision.document_id)
        assert document is not None
        document.display = {
            "kind": "recipe-display",
            "identity": {"publisher": document.publisher, "slug": document.slug},
            "title": "Typed",
            "description": "Typed knobs",
            "tags": [],
            "knobs": {"num-frames": {"label": "Frames", "unit": "frames"}},
        }


def test_typed_knobs_compile_enforce_and_show_display_wording(tmp_path) -> None:
    sessions, _operations, _queue, service, run_id, _node = running_artifact_service(
        tmp_path, recipe_transform=_typed_knobs
    )
    before = service.job_contract(run_id=run_id)
    _show_wording(sessions, run_id)
    after = service.job_contract(run_id=run_id)
    assert before.contract is not None
    assert after.contract is not None

    # Wording is joined from the display row and is not execution identity.
    assert after.contract_sha256 == before.contract_sha256
    frames = {item.name: item for item in after.contract.parameters}["num-frames"]
    assert (frames.label, frames.unit) == ("Frames", "frames")
    assert {item.name: item.label for item in before.contract.parameters}[
        "num-frames"
    ] is None

    definitions = after.contract.parameters
    flow = {item.name: item for item in definitions}["flow-shift"]
    assert flow.type == "float"
    assert isinstance(flow.default, float)
    assert _effective_parameters(definitions, {"num-frames": 141})["num-frames"] == 141
    assert (
        _effective_parameters(definitions, {"flow-shift": 12.5})["flow-shift"] == 12.5
    )
    for bad in (
        {"num-frames": 130},
        {"num-frames": 346},
        {"flow-shift": 20.5},
        {"style": True},
        {"style": 3},
    ):
        with pytest.raises(ArtifactJobInvalid):
            _effective_parameters(definitions, bad)
    with pytest.raises(ArtifactJobInvalid, match="124..345 in steps of 17"):
        _effective_parameters(definitions, {"num-frames": 130})


def test_contract_stored_before_typed_knobs_still_parses(tmp_path) -> None:
    _sessions, _operations, _queue, service, run_id, _node = running_artifact_service(
        tmp_path, recipe_transform=_typed_knobs
    )
    contract = service.job_contract(run_id=run_id).contract
    assert contract is not None
    stored: Any = contract.to_mapping()
    for item in stored["parameters"]:
        for key in ("step", "label", "description", "unit"):
            del item[key]
    assert CompiledArtifactContract.parse(stored).parameters[0].step is None


def test_a_supplied_knob_reaches_the_adapter_argument(tmp_path) -> None:
    sessions, _operations, _queue, _service, run_id, _node = running_artifact_service(
        tmp_path, recipe_transform=_typed_knobs
    )
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        installation = session.get(RecipeInstallation, run.installation_id)
        assert installation is not None
        revision = session.get(CatalogDocumentRevision, installation.recipe_revision_id)
        assert revision is not None
        recipe = read_recipe(revision.document)
    arguments = _compiled_arguments(recipe, {"num-frames": 141})
    assert ("num-frames", 141) in [(item.name, item.value) for item in arguments]


def _slot(media_types: list[str], extensions: list[str]) -> Any:

    return SimpleNamespace(
        id="in", media_types=media_types, extensions=extensions, max_files=1
    )


@pytest.mark.parametrize(
    ("name", "media_types", "extensions", "expected"),
    [
        ("Song.FLAC", ["audio/flac", "audio/wav"], [".flac", ".wav"], "audio/flac"),
        (
            "clip.mkv",
            ["video/mp4", "video/x-matroska"],
            [".mkv", ".mp4"],
            "video/x-matroska",
        ),
        ("a.obj", ["model/obj", "model/ply"], [".obj", ".ply"], "model/obj"),
        ("a.ply", ["model/obj", "model/ply"], [".obj", ".ply"], "model/ply"),
        ("a.aiff", ["audio/flac"], [".flac"], None),
    ],
)
def test_an_input_media_type_comes_from_its_name_and_the_slot(
    name, media_types, extensions, expected
) -> None:

    assert slot_input_media_type(_slot(media_types, extensions), name) == expected
