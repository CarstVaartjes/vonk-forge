"""The job contract a recipe revision publishes, readable before any job exists.

``create`` validates a job against the contract compiled from the run's recipe
revision (``_recipe_interface`` then ``_compile_contract``). A client that wants
to build a valid create request reads that same compilation here, keyed by what
it already knows: the running run or the exact recipe revision. Nothing is
stored and no second slot schema exists: the answer is the
``CompiledArtifactContract`` that ``create`` would check.

A read observes and answers. What cannot be given is a typed ``state``, not a
refusal: only a key that names nothing is not found.
"""

from __future__ import annotations

from enum import StrEnum
from functools import partial

from pydantic import Field, model_validator
from sqlalchemy.orm import Session
from vonk_agent_protocol import RunState

from ..compiled_artifact_contract import CompiledArtifactContract
from ..library_contract import UuidId
from ..lifecycle.evidence import Damaged
from ..models import RecipeInstallation, RecipeRun
from .contracts import (
    ArtifactJobContractModel,
    ArtifactJobError,
    _active_recipe_revision,
    _compile_contract,
    _recipe_interface,
)


class JobContractState(StrEnum):
    """What the read can say about the key it was given."""

    #: The contract is given; ``create`` validates against exactly this.
    AVAILABLE = "available"
    #: The run is not running (yet or any more): it takes no job now.
    NOT_RUNNING = "not-running"
    #: The recipe serves chat, or declares no single job interface.
    NOT_A_JOB_RECIPE = "not-a-job-recipe"
    #: The revision is immutable and its contract never compiles.
    CONTRACT_INVALID = "contract-invalid"


#: The stable code of the one refusal a read has: a key that names nothing.
JOB_CONTRACT_NOT_FOUND = "job_contract.not_found"


class JobContractNotFound(ArtifactJobError):
    """The key names no run or active recipe revision."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class ArtifactJobContractResponse(ArtifactJobContractModel):
    """The compiled job contract of one recipe revision, and what it was read by."""

    state: JobContractState
    #: Why there is no contract; absent while the contract is available.
    detail: str | None = Field(default=None, min_length=1, max_length=256)
    recipe_revision_id: UuidId | None = None
    #: ``publisher/slug`` of the recipe, the selector ``recipe detail`` accepts.
    recipe: str | None = Field(default=None, min_length=3, max_length=127)
    #: The run the key resolved to; absent when read by revision.
    run_id: UuidId | None = None
    #: The route name that run serves; absent when read by revision.
    alias: str | None = Field(default=None, min_length=1, max_length=64)
    #: The run's observed state; absent when read by revision.
    run_state: RunState | None = None
    contract_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    contract: CompiledArtifactContract | None = None

    @model_validator(mode="after")
    def contract_iff_available(self) -> ArtifactJobContractResponse:
        available = self.state is JobContractState.AVAILABLE
        if available != (self.contract is not None) or available != (
            self.contract_sha256 is not None
        ):
            raise ValueError("a job contract is given exactly when it is available")
        if available == (self.detail is not None):
            raise ValueError("a job contract has a detail exactly when it is absent")
        return self


def read_job_contract(
    session: Session,
    *,
    run_id: str | None = None,
    recipe_revision_id: str | None = None,
) -> ArtifactJobContractResponse:
    """Resolve exactly one key to a recipe revision and compile its contract."""

    if sum(key is not None for key in (run_id, recipe_revision_id)) != 1:
        raise ValueError("exactly one of run_id or recipe_revision_id")
    run: RecipeRun | None = None
    if run_id is not None:
        run = _run_by_id(session, run_id)
    if run is not None:
        installation = session.get(RecipeInstallation, run.installation_id)
        recipe_revision_id = (
            installation.recipe_revision_id if installation is not None else None
        )
    resolved = (
        _active_recipe_revision(session, recipe_revision_id)
        if recipe_revision_id is not None
        else None
    )
    if resolved is None or recipe_revision_id is None:
        raise JobContractNotFound("the recipe revision is not an active recipe")
    revision, recipe = resolved
    answer = partial(
        ArtifactJobContractResponse,
        recipe_revision_id=recipe_revision_id,
        recipe=f"{revision.publisher}/{revision.slug}",
        run_id=run.id if run is not None else None,
        alias=run.alias if run is not None else None,
        run_state=RunState(run.state) if run is not None else None,
    )
    if run is not None and run.state != RunState.RUNNING:
        return answer(
            state=JobContractState.NOT_RUNNING,
            detail=f"run {run.id} is {run.state}, not running",
        )
    interface = _recipe_interface(recipe)
    if interface is None:
        return answer(
            state=JobContractState.NOT_A_JOB_RECIPE,
            detail="the recipe declares no single job interface, so it takes no jobs",
        )
    contract = _compile_contract(recipe, interface)
    if isinstance(contract, Damaged):
        return answer(
            state=JobContractState.CONTRACT_INVALID,
            detail=f"recipe artifact contract does not compile: {contract.note}"[:256],
        )
    return answer(
        state=JobContractState.AVAILABLE,
        contract_sha256=contract.sha256(),
        contract=contract,
    )


def _run_by_id(session: Session, run_id: str) -> RecipeRun:
    run = session.get(RecipeRun, run_id)
    if run is None:
        raise JobContractNotFound(f"run {run_id} does not exist")
    return run
