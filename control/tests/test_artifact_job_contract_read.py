"""A client reads the job contract of a running recipe before it creates a job.

The read is built on the real service and database that ``create`` uses, so the
contract it returns is proven to be the one ``create`` validates against.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from vonk_agent_protocol import RunState
from vonk_control.artifact_blob_store import ArtifactBlobStore
from vonk_control.artifact_job_api import install_artifact_job_routes
from vonk_control.artifact_jobs import (
    ArtifactJobContractResponse,
    ArtifactJobService,
    JobContractNotFound,
    JobContractState,
)
from vonk_control.auth import Actor
from vonk_control.models import RecipeInstallation, RecipeRun
from vonk_control.run_route_name import alias_owner
from vonk_control.strict_json import ControllerAPIRoute

from cluster_profiles.control_client import validate_control_document
from cluster_profiles.generated_control.models.artifact_job_contract_response import (
    ArtifactJobContractResponse as ClientArtifactJobContractResponse,
)

from .test_artifact_job_uncompilable_contract import (
    _required_input_without_required_slot,
)
from .test_artifact_jobs import (
    artifact_create_request,
    create_artifact_job,
    running_artifact_service,
)
from .test_recipe_operations import NOW, installed_recipe, setup_services

UNKNOWN = "00000000-0000-4000-8000-0000000000ff"


def _run(sessions, run_id: str) -> tuple[str, str]:
    """The alias and recipe revision of a run, read from the database."""
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        installation = session.get(RecipeInstallation, run.installation_id)
        return run.alias, installation.recipe_revision_id


def _stop(sessions, run_id: str) -> None:
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        run.state = RunState.STOPPING


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


def test_contract_read_by_alias_and_by_revision_agree_with_the_run(tmp_path) -> None:
    sessions, _operations, _queue, service, run_id, _node = running_artifact_service(
        tmp_path
    )
    alias, revision_id = _run(sessions, run_id)

    by_run = service.job_contract(run_id=run_id)
    by_alias = service.job_contract(alias=alias)
    by_revision = service.job_contract(recipe_revision_id=revision_id)

    assert by_alias == by_run
    assert by_revision.contract == by_run.contract
    assert by_revision.contract_sha256 == by_run.contract_sha256
    assert (by_revision.run_id, by_revision.alias, by_revision.run_state) == (
        None,
        None,
        None,
    )


@pytest.mark.parametrize("key", ["run_id", "alias", "recipe_revision_id"])
def test_contract_read_of_an_unknown_key_is_not_found(tmp_path, key) -> None:
    _sessions, _operations, _queue, service, _run_id, _node = running_artifact_service(
        tmp_path
    )

    with pytest.raises(JobContractNotFound):
        service.job_contract(**{key: "no-such-alias" if key == "alias" else UNKNOWN})


def test_contract_read_of_a_run_that_is_not_running_answers_not_running(
    tmp_path,
) -> None:
    sessions, _operations, _queue, service, run_id, _node = running_artifact_service(
        tmp_path
    )
    alias, revision_id = _run(sessions, run_id)
    _stop(sessions, run_id)

    for key in ({"run_id": run_id}, {"alias": alias}):
        answer = service.job_contract(**key)
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


def test_alias_owner_is_the_newest_accepted_intent() -> None:
    def run(run_id: str, created: int, renamed: int | None = None) -> RecipeRun:
        return RecipeRun(
            id=run_id,
            created_at=datetime(2026, 1, created, tzinfo=UTC),
            renamed_at=None
            if renamed is None
            else datetime(2026, 1, renamed, tzinfo=UTC),
        )

    older, renamed_in, newer = run("a", 1), run("b", 1, renamed=5), run("c", 3)

    assert alias_owner([older, renamed_in, newer]) is renamed_in
    assert alias_owner([older, newer]) is newer
    assert alias_owner([]) is None


def test_http_read_returns_the_typed_contract_and_typed_states(tmp_path) -> None:
    sessions, _operations, _queue, service, run_id, _node = running_artifact_service(
        tmp_path
    )
    alias, revision_id = _run(sessions, run_id)
    client = _client(service)

    read = client.get(f"/api/artifact-jobs/contract/alias/{alias}")

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

    missing = client.get("/api/artifact-jobs/contract/alias/nope")
    assert missing.status_code == 404
    assert missing.headers["x-vonk-error-code"] == "job_contract.not_found"
    assert client.get("/api/artifact-jobs/contract/run/not-a-uuid").status_code == 404

    _stop(sessions, run_id)
    stopping = client.get(f"/api/artifact-jobs/contract/run/{run_id}")
    # A read answers; the state says it is not running.
    assert stopping.status_code == 200
    assert stopping.json()["state"] == "not-running"
    assert validate_control_document("ArtifactJobContractResponse", stopping.json())
