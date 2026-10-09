"""Validated atomic LiteLLM publication for database-authoritative recipe runs."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    AgentOperation,
    LifecycleState,
    RoutePublicationState,
    RunState,
    SecurityRefusalError,
    WaitReason,
)
from vonk_agent_protocol import RouteState as RunRouteState
from vonk_agent_protocol.route_activation import ROUTE_EVIDENCE_MAX_AGE_SECONDS

from ..bounded_retry import bounded_attempts
from ..job_documents import DistributedRecoveryMarker
from ..litellm import (
    LiteLlmGeneration,
)
from ..logging import log_event
from ..models import (
    Job,
    RecipeRouteAuthority,
    RecipeRun,
    RoutePublication,
    RoutePublicationOwner,
)
from ..presence import ManagementAddressPolicy
from ..recipe_execution_contract import RouteWithdrawalFollowUp
from ..recipe_lifecycle_contract import RecipeOperationResult
from ..recovery_policy import RecoveryPolicy
from ..route_runtime import (
    RECIPE_ROUTE_AUTHORITY_ID,
    ActivationMarker,
)
from ..stored_documents import RouteClaimMarker
from . import candidate
from . import withdrawal as withdrawal_steps
from .publisher import AtomicRecipeRoutePublisher
from .shared import (
    _LOGGER,
    RECIPE_ROUTE_CLAIM_ID,
    RecipeRecoveryDeadlineError,
    RecipeRouteError,
    RecipeRouteNotReady,
    RecipeRouteSuperseded,
    _ActivatedRecipeRouteError,
    _aware,
    _Claim,
    _claim_ordinal,
    _Publication,
    _RecipeCandidate,
    _RecipeWithdrawal,
    _RecoveryPublication,
    route_publication_transaction,
)


class RecipeRouteService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        publisher: AtomicRecipeRoutePublisher,
        management_policy: ManagementAddressPolicy,
        clock: Callable[[], datetime],
        maximum_age_seconds: int = ROUTE_EVIDENCE_MAX_AGE_SECONDS,
    ) -> None:
        if not 1 <= maximum_age_seconds <= ROUTE_EVIDENCE_MAX_AGE_SECONDS:
            raise ValueError("recipe route evidence age is invalid")
        self.sessions = sessions
        self._publisher = publisher
        self._management_policy = management_policy
        self._clock = clock
        self._maximum_age = timedelta(seconds=maximum_age_seconds)
        self._retained: dict[str, str] = {}

    def publish_run(self, run_id: str) -> LiteLlmGeneration:
        """Observe acquisition in fresh transactions, then reconcile the exact effect."""
        last: Exception | None = None
        for _attempt in bounded_attempts():
            try:
                publication = self._claim_run(run_id)
                break
            except SecurityRefusalError:
                raise
            except RecipeRouteNotReady as error:
                last = error
            except Exception as error:  # noqa: BLE001
                last = error
        else:
            assert last is not None
            raise last
        return self._execute(publication)

    def _claim_run(self, run_id: str) -> _Publication:
        """Claim one run before effects and conditional completion.

        No database transaction is open while the bundle is activated or the
        supervisor acknowledgement is awaited, so a slow LiteLLM reload cannot
        lose a transaction or block a competing route change.
        """

        committed_error: RecipeRouteError | None = None
        publication: _Publication | None = None
        with self.publication_transaction() as session:
            try:
                publication = self._claim_run_publication(session, run_id)
                owner = session.get(RoutePublicationOwner, 1)
                if owner is not None:
                    owner.reconciliation_attempts = 0
                    owner.reconciliation_next_at = None
                    owner.reconciliation_deadline_at = None
            except RecipeRecoveryDeadlineError as error:
                committed_error = error
        if committed_error is not None:
            raise committed_error
        assert publication is not None
        return publication

    def publication_transaction(self) -> AbstractContextManager[Session]:
        return route_publication_transaction(self.sessions)

    def _claim_in_session(self, session: Session, digest: str) -> _Claim:
        """Claim the next publication ordinal inside the owner-locked transaction.

        A newer claim replaces the one in flight, which makes the older
        publication's completion a no-op. The claim row never carries a bundle
        generation, so it cannot collide with the active publication's.
        """

        owner = session.get(RoutePublicationOwner, 1)
        if owner is None:
            raise RecipeRouteNotReady("route publication owner is unavailable")
        now = _aware(self._clock())
        pending = session.get(RoutePublication, RECIPE_ROUTE_CLAIM_ID)
        ordinal = (_claim_ordinal(pending) or 0) + 1
        marker = RouteClaimMarker(claim_ordinal=ordinal).model_dump(mode="json")
        if session.get(RecipeRouteAuthority, RECIPE_ROUTE_CLAIM_ID) is None:
            session.add(
                RecipeRouteAuthority(
                    authority_id=RECIPE_ROUTE_CLAIM_ID, created_at=now, updated_at=now
                )
            )
            session.flush()
        if pending is None:
            session.add(
                RoutePublication(
                    authority_id=RECIPE_ROUTE_CLAIM_ID,
                    state=RoutePublicationState.PUBLICATION_PENDING,
                    plan_digest=digest,
                    activation_marker=marker,
                )
            )
        else:
            pending.state = RoutePublicationState.PUBLICATION_PENDING
            pending.plan_digest = digest
            pending.activation_marker = marker
        session.flush()
        return _Claim(ordinal, owner.owner_generation)

    @staticmethod
    def _claim_current(session: Session, claim: _Claim) -> bool:
        pending = session.get(RoutePublication, RECIPE_ROUTE_CLAIM_ID)
        owner = session.get(RoutePublicationOwner, 1)
        return (
            pending is not None
            and pending.state == RoutePublicationState.PUBLICATION_PENDING
            and _claim_ordinal(pending) == claim.ordinal
            and (owner.owner_generation if owner is not None else 0)
            == claim.base_generation
        )

    def _claim_is_current(self, claim: _Claim) -> bool:
        with self.sessions() as session:
            return self._claim_current(session, claim)

    def _execute(self, publication: _Publication) -> LiteLlmGeneration:
        """Reconcile ordinary unknown outcomes without retaining locks between attempts."""
        last: Exception | None = None
        try:
            for _attempt in bounded_attempts():
                try:
                    return self._execute_once(publication)
                except SecurityRefusalError:
                    raise
                except RecipeRouteNotReady as error:
                    last = error
                except Exception as error:  # noqa: BLE001
                    last = error
            assert last is not None
            raise last
        finally:
            # Cleanup must never replace an authenticated refusal with a local
            # bookkeeping error. A new claim supersedes this abandoned attempt.
            try:
                with self.publication_transaction() as session:
                    if self._claim_current(session, publication.claim):
                        pending = session.get(RoutePublication, RECIPE_ROUTE_CLAIM_ID)
                        if (
                            pending is not None
                            and pending.state
                            == RoutePublicationState.PUBLICATION_PENDING
                        ):
                            pending.state = RoutePublicationState.FAILED
            except Exception as error:  # noqa: BLE001 - bounded owner observation already ended
                _LOGGER.warning("publication claim settlement is unobserved: %s", error)

    def _execute_once(self, publication: _Publication) -> LiteLlmGeneration:
        """Run the effect with no transaction open, then complete conditionally."""

        try:
            if not self._claim_is_current(publication.claim):
                raise RecipeRouteSuperseded("route publication was superseded")
            with self._publisher.fenced(
                lambda: self._effect_claim_current(publication.claim)
            ):
                generation = publication.effect()
        except (SecurityRefusalError, RecipeRouteSuperseded):
            raise
        except Exception as error:
            if not self._claim_is_current(publication.claim):
                # A superseded marker may never be acknowledged; that is the
                # newer publication's business, not a failure of this one.
                raise RecipeRouteSuperseded(
                    "route publication was superseded"
                ) from error
            self._record_failure(publication, error)
            raise
        self._record_completion(publication, generation)
        return generation

    def _record_failure(self, publication: _Publication, error: Exception) -> None:
        if publication.fail is None:
            raise error
        committed_error: RecipeRouteError | None = None
        with self.publication_transaction() as session:
            try:
                publication.fail(session, error)
            except RecipeRecoveryDeadlineError as failure:
                committed_error = failure
        if committed_error is not None:
            self._settle_failed_run_withdrawal(committed_error.run_id)
            raise committed_error from error
        raise error

    def _settle_failed_run_withdrawal(self, run_id: str | None) -> None:
        """Withdraw a failed recovery's route now, with no transaction open.

        The failure is already committed with the owed withdrawal, so a
        refusal here only defers the withdrawal to the next maintenance pass.
        """

        if run_id is None:
            return
        self.maintain()

    def _record_completion(
        self, publication: _Publication, generation: LiteLlmGeneration
    ) -> None:
        committed_error: RecipeRouteError | None = None
        with self.publication_transaction() as session:
            if not self._claim_current(session, publication.claim):
                raise RecipeRouteSuperseded("route publication was superseded")
            pending = session.get(RoutePublication, RECIPE_ROUTE_CLAIM_ID)
            assert pending is not None
            pending.state = RoutePublicationState.COMPLETED
            try:
                publication.complete(session, generation)
            except RecipeRecoveryDeadlineError as failure:
                committed_error = failure
        if committed_error is not None:
            self._settle_failed_run_withdrawal(committed_error.run_id)
            raise committed_error

    def _effect_claim_current(self, claim: _Claim) -> bool:
        # Artifact lock is already held. Never wait for the SQL owner here.
        try:
            with self.sessions.begin() as session:
                owner = session.scalar(
                    select(RoutePublicationOwner)
                    .where(RoutePublicationOwner.singleton_id == 1)
                    .with_for_update(nowait=True)
                )
                if owner is None:
                    return False
                return self._claim_current(session, claim)
        except SQLAlchemyError as error:
            raise RecipeRouteNotReady(
                "route ownership observation is unavailable"
            ) from error

    def _claim_run_publication(self, session: Session, run_id: str) -> _Publication:
        return self._claim_run_publication_once(session, run_id)

    def _claim_run_publication_once(
        self, session: Session, run_id: str
    ) -> _Publication:
        run = session.get(RecipeRun, run_id, with_for_update=True)
        if run is None:
            raise RecipeRouteSuperseded(
                "recipe run is no longer present", run_id=run_id
            )
        if run.state != RunState.RUNNING:
            raise RecipeRouteSuperseded(
                "recipe run is not ready for publication", run_id=run_id
            )
        deadline = self._enforce_recovery_publication_deadline(session, run)
        if isinstance(deadline, RecipeRecoveryDeadlineError):
            raise deadline
        candidate = self.candidate_in_session(
            session,
            include_run_id=run_id,
            exclude_run_ids=frozenset(),
            lock=True,
        )
        if run_id not in candidate.included:
            raise RecipeRouteSuperseded(
                "recipe run is absent from route candidate", run_id=run_id
            )
        claim = self._claim_in_session(session, candidate.state.digest)

        def complete(session: Session, generation: LiteLlmGeneration) -> None:
            self._complete_run_publication(session, run_id, candidate, generation)

        def fail(session: Session, error: Exception) -> None:
            self._fail_run_publication(session, claim, run_id, error)

        return _Publication(
            claim=claim,
            effect=lambda: self._publish(candidate),
            complete=complete,
            fail=fail,
            contained=False,
        )

    def _complete_run_publication(
        self,
        session: Session,
        run_id: str,
        candidate: _RecipeCandidate,
        generation: LiteLlmGeneration,
    ) -> None:
        run = session.get(RecipeRun, run_id, with_for_update=True)
        if run is None or run.state != RunState.RUNNING:
            # The exact activated bundle is still recorded; missing bookkeeping
            # cannot roll back completion or hold the next publisher's claim.
            self.projection_in_session(
                session, generation, state=RoutePublicationState.WITHDRAWAL_PENDING
            )
            log_event(
                _LOGGER,
                "recipe.route.target_changed",
                service="control-routes",
                run_id=run_id,
                reason=WaitReason.SCOPE_CHANGED.value,
            )
            return
        recovery = self._recovery_context(session, run)
        if recovery is not None:
            self._enforce_recovery_publication_deadline(session, run)
        self.projection_in_session(
            session, generation, state=RoutePublicationState.COMPLETED
        )
        if recovery is not None and recovery.job.state != LifecycleState.FAILED:
            result = (
                dict(recovery.job.result)
                if isinstance(recovery.job.result, Mapping)
                else {}
            )
            recovery.job.result = {**result, "recovery_route_published": True}
            recovery.job.updated_at = self._clock()
        for included_id in sorted(candidate.included):
            included = session.get(RecipeRun, included_id, with_for_update=True)
            if included is None or included.state != RunState.RUNNING:
                publication = session.get(RoutePublication, RECIPE_ROUTE_AUTHORITY_ID)
                if publication is not None:
                    publication.state = RoutePublicationState.WITHDRAWAL_PENDING
                continue
            included.route_state = RunRouteState.PUBLISHED
            included.route_generation = generation.generation
            included.route_digest = generation.route_digest
            included.route_error = None
            included.route_attempts = 0
            included.route_next_attempt_at = None
            included.observation_deadline_at = None
            included.updated_at = self._clock()

    def _fail_run_publication(
        self,
        session: Session,
        claim: _Claim,
        run_id: str,
        publication_error: Exception,
    ) -> None:
        run = session.get(RecipeRun, run_id, with_for_update=True)
        recovery = self._recovery_context(session, run) if run is not None else None
        if run is None or recovery is None:
            raise publication_error
        deadline_error = self._enforce_recovery_publication_deadline(session, run)
        if isinstance(deadline_error, RecipeRecoveryDeadlineError):
            failure: RecipeRouteError = deadline_error
        else:
            # Ordinary peer/local representations share the same observation
            # bound. Authenticated refusals bypass this handler unchanged.
            raise publication_error
        # A newer publication owns the bundle record; do not overwrite it.
        generation = (
            publication_error.generation
            if isinstance(publication_error, _ActivatedRecipeRouteError)
            and self._claim_current(session, claim)
            else None
        )
        self._fail_recovery_publication_in_session(
            session, run, recovery, failure, generation=generation
        )
        raise failure from publication_error

    def _fail_recovery_publication_in_session(
        self,
        session: Session,
        run: RecipeRun,
        recovery: _RecoveryPublication,
        failure: RecipeRouteError,
        *,
        generation: LiteLlmGeneration | None,
    ) -> None:
        """End this recovery observation without inferring a stopped workload.

        An activated route remains usable. The next authorized publication
        ignores this ended recovery and acquires a fresh claim normally.
        """

        if generation is not None:
            self.projection_in_session(
                session, generation, state=RoutePublicationState.PUBLICATION_PENDING
            )
        try:
            result = RecipeOperationResult.model_validate_json(
                json.dumps(recovery.job.result)
            )
        except (TypeError, ValueError) as error:
            # A damaged receipt is not evidence of success and cannot prevent
            # this ended observation from releasing its recovery gate.
            _LOGGER.warning("recovery receipt is unavailable: %s", error)
            result = RecipeOperationResult(
                successful_nodes=[], failed_nodes=[], node_evidence={}
            )
        result.recovery_error = str(failure)[:512]
        recovery.job.state = LifecycleState.FAILED
        recovery.job.result = result.model_dump(mode="json", exclude_none=True)
        recovery.job.updated_at = self._clock()
        run.route_error = str(failure)[:512]
        run.updated_at = self._clock()

    def _recovery_job(self, session: Session, run: RecipeRun) -> Job | None:
        """The run's recovery start that still owes its route publication."""

        jobs = session.scalars(
            select(Job)
            .where(
                Job.kind == AgentOperation.RECIPE_START,
                Job.state != LifecycleState.FAILED,
            )
            .order_by(Job.created_at.desc(), Job.id.desc())
        )
        recovery_job = next(
            (
                job
                for job in jobs
                if isinstance(job.payload, Mapping)
                and job.payload.get("owner_id") == run.id
                and job.payload.get("recovery") is not None
            ),
            None,
        )
        if recovery_job is None:
            return None
        result = recovery_job.result
        if (
            isinstance(result, Mapping)
            and result.get("recovery_route_published") is True
        ):
            return None
        return recovery_job

    @staticmethod
    def _recovery_publication(job: Job) -> _RecoveryPublication:
        try:
            marker = DistributedRecoveryMarker.model_validate_json(
                json.dumps(job.payload["recovery"])
            )
        except (TypeError, ValueError, KeyError) as error:
            raise RecipeRouteNotReady("recovery metadata is unavailable") from error
        return _RecoveryPublication(
            job, _aware(datetime.fromisoformat(marker.deadline))
        )

    def _recovery_context(
        self, session: Session, run: RecipeRun
    ) -> _RecoveryPublication | None:
        job = self._recovery_job(session, run)
        return None if job is None else self._recovery_publication(job)

    def _enforce_recovery_publication_deadline(
        self, session: Session, run: RecipeRun
    ) -> _RecoveryPublication | RecipeRecoveryDeadlineError | None:
        recovery_job = self._recovery_job(session, run)
        if recovery_job is None:
            return None
        recovery = self._recovery_publication(recovery_job)
        if _aware(self._clock()) >= recovery.deadline:
            failure = RecipeRecoveryDeadlineError(
                "recovery deadline elapsed", run_id=run.id
            )
            self._fail_recovery_publication_in_session(
                session, run, recovery, failure, generation=None
            )
            return failure
        return recovery

    def withdraw_run(
        self,
        run_id: str,
        *,
        pending: RouteWithdrawalFollowUp | None = None,
        before_withdrawal: Callable[[Session], None] | None = None,
    ) -> LiteLlmGeneration | None:
        return withdrawal_steps.withdraw_run(
            self, run_id, pending=pending, before_withdrawal=before_withdrawal
        )

    def withdraw_runs(
        self,
        run_ids: Iterable[str],
        *,
        pending: RouteWithdrawalFollowUp | None = None,
        before_withdrawal: Callable[[Session], None] | None = None,
    ) -> LiteLlmGeneration | None:
        return withdrawal_steps.withdraw_runs(
            self, run_ids, pending=pending, before_withdrawal=before_withdrawal
        )

    def _withdrawn_after_backoff(self, run_ids: frozenset[str], attempt: int) -> bool:
        return withdrawal_steps._withdrawn_after_backoff(self, run_ids, attempt)

    def _withdraw_runs_once(
        self,
        run_ids: frozenset[str],
        reason: str | None = None,
        before_withdrawal: Callable[[Session], None] | None = None,
    ) -> LiteLlmGeneration:
        return withdrawal_steps._withdraw_runs_once(
            self, run_ids, reason, before_withdrawal
        )

    def withdrawal_complete_in_session(
        self, session: Session, run_ids: Iterable[str]
    ) -> bool:
        return withdrawal_steps.withdrawal_complete_in_session(self, session, run_ids)

    def prepare_withdrawal_in_session(
        self, session: Session, initial_run_ids: frozenset[str]
    ) -> _RecipeWithdrawal:
        return withdrawal_steps.prepare_withdrawal_in_session(
            self, session, initial_run_ids
        )

    def _withdrawal_effect(self, withdrawal: _RecipeWithdrawal) -> LiteLlmGeneration:
        return withdrawal_steps._withdrawal_effect(self, withdrawal)

    def _withdrawal_publication(
        self,
        session: Session,
        withdrawal: _RecipeWithdrawal,
        *,
        after: Callable[[Session], None] | None = None,
    ) -> _Publication:
        return withdrawal_steps._withdrawal_publication(
            self, session, withdrawal, after=after
        )

    def _record_withdrawal_in_session(
        self,
        session: Session,
        withdrawal: _RecipeWithdrawal,
        generation: LiteLlmGeneration,
    ) -> None:
        return withdrawal_steps._record_withdrawal_in_session(
            self, session, withdrawal, generation
        )

    def maintain(self) -> bool:
        for _attempt in bounded_attempts():
            try:
                return self._maintain_once()
            except SecurityRefusalError:
                raise
            except RecipeRouteNotReady:
                continue
            except Exception as error:  # noqa: BLE001 - local observation ends within this budget
                _LOGGER.warning(
                    "route reconciliation observation is unavailable: %s", error
                )
                continue
        return False

    def _maintain_once(self) -> bool:
        now = _aware(self._clock())
        with self.publication_transaction() as session:
            owner = session.get(RoutePublicationOwner, 1)
            if owner is None:
                raise RecipeRouteNotReady("route publication owner is unavailable")
            if owner.reconciliation_next_at is not None and now < _aware(
                owner.reconciliation_next_at
            ):
                return False
            # Standing desired routes survive an ended observation attempt.
            # The next scheduled reconciliation begins a fresh finite budget;
            # neither that schedule nor an old claim gates an explicit request.
            if owner.reconciliation_deadline_at is None or now >= _aware(
                owner.reconciliation_deadline_at
            ):
                owner.reconciliation_attempts = 0
                owner.reconciliation_deadline_at = now + timedelta(minutes=5)
        try:
            result = withdrawal_steps.maintain(self)
        except SecurityRefusalError:
            raise
        except (RecipeRouteNotReady, Exception) as error:  # noqa: BLE001
            with self.publication_transaction() as session:
                owner = session.get(RoutePublicationOwner, 1)
                if owner is None:
                    raise RecipeRouteNotReady("route publication owner is unavailable")
                owner.reconciliation_attempts += 1
                policy = RecoveryPolicy(
                    max_failures=6, base_delay_seconds=5, max_delay_seconds=60
                )
                owner.reconciliation_next_at = policy.next_attempt(
                    RECIPE_ROUTE_AUTHORITY_ID, owner.reconciliation_attempts, now
                )
                if owner.reconciliation_next_at is None:
                    owner.reconciliation_deadline_at = None
                    owner.reconciliation_next_at = now + timedelta(seconds=60)
            log_event(
                _LOGGER,
                "recipe.route.observation_ended",
                service="control-routes",
                reason=str(error),
            )
            return False
        with self.publication_transaction() as session:
            owner = session.get(RoutePublicationOwner, 1)
            if owner is None:
                raise RecipeRouteNotReady("route publication owner is unavailable")
            owner.reconciliation_attempts = 0
            owner.reconciliation_next_at = None
            owner.reconciliation_deadline_at = None
        return result

    def _restore_abandoned_stop_withdrawals(self, session: Session) -> bool:
        return withdrawal_steps._restore_abandoned_stop_withdrawals(self, session)

    def _maintenance_step_in_session(self, session: Session) -> bool | _Publication:
        return withdrawal_steps._maintenance_step_in_session(self, session)

    def _maintain_empty_routes(self, session: Session) -> bool | _Publication:
        return withdrawal_steps._maintain_empty_routes(self, session)

    def _publish(self, candidate: _RecipeCandidate) -> LiteLlmGeneration:
        return self._publisher.publish_recipe(candidate)

    def _publish_empty(self, route_digest: str) -> LiteLlmGeneration:
        return self._publisher.publish_empty(route_digest)

    def projection_in_session(
        self,
        session: Session,
        generation: LiteLlmGeneration,
        *,
        state: RoutePublicationState,
    ) -> None:
        marker = getattr(generation, "activation_marker", None)
        if not isinstance(marker, ActivationMarker):
            return
        now = _aware(self._clock())
        authority = session.get(RecipeRouteAuthority, RECIPE_ROUTE_AUTHORITY_ID)
        if authority is None:
            authority = RecipeRouteAuthority(
                authority_id=RECIPE_ROUTE_AUTHORITY_ID, created_at=now, updated_at=now
            )
            session.add(authority)
            session.flush()
        else:
            authority.updated_at = now
        publication = session.get(RoutePublication, RECIPE_ROUTE_AUTHORITY_ID)
        values = {
            "state": state,
            "generation": marker.generation,
            "plan_digest": marker.plan_digest,
            "route_digest": marker.routes_sha256,
            "litellm_digest": marker.litellm_sha256,
            "bundle_digest": marker.manifest_sha256,
            "activation_marker": marker.model_dump(),
            "activation_marker_digest": marker.digest,
        }
        if publication is None:
            publication = RoutePublication(
                authority_id=RECIPE_ROUTE_AUTHORITY_ID, **values
            )
            session.add(publication)
        else:
            for field, value in values.items():
                setattr(publication, field, value)
        owner = session.get(RoutePublicationOwner, 1)
        if owner is None:
            session.add(
                RoutePublicationOwner(
                    singleton_id=1,
                    authority_id=RECIPE_ROUTE_AUTHORITY_ID,
                    owner_generation=marker.generation,
                    updated_at=now,
                )
            )
        else:
            owner.authority_id = RECIPE_ROUTE_AUTHORITY_ID
            owner.owner_generation = marker.generation
            owner.updated_at = now

    def _note_retained(self, run_id: str, reasons: list[str]) -> None:
        """Say once, when it starts or changes, why a serving route is kept."""

        reason = "; ".join(reasons) if reasons else None
        if self._retained.get(run_id) == reason:
            return
        if reason is None:
            self._retained.pop(run_id, None)
            log_event(
                _LOGGER,
                "recipe.route.evidence_current",
                service="control-routes",
                run_id=run_id,
            )
            return
        self._retained[run_id] = reason
        log_event(
            _LOGGER,
            "recipe.route.kept_without_current_evidence",
            service="control-routes",
            run_id=run_id,
            reason=reason,
        )

    def candidate_in_session(
        self,
        session: Session,
        *,
        include_run_id: str | None,
        exclude_run_ids: frozenset[str],
        lock: bool,
    ) -> _RecipeCandidate:
        return candidate.candidate_in_session(
            self,
            session,
            include_run_id=include_run_id,
            exclude_run_ids=exclude_run_ids,
            lock=lock,
        )
