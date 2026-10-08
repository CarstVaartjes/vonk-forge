"""Run observation for digest-bound recipe operations."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Literal
from typing import cast as typing_cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import AgentOperation as WireAgentOperation
from vonk_agent_protocol import LifecycleState

from .. import job_states
from ..categorized_errors import (
    MissingRecord,
)
from ..lifecycle.evidence import (
    Residue,
)
from ..models import (
    Job,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from ..recipe_progress import (
    _parent_identity as _parent_identity,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_progress import (
    _parent_recovery as _parent_recovery,  # noqa: PLC0414 -- shared helper export
)
from ..recipe_routes import (
    route_health_recovery_pending,
)
from ..run_admission import (
    RunPlan,
)
from .interfaces import (
    RecipeOperationView,
    RecipeRunRankStatus,
    RecipeRunRecoveryOwner,
    RecipeRunStatus,
)
from .observation_helpers import _aware
from .rank_authority import _run_accepted_ranks

if TYPE_CHECKING:
    from .service import RecipeOperationService


class RunObservationMixin:
    def preview_run(
        self,
        installation_id: str,
        alias: str,
        *,
        released_run_ids: Collection[str] = (),
        profile_application_id: str | None = None,
        excluded_profile_application_ids: Sequence[str] = (),
    ) -> RunPlan:
        service = typing_cast("RecipeOperationService", self)
        return service._run_admission.plan_run(
            installation_id,
            alias,
            now=service._clock(),
            released_run_ids=released_run_ids,
            profile_application_id=profile_application_id,
            excluded_profile_application_ids=excluded_profile_application_ids,
        )

    def _adopt_start_in_session(
        self,
        session: Session,
        installation_id: str,
        alias: str,
        *,
        request_id: str,
        plan_digest: str | None,
    ) -> RecipeOperationView | None:
        service = typing_cast("RecipeOperationService", self)
        existing = session.scalar(select(Job).where(Job.request_id == request_id))
        if (
            existing is None
            or existing.kind != WireAgentOperation.RECIPE_START.value
            or _parent_identity(existing, "owner_kind") != "run"
        ):
            return None
        recorded_digest = _parent_identity(existing, "plan_digest")
        if not isinstance(recorded_digest, str):
            return None
        owner_id = _parent_identity(existing, "owner_id")
        if not isinstance(owner_id, str):
            return None
        run = session.get(RecipeRun, owner_id)
        if (
            run is None
            or run.installation_id != installation_id
            or run.alias != alias
            # The run carries the admitted authority this child was queued
            # under, so adopting a child whose run no longer matches is unsafe
            # even when the caller cannot reproduce the original digest.
            or run.plan_digest != recorded_digest
        ):
            return None
        return service._view(existing)

    def replay_start(
        self,
        installation_id: str,
        alias: str,
        *,
        plan_digest: str,
        request_id: str,
    ) -> RecipeOperationView | None:
        """Adopt the original start child when its exact plan is still known."""
        service = typing_cast("RecipeOperationService", self)

        with service._sessions() as session:
            return service._adopt_start_in_session(
                session,
                installation_id,
                alias,
                request_id=request_id,
                plan_digest=plan_digest,
            )

    def adopt_start(
        self,
        installation_id: str,
        alias: str,
        *,
        request_id: str,
    ) -> RecipeOperationView | None:
        """Adopt an already admitted start child before re-reading admission.

        Automatic recovery must bind the child that was actually queued before
        it re-derives mutable admission.  ``preview_run`` hashes node documents
        containing inventory observation time and current memory/reservation
        facts, so a refreshed inventory or the first start's own reservations
        legitimately change the digest.  Reproducing that digest is therefore
        not a requirement for recognising our own durable child: the request
        key, operation kind, owner kind and the run it owns are the identity
        that must match, and the run carries the admitted authority.
        """
        service = typing_cast("RecipeOperationService", self)

        with service._sessions() as session:
            return service._adopt_start_in_session(
                session,
                installation_id,
                alias,
                request_id=request_id,
                plan_digest=None,
            )

    def adopt_owned_operation(
        self,
        request_id: str,
        *,
        kind: str,
        owner_kind: str,
        owner_id: str,
    ) -> RecipeOperationView | None:
        """Adopt the exact child already recorded for this request key.

        Scoped resume of a parent step needs to recognise a child it queued
        earlier without re-deriving a plan digest.  Admission deliberately
        keeps comparing digests for new work; this lookup is only the
        recovery-side identity check, so it requires the same request key,
        operation kind and owner as the durable record.
        """
        service = typing_cast("RecipeOperationService", self)

        with service._sessions() as session:
            existing = session.scalar(select(Job).where(Job.request_id == request_id))
            if (
                existing is None
                or existing.kind != kind
                or _parent_identity(existing, "owner_kind") != owner_kind
                or _parent_identity(existing, "owner_id") != owner_id
            ):
                return None
            return service._view(existing)

    def run_status(self, run_id: str) -> RecipeRunStatus:
        service = typing_cast("RecipeOperationService", self)
        now = _aware(service._clock())
        with service._sessions() as session:
            run = session.get(RecipeRun, run_id)
            if run is None:
                raise MissingRecord(run_id)
            nodes = tuple(
                session.scalars(
                    select(RunNode)
                    .where(RunNode.run_id == run_id)
                    .order_by(RunNode.rank)
                )
            )
            installation = session.get(RecipeInstallation, run.installation_id)
            accepted = _run_accepted_ranks(
                session,
                run,
                installation.recipe_revision_id if installation is not None else "",
            )
            # A plan nobody can read proves no membership: the run is shown not
            # healthy until its ranks are re-established, never refused.
            exact_ranks = (
                not isinstance(accepted, Residue)
                and len(accepted[0]) == len(nodes)
                and accepted[0]
                == {(node.node_id, node.rank, node.role) for node in nodes}
            )
            recovery_jobs = tuple(
                job
                for job in session.scalars(
                    select(Job)
                    .where(
                        Job.kind.in_(
                            {
                                WireAgentOperation.RECIPE_START.value,
                                WireAgentOperation.RECIPE_STOP.value,
                            }
                        ),
                        Job.state.in_(
                            job_states.words(
                                LifecycleState.QUEUED,
                                LifecycleState.RUNNING,
                                LifecycleState.NEEDS_OPERATOR,
                            )
                        ),
                        Job.payload["owner_kind"].as_string() == "run",
                        Job.payload["owner_id"].as_string() == run.id,
                    )
                    .order_by(Job.created_at, Job.id)
                )
                if _parent_recovery(job) is not None
            )
            recovery_owners: list[RecipeRunRecoveryOwner] = []
            for job in recovery_jobs:
                if job.kind == WireAgentOperation.RECIPE_START.value:
                    kind: Literal[
                        WireAgentOperation.RECIPE_START,
                        WireAgentOperation.RECIPE_STOP,
                    ] = WireAgentOperation.RECIPE_START
                elif job.kind == WireAgentOperation.RECIPE_STOP.value:
                    kind = WireAgentOperation.RECIPE_STOP
                else:
                    continue
                if job.state == LifecycleState.QUEUED.value:
                    state = LifecycleState.QUEUED.value
                elif job.state == LifecycleState.RUNNING.value:
                    state = LifecycleState.RUNNING.value
                elif job.state in job_states.words(LifecycleState.NEEDS_OPERATOR):
                    state = LifecycleState.OBSERVING.value
                else:
                    continue
                recovery_owners.append(
                    RecipeRunRecoveryOwner(
                        operation_id=job.id,
                        kind=kind,
                        state=state,
                    )
                )
            ranks: list[RecipeRunRankStatus] = []
            for node in nodes:
                observed_at = _aware(node.updated_at)
                age = now - observed_at
                fresh = timedelta(0) <= age < service._run_health_maximum_age
                ranks.append(
                    RecipeRunRankStatus(
                        node_id=node.node_id,
                        rank=node.rank,
                        role=node.role,
                        state=node.state,
                        observed_at=observed_at,
                        age_seconds=max(0.0, age.total_seconds()),
                        fresh=fresh,
                    )
                )
            return RecipeRunStatus(
                id=run.id,
                alias=run.alias,
                state=run.state,
                route_state=run.route_state,
                healthy=bool(exact_ranks)
                and all(
                    rank.state == LifecycleState.RUNNING.value and rank.fresh
                    for rank in ranks
                ),
                ranks=tuple(ranks),
                run_generation=run.run_generation,
                observation_deadline_at=(
                    _aware(run.observation_deadline_at)
                    if run.observation_deadline_at is not None
                    else None
                ),
                route_error=run.route_error,
                route_next_attempt_at=(
                    _aware(run.route_next_attempt_at)
                    if run.route_next_attempt_at is not None
                    else None
                ),
                route_recovery_pending=route_health_recovery_pending(run.route_error),
                recovery_owners=tuple(recovery_owners),
            )
