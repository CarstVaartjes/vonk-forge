"""Action plans for digest-bound recipe operations."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import TYPE_CHECKING
from typing import cast as typing_cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import (
    InstallationNodeState,
    InstallationState,
    LifecycleState,
    ReservationState,
    RunState,
    WaitReason,
)

from .. import job_states
from ..lifecycle.evidence import (
    BookkeepingReason,
    Residue,
    retire_as_unknown,
)
from ..models import (
    AgentNode,
    InstallationNode,
    Job,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from ..recipe_action_plans import (
    StopNodeImpact,
    StopPlan,
    UninstallActiveRun,
    UninstallNodeImpact,
    UninstallPlan,
    stop_plan,
    uninstall_plan,
)
from ..recipe_progress import (
    _lower_hex_digest as _lower_hex_digest,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import (
    _primary_model_identity as _primary_model_identity,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import (
    _recipe_model_identities as _recipe_model_identities,  # noqa: PLC0414 -- shared helper export
)
from ..stored_json import read_row_column
from .constants import _MAX_ACTION_NODES, _MAX_ACTIVE_RUNS, _MEMORY_RESERVATION_KINDS
from .errors import RecipeRequestInvalid, RecipeRetryLater
from .intent import _active_owned_workload_jobs, _unissued_workload_children
from .observation_helpers import _active_recipe_revision
from .rank_authority import (
    _installation_accepted_ranks,
    _run_accepted_ranks,
    _uninstall_recipe,
)

if TYPE_CHECKING:
    from .service import RecipeOperationService


class ActionPlansMixin:
    def _stop_plan_in_session(
        self,
        session: Session,
        run_id: str,
        *,
        lock: bool,
        profile_target_node_ids: Sequence[str] | None = None,
        reviewed_route_state: str | None = None,
    ) -> StopPlan:
        run_statement = select(RecipeRun).where(RecipeRun.id == run_id)
        if lock:
            run_statement = run_statement.with_for_update(of=RecipeRun)
        run = session.scalar(run_statement)
        if run is None:
            raise RecipeRequestInvalid("recipe run does not exist")
        installation_statement = select(RecipeInstallation).where(
            RecipeInstallation.id == run.installation_id
        )
        if lock:
            installation_statement = installation_statement.with_for_update(
                of=RecipeInstallation
            )
        installation = session.scalar(installation_statement)
        if installation is None:
            raise RecipeRequestInvalid("recipe installation does not exist")

        # The Stop plan names the recipe revision by id and needs no more: the run
        # is stopped from its own durable Start authority, so a revision the
        # catalog can no longer serve never blocks the Stop.
        recipe_revision_id = installation.recipe_revision_id
        _active_recipe_revision(session, recipe_revision_id, for_update=lock)

        node_statement = (
            select(RunNode)
            .where(RunNode.run_id == run_id)
            .order_by(RunNode.rank, RunNode.node_id)
            .limit(_MAX_ACTION_NODES + 1)
        )
        if lock:
            node_statement = node_statement.with_for_update(of=RunNode)
        all_nodes = tuple(session.scalars(node_statement))
        nodes = all_nodes[:_MAX_ACTION_NODES]

        reservation_statement = (
            select(ResourceReservation)
            .where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == run_id,
                ResourceReservation.kind.in_(_MEMORY_RESERVATION_KINDS),
                ResourceReservation.state == ReservationState.ACTIVE,
            )
            .order_by(
                ResourceReservation.node_id,
                ResourceReservation.kind,
                ResourceReservation.resource_key,
                ResourceReservation.id,
            )
        )
        if lock:
            reservation_statement = reservation_statement.with_for_update(
                of=ResourceReservation
            )
        reservations = tuple(session.scalars(reservation_statement))
        active_by_node = {node.node_id: 0 for node in nodes}
        for reservation in reservations:
            if reservation.node_id in active_by_node:
                active_by_node[reservation.node_id] += reservation.amount_bytes

        full_node_ids = {node.node_id for node in nodes}
        target_node_ids = tuple(
            sorted(
                profile_target_node_ids
                if profile_target_node_ids is not None
                else full_node_ids
            )
        )
        missing_node_ids = tuple(sorted(full_node_ids - set(target_node_ids)))
        profile_scope_exact = profile_target_node_ids is None or (
            len(target_node_ids) == len(set(target_node_ids))
            and bool(target_node_ids)
            and set(target_node_ids) <= full_node_ids
        )
        if profile_target_node_ids is not None and profile_scope_exact:
            target_rows = tuple(
                session.scalars(
                    select(AgentNode)
                    .where(
                        AgentNode.node_id.in_(target_node_ids),
                        AgentNode.revoked_at.is_(None),
                    )
                    .order_by(AgentNode.node_id)
                    .with_for_update(of=AgentNode)
                    if lock
                    else select(AgentNode)
                    .where(
                        AgentNode.node_id.in_(target_node_ids),
                        AgentNode.revoked_at.is_(None),
                    )
                    .order_by(AgentNode.node_id)
                )
            )
            removed_active_rows = tuple(
                session.scalars(
                    select(AgentNode.node_id).where(
                        AgentNode.node_id.in_(missing_node_ids),
                        AgentNode.revoked_at.is_(None),
                    )
                )
            )
            profile_scope_exact = (
                tuple(row.node_id for row in target_rows) == target_node_ids
                and not removed_active_rows
                and len(full_node_ids) >= 2
                and bool(missing_node_ids)
            )

        actual_identity = {(node.node_id, node.rank, node.role) for node in nodes}
        accepted_run = _run_accepted_ranks(session, run, recipe_revision_id)
        accepted_ranks, accepted_exact = (
            (frozenset[tuple[str, int, str]](), False)
            if isinstance(accepted_run, Residue)
            else accepted_run
        )
        immutable_membership_exact = (
            len(all_nodes) <= _MAX_ACTION_NODES
            and accepted_exact
            and accepted_ranks == actual_identity
            and len(actual_identity) == len(nodes)
            and profile_scope_exact
        )
        reservation_membership_exact = (
            len(reservations) == len(nodes)
            and {reservation.node_id for reservation in reservations}
            == {node.node_id for node in nodes}
            and all(
                reservation.plan_digest == run.plan_digest
                for reservation in reservations
            )
            and all(
                active_by_node[node.node_id] == node.reserved_memory_bytes
                for node in nodes
            )
        )
        reservation_facts = tuple(
            {
                "id": reservation.id,
                "node_id": reservation.node_id,
                "kind": reservation.kind,
                "resource_key": reservation.resource_key,
                "amount_bytes": reservation.amount_bytes,
                "plan_digest": reservation.plan_digest,
                "state": reservation.state,
            }
            for reservation in reservations
        )
        return stop_plan(
            run_id=run.id,
            installation_id=run.installation_id,
            recipe_revision_id=recipe_revision_id,
            alias=run.alias,
            run_state=run.state,
            route_state=run.route_state
            if reviewed_route_state is None
            else reviewed_route_state,
            route_generation=run.route_generation,
            route_digest=run.route_digest,
            authority_digest=run.plan_digest,
            nodes=tuple(
                StopNodeImpact(
                    node_id=node.node_id,
                    rank=node.rank,
                    role=node.role,
                    state=node.state,
                    reserved_memory_bytes=node.reserved_memory_bytes,
                    active_memory_reservation_bytes=active_by_node[node.node_id],
                )
                for node in nodes
            ),
            target_node_ids=target_node_ids,
            missing_node_ids=missing_node_ids,
            immutable_membership_exact=immutable_membership_exact,
            reservation_membership_exact=reservation_membership_exact,
            reservation_facts=reservation_facts,
        )

    def _uninstall_plan_in_session(
        self,
        session: Session,
        installation_id: str,
        *,
        lock: bool,
        also_removing: Collection[str] = (),
    ) -> UninstallPlan:
        service = typing_cast("RecipeOperationService", self)
        installation_statement = select(RecipeInstallation).where(
            RecipeInstallation.id == installation_id
        )
        if lock:
            installation_statement = installation_statement.with_for_update(
                of=RecipeInstallation
            )
        installation = session.scalar(installation_statement)
        if installation is None:
            raise RecipeRequestInvalid("recipe installation does not exist")

        revision = _uninstall_recipe(session, installation, lock=lock)
        if revision is None:
            raise RecipeRetryLater(
                "recipe revision authority is unavailable",
                reason=WaitReason.OBSERVATION_UNAVAILABLE,
            )
        node_statement = (
            select(InstallationNode)
            .where(InstallationNode.installation_id == installation_id)
            .order_by(InstallationNode.rank, InstallationNode.node_id)
            .limit(_MAX_ACTION_NODES + 1)
        )
        if lock:
            node_statement = node_statement.with_for_update(of=InstallationNode)
        all_nodes = tuple(session.scalars(node_statement))
        nodes = all_nodes[:_MAX_ACTION_NODES]

        active_count = int(
            session.scalar(
                select(func.count(RecipeRun.id)).where(
                    RecipeRun.installation_id == installation_id,
                    RecipeRun.state != RunState.STOPPED,
                )
            )
            or 0
        )
        active_statement = (
            select(RecipeRun)
            .where(
                RecipeRun.installation_id == installation_id,
                RecipeRun.state != RunState.STOPPED,
            )
            .order_by(RecipeRun.id)
            .limit(_MAX_ACTIVE_RUNS)
        )
        if lock:
            active_statement = active_statement.with_for_update(of=RecipeRun)
        active_runs = tuple(session.scalars(active_statement))

        operation_statement = (
            select(Job)
            .where(
                Job.kind.in_(
                    (
                        WireAgentOperation.RECIPE_UNINSTALL.value,
                        WireAgentOperation.RECIPE_RECONCILE.value,
                    )
                ),
                Job.state.in_(
                    job_states.words(
                        LifecycleState.QUEUED,
                        LifecycleState.RUNNING,
                        LifecycleState.NEEDS_OPERATOR,
                    )
                ),
                Job.payload["owner_id"].as_string() == installation_id,
            )
            .order_by(Job.id)
            .limit(1)
        )
        if lock:
            operation_statement = operation_statement.with_for_update(of=Job)
        active_operation = session.scalar(operation_statement) is not None
        if active_operation and not lock:
            active_reconciliation = session.scalar(
                select(Job.id)
                .where(
                    Job.kind == WireAgentOperation.RECIPE_RECONCILE.value,
                    Job.state.in_(
                        job_states.words(
                            LifecycleState.QUEUED,
                            LifecycleState.RUNNING,
                            LifecycleState.NEEDS_OPERATOR,
                        )
                    ),
                    Job.payload["owner_id"].as_string() == installation_id,
                )
                .limit(1)
            )
            active_uninstalls = _active_owned_workload_jobs(
                session, WireAgentOperation.RECIPE_UNINSTALL.value, installation_id
            )
            scope = tuple(sorted(node.node_id for node in nodes))
            if (
                active_reconciliation is None
                and active_uninstalls
                and all(
                    tuple(sorted(job.targets)) == scope
                    and _unissued_workload_children(session, job) is not None
                    for job in active_uninstalls
                )
            ):
                active_operation = False

        actual_identity = {(node.node_id, node.rank, node.role) for node in nodes}
        accepted = _installation_accepted_ranks(session, installation, revision)
        accepted_ranks, accepted_exact = (
            (frozenset[tuple[str, int, str]](), False)
            if isinstance(accepted, Residue)
            else accepted
        )
        immutable_membership_exact = (
            len(all_nodes) <= _MAX_ACTION_NODES
            and accepted_exact
            and accepted_ranks == actual_identity
            and len(actual_identity) == len(nodes)
        )
        # The model that was installed is the installation row's own record; the
        # recipe document names it too.  When they disagree or the document names
        # none, the installed model wins: cleanup never removes a model it cannot
        # prove unused, and an unproven identity keeps the model.
        document_model = _primary_model_identity(revision.document)
        model_content_sha256, model_title = document_model or (
            installation.model_content_sha256 or "",
            "",
        )
        if installation.model_content_sha256 not in {None, model_content_sha256}:
            retire_as_unknown(
                "recipe.installation-model",
                installation.id,
                BookkeepingReason.EVIDENCE_MISMATCH,
                "installation model differs from its recipe revision",
            )
            model_content_sha256 = installation.model_content_sha256 or ""
            model_title = model_title or model_content_sha256[:12]
        model_title = model_title or model_content_sha256[:12] or "unknown model"
        node_ids = {node.node_id for node in nodes}
        if _lower_hex_digest(model_content_sha256):
            dependent_recipe_ids_by_node = service._model_dependents_on_nodes(
                session,
                model_content_sha256,
                node_ids,
                exclude_installation_ids={installation.id, *also_removing},
                lock=lock,
            )
        else:
            # No provable model identity: every node keeps its model.
            retire_as_unknown(
                "recipe.installation-model",
                installation.id,
                BookkeepingReason.PERSISTED_STATE_DAMAGED,
                "no provable model identity; the model is kept",
            )
            dependent_recipe_ids_by_node = {
                node_id: (revision.document_id,) for node_id in sorted(node_ids)
            }
        return uninstall_plan(
            installation_id=installation.id,
            recipe_id=revision.document_id,
            recipe_revision_id=revision.id,
            recipe_content_sha256=revision.content_digest,
            recipe_content=revision.document,
            original_plan_digest=installation.plan_digest,
            installation_state=installation.state,
            nodes=tuple(
                UninstallNodeImpact(
                    node_id=node.node_id,
                    rank=node.rank,
                    role=node.role,
                    state=node.state,
                    # Failed or partial ranks have no trustworthy byte count;
                    # a planned rank's count proves it never installed.
                    installed_bytes=(
                        node.installed_bytes
                        if node.state
                        in {
                            InstallationNodeState.INSTALLED,
                            InstallationNodeState.PLANNED,
                        }
                        else None
                    ),
                )
                for node in nodes
            ),
            immutable_membership_exact=immutable_membership_exact,
            active_runs=tuple(
                UninstallActiveRun(
                    run_id=run.id,
                    alias=run.alias,
                    state=run.state,
                    route_state=run.route_state,
                )
                for run in active_runs
            ),
            active_run_count=active_count,
            active_runs_truncated=active_count > _MAX_ACTIVE_RUNS,
            active_operation=active_operation,
            model_content_sha256=model_content_sha256,
            model_title=model_title,
            dependent_recipe_ids_by_node=dependent_recipe_ids_by_node,
        )

    def _model_dependents_on_nodes(
        self,
        session: Session,
        model_content_sha256: str,
        node_ids: set[str],
        *,
        exclude_installation_ids: Collection[str],
        lock: bool,
    ) -> dict[str, tuple[str, ...]]:
        statement = select(RecipeInstallation).where(
            RecipeInstallation.state != InstallationState.UNINSTALLED
        )
        if exclude_installation_ids:
            statement = statement.where(
                RecipeInstallation.id.not_in(tuple(exclude_installation_ids))
            )
        if lock:
            statement = statement.with_for_update(of=RecipeInstallation)
        candidates = tuple(session.scalars(statement))
        dependent_recipe_ids: dict[str, set[str]] = {
            node_id: set() for node_id in node_ids
        }
        if not candidates or not node_ids:
            return {node_id: () for node_id in sorted(node_ids)}
        candidate_ids = [item.id for item in candidates]
        memberships: dict[str, set[str]] = {}
        for installation_id, node_id in session.execute(
            select(InstallationNode.installation_id, InstallationNode.node_id).where(
                InstallationNode.installation_id.in_(candidate_ids),
                InstallationNode.node_id.in_(node_ids),
                InstallationNode.state != InstallationNodeState.UNINSTALLED,
            )
        ):
            memberships.setdefault(installation_id, set()).add(node_id)
        for installation in candidates:
            member_nodes = memberships.get(installation.id)
            if not member_nodes:
                continue
            revision = _active_recipe_revision(session, installation.recipe_revision_id)
            # What this installation needs is known from its recipe revision and,
            # failing that, from the model its own row records.  Whatever cannot
            # be proven unused is a dependent: a model is never removed on a guess.
            recipe_id = (
                revision.document_id
                if revision is not None
                else installation.recipe_revision_id
            )
            identity = (
                _primary_model_identity(read_row_column(revision, "document"))
                if revision is not None
                else None
            )
            identities = (
                _recipe_model_identities(session, read_row_column(revision, "document"))
                if revision is not None and identity is not None
                else None
            )
            if identity is None or identities is None:
                retire_as_unknown(
                    "recipe.model-dependents",
                    installation.id,
                    BookkeepingReason.EVIDENCE_UNAVAILABLE,
                    "the installation's model needs are not provable",
                )
                uses = True
            else:
                if installation.model_content_sha256 not in {None, identity[0]}:
                    retire_as_unknown(
                        "recipe.model-dependents",
                        installation.id,
                        BookkeepingReason.EVIDENCE_MISMATCH,
                        "installation model differs from its recipe revision",
                    )
                uses = (
                    any(digest == model_content_sha256 for digest, _title in identities)
                    or installation.model_content_sha256 == model_content_sha256
                )
            if uses:
                for node_id in member_nodes:
                    dependent_recipe_ids[node_id].add(recipe_id)
        return {
            node_id: tuple(sorted(recipe_ids))
            for node_id, recipe_ids in sorted(dependent_recipe_ids.items())
        }
