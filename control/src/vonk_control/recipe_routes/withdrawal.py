"""Validated atomic LiteLLM publication for database-authoritative recipe runs."""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    RoutePublicationState,
    RunState,
    SecurityRefusalError,
    WaitReason,
)
from vonk_agent_protocol import RouteState as RunRouteState
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState

from ..bounded_retry import bounded_attempts
from ..litellm import (
    LiteLlmGeneration,
)
from ..logging import log_event
from ..models import (
    Job,
    RecipeRun,
    RoutePublication,
    RoutePublicationOwner,
)
from ..route_runtime import (
    RECIPE_ROUTE_AUTHORITY_ID,
)
from .shared import (
    _FOLLOW_UP_REASONS,
    _HEALTH_RECOVERY_ERROR,
    _LOGGER,
    _WITHDRAWAL_ATTEMPTS,
    _WITHDRAWAL_BACKOFF_SECONDS,
    STOP_DISPATCH_GRACE,
    STOP_WITHDRAWAL_PENDING,
    RecipeEndpointAuthorityRefused,
    RecipeRankStopped,
    RecipeRouteError,
    RecipeRouteNotReady,
    RecipeRouteSuperseded,
    WithdrawalFollowUp,
    _aware,
    _Publication,
    _RecipeCandidate,
    _RecipeWithdrawal,
)

if TYPE_CHECKING:
    from .service import RecipeRouteService


def withdraw_run(
    self: RecipeRouteService,
    run_id: str,
    *,
    pending: WithdrawalFollowUp | None = None,
    before_withdrawal: Callable[[Session], None] | None = None,
) -> LiteLlmGeneration | None:
    """Withdraw one run's route; the caller returns once a bundle without it is live."""

    return self.withdraw_runs(
        frozenset({run_id}), pending=pending, before_withdrawal=before_withdrawal
    )


def withdraw_runs(
    self: RecipeRouteService,
    run_ids: Iterable[str],
    *,
    pending: WithdrawalFollowUp | None = None,
    before_withdrawal: Callable[[Session], None] | None = None,
) -> LiteLlmGeneration | None:
    """Withdraw these runs' routes with no transaction held by the caller.

    Claim in a short transaction (the withdrawal intent is durable with the
    claim, and the maintenance pass converges the live bundle on it after a
    crash), publish and await the acknowledgement with none open, then
    complete conditionally. Lifecycle paths that must see the route gone
    before they act call this first and then re-check
    :meth:`withdrawal_complete_in_session` in their own short transaction.

    The generation is ``None`` when a newer publication, which already
    excludes these runs through their durable intent, completed the
    withdrawal for us.

    ``pending`` names the follow-up the caller owes (a Stop dispatch or a
    recovery). The runs show that reason meanwhile and the caller replaces
    it when it follows up; if it never does, the Controller resolves it
    itself (a Stop's withdrawal is restored by the maintenance pass, a
    recovery's by the rank-health rule).
    """
    reason = _FOLLOW_UP_REASONS[pending] if pending is not None else None

    run_ids = frozenset(run_ids)
    for attempt in range(_WITHDRAWAL_ATTEMPTS - 1):
        try:
            return self._withdraw_runs_once(run_ids, reason, before_withdrawal)
        except RecipeRouteSuperseded:
            # A newer publication replaced this claim. It was claimed after
            # our intent committed, so it excludes these runs too: do not
            # supersede it in turn (two withdrawals would take turns
            # superseding each other), wait for it, and claim again only
            # while the bundle still lists them.
            if self._withdrawn_after_backoff(run_ids, attempt):
                return None
    return self._withdraw_runs_once(run_ids, reason, before_withdrawal)


def _withdrawn_after_backoff(
    self: RecipeRouteService, run_ids: frozenset[str], attempt: int
) -> bool:
    time.sleep(random.uniform(0.0, _WITHDRAWAL_BACKOFF_SECONDS) * (attempt + 1))
    with self.publication_transaction() as session:
        return self.withdrawal_complete_in_session(session, run_ids)


def _withdraw_runs_once(
    self: RecipeRouteService,
    run_ids: frozenset[str],
    reason: str | None = None,
    before_withdrawal: Callable[[Session], None] | None = None,
) -> LiteLlmGeneration:
    with self.publication_transaction() as session:

        def record_reason(session: Session) -> None:
            for run_id in sorted(run_ids):
                run = session.get(RecipeRun, run_id, with_for_update=True)
                if run is not None and run.state == RunState.RUNNING and reason:
                    run.route_error = reason[:512]

        withdrawal = self.prepare_withdrawal_in_session(session, run_ids)
        if before_withdrawal is not None:
            before_withdrawal(session)
        publication = self._withdrawal_publication(
            session,
            withdrawal,
            after=record_reason if reason else None,
        )
    return self._execute(publication)


def withdrawal_complete_in_session(
    self: RecipeRouteService, session: Session, run_ids: Iterable[str]
) -> bool:
    """Whether the live bundle is the one without these runs' routes.

    Call inside :meth:`publication_transaction`. The withdrawal intent on a
    run says what should be live; this says it is: the completed
    publication, still the live marker, is the one a candidate without the
    runs would produce. A withdrawal that was claimed and never executed,
    or superseded by a publication that has not completed, is not complete.
    """

    run_ids = frozenset(run_ids)
    candidate = self.prepare_withdrawal_in_session(session, run_ids).candidate
    owner = session.get(RoutePublicationOwner, 1)
    publication = (
        session.get(RoutePublication, RECIPE_ROUTE_AUTHORITY_ID)
        if owner is not None and owner.authority_id == RECIPE_ROUTE_AUTHORITY_ID
        else None
    )
    if publication is None:
        # No publication is on record (the real publisher records one with
        # every activation, so this is only a stand-in publisher or a
        # database that never published): the runs' own route state is the
        # only evidence there is.
        return not any(
            run.route_state == RunRouteState.PUBLISHED
            for run in session.scalars(
                select(RecipeRun).where(RecipeRun.id.in_(set(run_ids)))
            )
        )
    assert owner is not None
    return (
        publication.state
        in (RoutePublicationState.COMPLETED, RoutePublicationState.ROUTES_WITHDRAWN)
        and publication.generation == owner.owner_generation
        and publication.plan_digest == candidate.state.digest
        and self._publisher.active_marker_digest()
        == publication.activation_marker_digest
    )


def prepare_withdrawal_in_session(
    self: RecipeRouteService, session: Session, initial_run_ids: frozenset[str]
) -> _RecipeWithdrawal:
    excluded = set(initial_run_ids)
    run_count = len(tuple(session.scalars(select(RecipeRun.id))))
    candidate: _RecipeCandidate | None = None
    for _attempt in range(run_count + 1):
        try:
            candidate = self.candidate_in_session(
                session,
                include_run_id=None,
                exclude_run_ids=frozenset(excluded),
                lock=True,
            )
            break
        except RecipeRouteError as error:
            if (
                error.run_id is None
                or error.run_id in excluded
                or not isinstance(
                    error, RecipeRankStopped | RecipeEndpointAuthorityRefused
                )
            ):
                raise
            excluded.add(error.run_id)
    assert candidate is not None
    return _RecipeWithdrawal(
        candidate=candidate,
        excluded=frozenset(excluded),
        initial=initial_run_ids,
    )


def _withdrawal_effect(
    self: RecipeRouteService, withdrawal: _RecipeWithdrawal
) -> LiteLlmGeneration:
    candidate = withdrawal.candidate
    return (
        self._publish(candidate)
        if candidate.state.aliases
        else self._publish_empty(candidate.state.digest)
    )


def _withdrawal_publication(
    self: RecipeRouteService,
    session: Session,
    withdrawal: _RecipeWithdrawal,
    *,
    after: Callable[[Session], None] | None = None,
) -> _Publication:
    claim = self._claim_in_session(session, withdrawal.candidate.state.digest)
    # A withdrawal is a safety decision: its intent is durable with the
    # claim, so a newer publication that supersedes this one already
    # excludes these runs and no withdrawal is ever lost.
    for run_id in sorted(withdrawal.excluded):
        run = session.get(RecipeRun, run_id, with_for_update=True)
        if run is not None:
            run.route_state = RunRouteState.WITHDRAWN
            run.updated_at = self._clock()
    if after is not None:
        # Applied now so a superseding publication keeps the reason, and
        # again at completion, which resets the withdrawn runs' fields.
        after(session)

    def complete(session: Session, generation: LiteLlmGeneration) -> None:
        self._record_withdrawal_in_session(session, withdrawal, generation)
        if after is not None:
            after(session)

    return _Publication(
        claim=claim,
        effect=lambda: self._withdrawal_effect(withdrawal),
        complete=complete,
    )


def _record_withdrawal_in_session(
    self: RecipeRouteService,
    session: Session,
    withdrawal: _RecipeWithdrawal,
    generation: LiteLlmGeneration,
) -> None:
    candidate = withdrawal.candidate
    self.projection_in_session(
        session,
        generation,
        state=(
            RoutePublicationState.COMPLETED
            if candidate.state.aliases
            else RoutePublicationState.ROUTES_WITHDRAWN
        ),
    )
    for included_id in sorted(candidate.included):
        included = session.get(RecipeRun, included_id, with_for_update=True)
        if included is not None:
            included.route_state = RunRouteState.PUBLISHED
            included.route_generation = generation.generation
            included.route_digest = generation.route_digest
            included.route_error = None
            included.route_attempts = 0
            included.route_next_attempt_at = None
            included.updated_at = self._clock()
    for run_id in sorted(withdrawal.excluded):
        run = session.get(RecipeRun, run_id, with_for_update=True)
        if run is None:
            log_event(
                _LOGGER,
                "recipe.route.target_gone",
                service="control-routes",
                run_id=run_id,
                reason=WaitReason.SCOPE_CHANGED.value,
            )
            continue
        run.route_state = RunRouteState.WITHDRAWN
        run.route_generation = generation.generation
        run.route_digest = generation.route_digest
        run.route_error = None
        run.updated_at = self._clock()


def maintain(self) -> bool:
    """Converge the live bundle on the served route set; no periodic renewal.

    The pass decides in one short transaction, performs the bundle effect
    with none open, and records the result conditionally.
    """

    last: Exception | None = None
    for _attempt in bounded_attempts():
        try:
            with self.publication_transaction() as session:
                step = self._maintenance_step_in_session(session)
            if isinstance(step, bool):
                return step
            self._execute(step)
            return True
        except SecurityRefusalError:
            raise
        except RecipeRouteNotReady as error:
            last = error
        except Exception as error:  # noqa: BLE001
            last = error
    assert last is not None
    raise last


def _restore_abandoned_stop_withdrawals(
    self: RecipeRouteService, session: Session
) -> bool:
    """Put back the route of a running run whose Stop never followed.

    A Stop withdraws the route first and dispatches afterwards. If the
    request died in between, nothing owns the withdrawal any more: the run
    still runs, so its route is published again (a pending run is published
    by the worker) instead of waiting for a client to retry.
    """

    now = _aware(self._clock())
    restored = False
    for run in session.scalars(
        select(RecipeRun)
        .where(
            RecipeRun.state == RunState.RUNNING,
            RecipeRun.route_state == RunRouteState.WITHDRAWN,
            RecipeRun.route_error == STOP_WITHDRAWAL_PENDING,
        )
        .with_for_update(of=RecipeRun)
    ):
        accepted_stop = session.scalar(
            select(Job.id)
            .where(
                Job.kind == "recipe.stop",
                Job.state == LifecycleState.RUNNING.value,
                Job.payload["owner_id"].as_string() == run.id,
                Job.payload["service_stop_review"]["stage"]
                .as_string()
                .in_(["accepted", "withdrawal-claimed"]),
            )
            .limit(1)
        )
        if accepted_stop is not None:
            continue
        if _aware(run.updated_at) > now - STOP_DISPATCH_GRACE:
            continue
        run.route_state = RunRouteState.PENDING
        run.route_error = None
        run.route_attempts = 0
        run.route_next_attempt_at = None
        run.updated_at = now
        log_event(
            _LOGGER,
            "recipe.route.restored",
            service="control-routes",
            run_id=run.id,
            reason="a Stop withdrew the route and was never dispatched",
        )
        restored = True
    return restored


def _maintenance_step_in_session(
    self: RecipeRouteService, session: Session
) -> bool | _Publication:
    if self._restore_abandoned_stop_withdrawals(session):
        return True
    pending = session.get(RoutePublication, RECIPE_ROUTE_AUTHORITY_ID)
    if (
        pending is not None
        and pending.state == RoutePublicationState.WITHDRAWAL_PENDING
    ):
        return self._withdrawal_publication(
            session, self.prepare_withdrawal_in_session(session, frozenset())
        )
    published = tuple(
        session.scalars(
            select(RecipeRun)
            .where(
                RecipeRun.route_state.in_(
                    [RunRouteState.PUBLISHED, RunRouteState.FAILED]
                )
            )
            .order_by(RecipeRun.created_at, RecipeRun.id)
            .with_for_update(of=RecipeRun)
        )
    )
    recovering = tuple(
        session.scalars(
            select(RecipeRun)
            .where(
                RecipeRun.state == RunState.RUNNING,
                RecipeRun.route_state == RunRouteState.WITHDRAWN,
                RecipeRun.route_error.startswith(_HEALTH_RECOVERY_ERROR),
            )
            .order_by(RecipeRun.created_at, RecipeRun.id)
            .with_for_update(of=RecipeRun)
        )
    )
    for run in recovering:
        # Give the request worker a fresh observation budget without holding
        # this maintenance transaction while publishing or retrying a rank.
        run.route_state = RunRouteState.PENDING
        run.route_attempts = 0
        run.route_next_attempt_at = self._clock()
    if not published:
        return self._maintain_empty_routes(session)
    not_running = frozenset(
        run.id for run in published if run.state != RunState.RUNNING
    )
    if not_running:
        for run in published:
            if run.id in not_running:
                log_event(
                    _LOGGER,
                    "recipe.route.withdrawn",
                    service="control-routes",
                    run_id=run.id,
                    reason=f"run is {run.state}",
                )
        return self._withdrawal_publication(
            session, self.prepare_withdrawal_in_session(session, not_running)
        )
    try:
        candidate = self.candidate_in_session(
            session,
            include_run_id=None,
            exclude_run_ids=frozenset(),
            lock=True,
        )
    except RecipeRouteError as error:
        if error.run_id is None:
            raise
        if not isinstance(error, RecipeRankStopped | RecipeEndpointAuthorityRefused):
            # Failure to reconstruct a candidate is not evidence that an
            # already accepted endpoint stopped serving. Initial route
            # publication still validates every candidate field.
            self._note_retained(error.run_id, [str(error)])
            raise
        published_ids = frozenset(run.id for run in published)
        recovery_error = f"{_HEALTH_RECOVERY_ERROR}: {error}"[:512]
        log_event(
            _LOGGER,
            "recipe.route.withdrawn",
            service="control-routes",
            run_id=error.run_id,
            reason=str(error),
        )
        if session.get(RecipeRun, error.run_id, with_for_update=True) is None:
            return False
        withdrawal = self.prepare_withdrawal_in_session(
            session, frozenset({error.run_id})
        )

        def record_reason(session: Session) -> None:
            withdrawn = tuple(
                session.scalars(
                    select(RecipeRun).where(
                        RecipeRun.id.in_(published_ids),
                        RecipeRun.state == RunState.RUNNING,
                        RecipeRun.route_state == RunRouteState.WITHDRAWN,
                    )
                )
            )
            for run in withdrawn:
                # The reason travels with the run so Fleet can show it;
                # recovery republishes once the ranks are healthy again.
                run.route_error = recovery_error
                run.updated_at = self._clock()

        return self._withdrawal_publication(session, withdrawal, after=record_reason)
    owner = session.get(RoutePublicationOwner, 1)
    publication = (
        session.get(RoutePublication, RECIPE_ROUTE_AUTHORITY_ID)
        if owner is not None and owner.authority_id == RECIPE_ROUTE_AUTHORITY_ID
        else None
    )
    current_digest = publication.plan_digest if publication is not None else None
    durable_current = (
        owner is not None
        and publication is not None
        and publication.state == RoutePublicationState.COMPLETED
        and publication.generation == owner.owner_generation
        # A missing, replaced or unreadable live marker is republished.
        and self._publisher.active_marker_digest()
        == publication.activation_marker_digest
    )
    candidate_changed = current_digest != candidate.state.digest
    if not durable_current or candidate_changed:
        claim = self._claim_in_session(session, candidate.state.digest)

        def complete(session: Session, generation: LiteLlmGeneration) -> None:
            self.projection_in_session(
                session, generation, state=RoutePublicationState.COMPLETED
            )
            for run_id in sorted(candidate.included):
                run = session.get(RecipeRun, run_id, with_for_update=True)
                if run is not None:
                    run.route_generation = generation.generation
                    run.route_digest = generation.route_digest
                    run.route_error = None
                    run.updated_at = self._clock()

        return _Publication(
            claim=claim,
            effect=lambda: self._publish(candidate),
            complete=complete,
        )
    return False


def _maintain_empty_routes(
    self: RecipeRouteService, session: Session
) -> bool | _Publication:
    candidate = self.candidate_in_session(
        session,
        include_run_id=None,
        exclude_run_ids=frozenset(),
        lock=True,
    )
    if candidate.included or candidate.endpoints or candidate.state.aliases:
        return False
    owner = session.get(RoutePublicationOwner, 1)
    publication = session.get(RoutePublication, RECIPE_ROUTE_AUTHORITY_ID)
    if (
        owner is not None
        and owner.authority_id == RECIPE_ROUTE_AUTHORITY_ID
        and publication is not None
        and publication.state == RoutePublicationState.COMPLETED
        and publication.generation == owner.owner_generation
    ):
        # The live bundle still lists models although no run is published:
        # a lifecycle path withdrew the last route only in the database.
        return self._withdrawal_publication(
            session, self.prepare_withdrawal_in_session(session, frozenset())
        )
    return False
