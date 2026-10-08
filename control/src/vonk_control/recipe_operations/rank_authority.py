"""Rank authority for digest-bound recipe operations."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import (
    RecipeBuildCleanupRequest,
    RecipeBuildRequest,
    RecipeInstallPayload,
    RecipeJobRunRequest,
    RecipeReconcilePayload,
    RecipeStartPayload,
    RecipeStopPayload,
    RecipeUninstallPayload,
    canonical_message,
)
from vonk_forge_contracts import RecipeDefinition

from ..lifecycle.evidence import (
    BookkeepingReason,
    Damaged,
    Residue,
    read_or_rebuild,
    retire_as_unknown,
)
from ..models import (
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    Job,
    RecipeInstallation,
    RecipeRun,
)
from ..profile_stop_authority import (
    ProfileJobRunStopJob,
)
from ..recipe_execution_contract import (
    StoredInstallationPlan,
    StoredRunPlan,
    parse_stored_installation_plan,
    parse_stored_run_plan,
)
from ..recipe_progress import (
    _catalog_recipe as _catalog_recipe,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import (
    _lower_hex_digest as _lower_hex_digest,  # noqa: PLC0414 -- shared helper export
)
from ..stored_json import read_row_column
from ..strict_json import read_stored_model
from .observation_helpers import _active_recipe_revision
from .results import _AcceptedRanks


def _ranks_from_mapping(
    session: Session, mapping_id: str, generation: int, revision_id: str
) -> _AcceptedRanks | None:
    """The accepted (node, rank, role) set from the saved mapping, as evidence.

    A mapping generation is immutable, so it names the same ranks the stored plan
    of an installation or run was accepted with.
    """

    mapping = session.get(ClusterMapping, mapping_id)
    if mapping is None:
        return None
    rows = tuple(
        session.scalars(
            select(ClusterMappingNode).where(
                ClusterMappingNode.mapping_id == mapping_id
            )
        )
    )
    if not rows:
        return None
    ranks = frozenset((row.node_id, row.rank, row.role) for row in rows)
    return ranks, (
        len(ranks) == len(rows)
        and mapping.node_count == len(rows)
        and mapping.generation == generation
        and mapping.recipe_revision_id == revision_id
    )


def _plan_ranks(
    plan: StoredRunPlan | StoredInstallationPlan,
) -> tuple[frozenset[tuple[str, int, str]], int]:
    return frozenset((node.node_id, node.rank, node.role) for node in plan.nodes), len(
        plan.nodes
    )


def _run_accepted_ranks(
    session: Session, run: RecipeRun, revision_id: str
) -> _AcceptedRanks | Residue:
    """The ranks a run was accepted with and whether its identity still matches.

    A stored run plan that does not parse is rebuilt from the saved mapping; when
    that is gone too the damage is retired as unknown and the caller treats the
    membership as unproven (it never refuses on the damaged document itself).
    """

    def read() -> _AcceptedRanks | Damaged:
        document = parse_stored_run_plan(read_row_column(run, "plan"))
        ranks, count = _plan_ranks(document)
        return ranks, (
            len(ranks) == count
            and document.installation_id == run.installation_id
            and document.mapping_id == run.mapping_id
            and document.mapping_generation == run.mapping_generation
            and document.recipe_revision_id == revision_id
            and document.plan_digest == run.plan_digest
        )

    return read_or_rebuild(
        kind="recipe.run-plan",
        subject=run.id,
        read=read,
        rebuild=lambda: _ranks_from_mapping(
            session, run.mapping_id, run.mapping_generation, revision_id
        ),
    )


@dataclass(frozen=True, slots=True)
class _UninstallRecipe:
    """What an uninstall needs to know of the recipe an installation came from."""

    id: str
    document_id: str
    content_digest: str
    document: RecipeDefinition | None


def _uninstall_recipe(
    session: Session, installation: RecipeInstallation, *, lock: bool
) -> _UninstallRecipe | None:
    """The recipe of an installation, from its active revision or else from the
    evidence the installation itself carries (its revision row in any state and
    the digest its accepted plan recorded); ``None`` only when no exact digest
    can be proven, which an uninstall cannot do without."""

    revision = _active_recipe_revision(
        session, installation.recipe_revision_id, for_update=lock
    )
    if revision is not None and revision.content_digest is not None:
        return _UninstallRecipe(
            revision.id,
            revision.document_id,
            revision.content_digest,
            _catalog_recipe(read_row_column(revision, "document")),
        )
    row = session.get(CatalogDocumentRevision, installation.recipe_revision_id)
    digest = row.content_digest if row is not None else None
    if digest is None:
        loaded_plan = read_or_rebuild(
            kind="recipe.installation-plan",
            subject=installation.id,
            read=lambda: parse_stored_installation_plan(
                read_row_column(installation, "plan"), for_uninstall=True
            ),
        )
        if not isinstance(loaded_plan, Residue):
            digest = loaded_plan.recipe_content_sha256
    if not isinstance(digest, str) or not _lower_hex_digest(digest):
        return None
    retire_as_unknown(
        "recipe.installation-recipe",
        installation.id,
        BookkeepingReason.EVIDENCE_UNAVAILABLE,
        "the recipe revision is not active; the uninstall uses its recorded digest",
    )
    document = read_row_column(row, "document") if row is not None else None
    return _UninstallRecipe(
        installation.recipe_revision_id,
        row.document_id if row is not None else installation.recipe_revision_id,
        digest,
        _catalog_recipe(document),
    )


def _installation_accepted_ranks(
    session: Session,
    installation: RecipeInstallation,
    revision: _UninstallRecipe,
) -> _AcceptedRanks | Residue:
    """The ranks an installation was accepted with; rebuilt from its mapping."""

    def read() -> _AcceptedRanks | Damaged:
        document = parse_stored_installation_plan(
            read_row_column(installation, "plan"), for_uninstall=True
        )
        ranks, count = _plan_ranks(document)
        return ranks, (
            len(ranks) == count
            and document.mapping_id == installation.mapping_id
            and document.mapping_generation == installation.mapping_generation
            and document.recipe_revision_id == revision.id
            and document.recipe_content_sha256 == revision.content_digest
            and document.plan_digest == installation.plan_digest
        )

    return read_or_rebuild(
        kind="recipe.installation-plan",
        subject=installation.id,
        read=read,
        rebuild=lambda: _ranks_from_mapping(
            session,
            installation.mapping_id,
            installation.mapping_generation,
            revision.id,
        ),
    )


def _profile_jobrun_parent(job: Job) -> ProfileJobRunStopJob | Residue:
    """The typed parent of a profile JobRun Stop; damage is retired as unknown."""

    return read_or_rebuild(
        kind="recipe.profile-jobrun-stop-parent",
        subject=job.id,
        read=lambda: ProfileJobRunStopJob.model_validate_parent(
            read_stored_model(
                ProfileJobRunStopJob,
                canonical_message(read_row_column(job, "payload")),
                from_json=True,
            ).model_dump(mode="json")
        ),
    )


def _run_is_one_shot(session: Session, run: RecipeRun) -> bool:
    """Whether the run only hosts one-shot jobs (no service container).

    The stored plan says so; when it does not parse, the evidence is the
    activation operation that created the run, which nothing else produces.
    """

    def read() -> bool:
        return (
            parse_stored_run_plan(read_row_column(run, "plan")).execution_mode
            == "one-shot-jobs"
        )

    def rebuild() -> bool:
        return (
            session.scalar(
                select(Job.id)
                .where(
                    Job.kind == "recipe.job.activate.v1",
                    Job.payload["owner_kind"].as_string() == "run",
                    Job.payload["owner_id"].as_string() == run.id,
                )
                .limit(1)
            )
            is not None
        )

    loaded = read_or_rebuild(
        kind="recipe.run-plan", subject=run.id, read=read, rebuild=rebuild
    )
    return loaded is True


def _run_observes_per_generation(run: RecipeRun) -> bool:
    """Whether the run's observations are tracked per generation.

    A plan that does not parse is read as the current schema: the observations it
    resets are re-established by the next report of each rank, so nothing is lost.
    """

    def read() -> bool:
        return (
            parse_stored_run_plan(
                read_row_column(run, "plan")
            ).observation_schema_version
            == 2
        )

    loaded = read_or_rebuild(kind="recipe.run-plan", subject=run.id, read=read)
    return True if isinstance(loaded, Residue) else loaded


_RECIPE_WIRE_PAYLOAD_MODELS = {
    WireAgentOperation.RECIPE_BUILD.value: RecipeBuildRequest,
    WireAgentOperation.RECIPE_JOB_RUN.value: RecipeJobRunRequest,
    WireAgentOperation.RECIPE_BUILD_CLEANUP.value: RecipeBuildCleanupRequest,
    WireAgentOperation.RECIPE_INSTALL.value: RecipeInstallPayload,
    WireAgentOperation.RECIPE_RECONCILE.value: RecipeReconcilePayload,
    WireAgentOperation.RECIPE_START.value: RecipeStartPayload,
    WireAgentOperation.RECIPE_STOP.value: RecipeStopPayload,
    WireAgentOperation.RECIPE_UNINSTALL.value: RecipeUninstallPayload,
}
