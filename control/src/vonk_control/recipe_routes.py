"""Validated atomic LiteLLM publication for database-authoritative recipe runs."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import random
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import GatewayRouteState, RoutePublicationState, RunState
from vonk_agent_protocol import RouteState as RunRouteState
from vonk_agent_protocol.route_activation import ROUTE_EVIDENCE_MAX_AGE_SECONDS
from vonk_forge_contracts import read_recipe

from .distributed_lifecycle import DistributedLifecycleError
from .distributed_recovery import enforce_recovery_deadline
from .interface_adapters import InterfaceAdapterError, interface_adapter
from .litellm import (
    LiteLlmGeneration,
    LiteLlmPolicy,
    RouteState,
    render_config,
    render_empty_config,
)
from .logging import log_event
from .models import (
    AgentPresence,
    CatalogDocumentRevision,
    ClusterMapping,
    Job,
    RecipeInstallation,
    RecipeRouteAuthority,
    RecipeRun,
    RoutePublication,
    RoutePublicationOwner,
    RunNode,
)
from .presence import ManagementAddressPolicy, PresenceError
from .recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_run_endpoint,
    run_plan_document,
)
from .route_runtime import (
    RECIPE_ROUTE_AUTHORITY_ID,
    ActivationMarker,
    AtomicRouteBundlePublisher,
    RouteRuntimeError,
)

_ALIAS = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}\Z")
_UPSTREAM_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,119}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_HEALTH_RECOVERY_ERROR = "recipe rank health requires recovery"
# A serving route is kept while its Sparks keep talking to the Controller,
# even when their rank reports are missing; a Spark silent this long is gone.
SPARK_SILENT_WITHDRAWAL_SECONDS = 300
_LOGGER = logging.getLogger(__name__)
_WITHDRAWAL_ATTEMPTS = 5
_WITHDRAWAL_BACKOFF_SECONDS = 0.05
#: Why a running run's route is withdrawn: a Stop claimed it and has not been
#: dispatched. Shown with the run; the maintenance pass restores the route when
#: no Stop follows within the grace period (longer than the acknowledgement wait).
STOP_WITHDRAWAL_PENDING = "route withdrawn for a Stop that is not dispatched yet"
STOP_DISPATCH_GRACE = timedelta(minutes=5)
type WithdrawalFollowUp = Literal["stop", "recovery"]
#: The same, for a distributed recovery that withdrew a failed run's route and
#: has not queued its recovery yet. The existing health-recovery rule restores
#: the route once the ranks are healthy again.
RECOVERY_WITHDRAWAL_PENDING = f"{_HEALTH_RECOVERY_ERROR}: recovery withdrawal pending"
_FOLLOW_UP_REASONS = {
    "stop": STOP_WITHDRAWAL_PENDING,
    "recovery": RECOVERY_WITHDRAWAL_PENDING,
}
_SQLITE_ROUTE_PUBLICATION_LOCK = threading.RLock()
# The one in-flight publication claim lives beside the active publication, in
# its own row, so an observer reading the active publication never sees a
# half-made generation.
RECIPE_ROUTE_CLAIM_ID = str(
    uuid.uuid5(uuid.NAMESPACE_URL, "https://vonkforge.ai/local-recipes/claim")
)


def route_health_recovery_pending(route_error: str | None) -> bool:
    """Return whether a run is waiting for its exact rank-health recovery owner."""

    if route_error is not None and route_error.startswith(
        f"{_HEALTH_RECOVERY_ERROR}: "
    ):
        return True

    return route_error == _HEALTH_RECOVERY_ERROR


class RecipeRouteError(RuntimeError):
    def __init__(self, message: str, *, run_id: str | None = None) -> None:
        super().__init__(message)
        self.run_id = run_id


class RecipeRouteNotReady(RecipeRouteError):
    """A fail-closed route candidate is waiting for current rank evidence."""


class RecipeRouteSuperseded(RecipeRouteNotReady):
    """A newer route change replaced this publication before it completed.

    Nothing was recorded for the superseded publication; the next attempt
    reads the current state again, so this is never a failed attempt.
    """


class RecipeRecoveryDeadlineError(RecipeRouteError):
    pass


class RecipeRecoveryPublicationError(RecipeRouteError):
    pass


class _ActivatedRecipeRouteError(RecipeRouteError):
    def __init__(self, message: str, *, generation: LiteLlmGeneration) -> None:
        super().__init__(message)
        self.generation = generation


def publication_is_temporary(error: BaseException) -> bool:
    """Whether another publication attempt can genuinely resolve ``error``.

    This is deliberately an allowlist.  When the Controller cannot tell a
    dependency hiccup from an invalid contract it must keep the route
    fail-closed and report the failure, not retry it forever; only the
    conditions below are known to carry no authority decision of their own.
    """

    if isinstance(error, RecipeRouteNotReady):
        # A fail-closed candidate is waiting for current rank evidence, which
        # the next attempt re-reads.
        return True
    if isinstance(error, _ActivatedRecipeRouteError):
        # The generation is activated but its supervisor acknowledgement was
        # not confirmed, so a later attempt reconciles that same generation.
        return True
    return isinstance(error, OSError)


@dataclass(frozen=True)
class _RecipeEndpoint:
    node_id: str
    address: str
    port: int
    observed_at: datetime
    operation_id: str

    @property
    def api_base(self) -> str:
        address = ipaddress.ip_address(self.address)
        host = (
            f"[{address}]"
            if isinstance(address, ipaddress.IPv6Address)
            else str(address)
        )
        return f"http://{host}:{self.port}/v1"

    def route_document(self) -> dict[str, object]:
        return {
            "address": self.address,
            "node_id": self.node_id,
            "observed_at": self.observed_at.isoformat(),
            "operation_id": self.operation_id,
            "path": "/v1",
            "port": self.port,
            "scheme": "http",
        }


@dataclass(frozen=True)
class _RecipeCandidate:
    state: RouteState
    included: frozenset[str]
    policy: LiteLlmPolicy
    endpoints: dict[str, _RecipeEndpoint]


@dataclass(frozen=True)
class _RecipeWithdrawal:
    candidate: _RecipeCandidate
    excluded: frozenset[str]
    initial: frozenset[str]


@dataclass(frozen=True)
class _AtomicRecipeGeneration(LiteLlmGeneration):
    activation_marker: ActivationMarker


@dataclass(frozen=True)
class _RecoveryPublication:
    job: Job
    deadline: datetime


@dataclass(frozen=True)
class _Claim:
    """A publication claimed in a short transaction.

    ``ordinal`` counts claims; ``base_generation`` is the owner generation the
    claim was made against. A completion that finds either the claim replaced
    or the owner moved on is superseded and records nothing.
    """

    ordinal: int
    base_generation: int


@dataclass(frozen=True)
class _Publication:
    """One claimed publication: effect with no transaction open, then record."""

    claim: _Claim
    effect: Callable[[], LiteLlmGeneration]
    complete: Callable[[Session, LiteLlmGeneration], None]
    fail: Callable[[Session, Exception], None] | None = None
    # A recovering run's failure is committed on the run; it must not stop the
    # maintenance pass that found it from being reported as progress.
    contained: bool = False


def route_publication_owner_lock_statement():
    """Return the one database row lock shared by every route publisher."""

    return (
        select(RoutePublicationOwner)
        .where(RoutePublicationOwner.singleton_id == 1)
        .with_for_update(of=RoutePublicationOwner)
    )


def lock_route_publication_owner_in_session(
    session: Session,
) -> RoutePublicationOwner:
    """Ensure and lock the singleton owner inside the caller's transaction."""

    statement = route_publication_owner_lock_statement()
    owner = session.scalar(statement)
    if owner is None:
        try:
            with session.begin_nested():
                session.add(
                    RoutePublicationOwner(
                        singleton_id=1,
                        owner_generation=0,
                    )
                )
                session.flush()
        except IntegrityError:
            pass
        owner = session.scalar(statement)
    if owner is None:
        raise RuntimeError("route publication owner is unavailable")
    return owner


@contextmanager
def route_publication_transaction(
    sessions: sessionmaker[Session],
) -> Iterator[Session]:
    """Open one owner-locked publication transaction.

    SQLite ignores ``FOR UPDATE``. Its process-wide lock therefore covers the
    full transaction and external publication, while PostgreSQL relies on the
    singleton row lock and remains safe across controller processes.
    """

    with sessions() as session:
        serialization = (
            _SQLITE_ROUTE_PUBLICATION_LOCK
            if session.get_bind().dialect.name == "sqlite"
            else nullcontext()
        )
        with serialization, session.begin():
            lock_route_publication_owner_in_session(session)
            yield session


class AtomicRecipeRoutePublisher:
    """Adapt recipe routes to the controller's one atomic live bundle."""

    _AUTHORITY_ID = RECIPE_ROUTE_AUTHORITY_ID

    def __init__(self, publisher: AtomicRouteBundlePublisher) -> None:
        self._publisher = publisher

    def publish_recipe(self, candidate: _RecipeCandidate) -> LiteLlmGeneration:
        return self._activate(
            candidate.state.digest,
            render_config(candidate.state, candidate.policy),
            endpoints=candidate.endpoints,
            state=GatewayRouteState.PUBLISHED,
        )

    def publish_empty(self, route_digest: str) -> LiteLlmGeneration:
        return self._activate(
            route_digest,
            render_empty_config(),
            endpoints={},
            state=GatewayRouteState.MAINTENANCE,
        )

    def active_marker_digest(self) -> str | None:
        """Return the live marker digest, or ``None`` when none is readable."""

        try:
            marker = self._publisher._read_marker(optional=True, verify_files=True)
        except RouteRuntimeError:
            return None
        return None if marker is None else marker.digest

    def _next_generation(self) -> int:
        """Allocate above every staged generation, even past an unreadable marker."""

        highest = 0
        for directory in self._publisher._generations.iterdir():
            prefix = directory.name.partition("-")[0]
            if len(prefix) == 8 and prefix.isdigit():
                highest = max(highest, int(prefix))
        return highest + 1

    def _activate(
        self,
        route_digest: str,
        litellm: bytes,
        *,
        endpoints: dict[str, _RecipeEndpoint],
        state: GatewayRouteState,
    ) -> LiteLlmGeneration:
        self._publisher._identity(self._AUTHORITY_ID, route_digest, route_digest)
        acknowledgement_error: Exception | None = None
        with self._publisher._locked():
            try:
                current = self._publisher._read_marker(optional=True, verify_files=True)
            except RouteRuntimeError:
                # An unreadable or retired marker is replaced by a fresh
                # generation instead of blocking publication forever.
                current = None

            def route_bytes(generation: int) -> bytes:
                document: dict[str, object] = {
                    "generation": generation,
                    "routes": {
                        alias: endpoint.route_document()
                        for alias, endpoint in sorted(endpoints.items())
                    },
                    "schema_version": 2,
                    "state": state,
                }
                if state == GatewayRouteState.MAINTENANCE:
                    document["reason"] = "recipe routes withdrawn"
                return (
                    json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
                ).encode()

            # Activation is durable before the supervisor acknowledgement and
            # before the database projection. After a lost acknowledgement,
            # adopt only the exact active, checksum-verified candidate. Merely
            # matching its route digest could reuse an unrelated generation
            # with different rendered bytes.
            reuse_current = (
                current is not None
                and current.state == state
                and current.authority_id == self._AUTHORITY_ID
                and current.plan_digest == route_digest
                and current.evidence_set_digest == route_digest
                and current.routes_sha256
                == hashlib.sha256(route_bytes(current.generation)).hexdigest()
                and current.litellm_sha256 == hashlib.sha256(litellm).hexdigest()
            )
            if reuse_current:
                assert current is not None
                marker = current
            else:
                generation = self._next_generation()
                marker = self._publisher._activate(
                    generation=generation,
                    state=state,
                    authority_id=self._AUTHORITY_ID,
                    plan_digest=route_digest,
                    evidence_set_digest=route_digest,
                    routes=route_bytes(generation),
                    litellm=litellm,
                )
        # The acknowledgement wait can last minutes. It runs after the file
        # lock is released so a newer publication is never queued behind it;
        # a superseded marker simply never acknowledges and its caller
        # discards that result.
        try:
            self._publisher._require_supervisor_ack(marker)
        except Exception as error:  # noqa: BLE001
            acknowledgement_error = error
        config_sha256 = hashlib.sha256(litellm).hexdigest()
        path = str(
            self._publisher._root / "generations" / marker.directory / "litellm.json"
        )
        result = _AtomicRecipeGeneration(
            marker.generation,
            route_digest,
            config_sha256,
            path,
            marker,
        )
        if acknowledgement_error is not None:
            raise _ActivatedRecipeRouteError(
                "recipe route activation acknowledgement failed: "
                f"{acknowledgement_error}",
                generation=result,
            ) from acknowledgement_error
        return result


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
        """Publish one run: claim, then the effect, then record the result.

        No database transaction is open while the bundle is activated or the
        supervisor acknowledgement is awaited, so a slow LiteLLM reload cannot
        lose a transaction or block a competing route change.
        """

        committed_error: RecipeRouteError | None = None
        publication: _Publication | None = None
        with self.publication_transaction() as session:
            try:
                publication = self._claim_run_publication(session, run_id)
            except (
                RecipeRecoveryDeadlineError,
                RecipeRecoveryPublicationError,
            ) as error:
                committed_error = error
        if committed_error is not None:
            raise committed_error
        assert publication is not None
        return self._execute(publication)

    def publication_transaction(self) -> AbstractContextManager[Session]:
        return route_publication_transaction(self.sessions)

    # -- claim, effect, conditional completion ------------------------------

    def _claim_in_session(self, session: Session, digest: str) -> _Claim:
        """Claim the next publication ordinal inside the owner-locked transaction.

        A newer claim replaces the one in flight, which makes the older
        publication's completion a no-op. The claim row never carries a bundle
        generation, so it cannot collide with the active publication's.
        """

        owner = session.get(RoutePublicationOwner, 1)
        if owner is None:
            raise RuntimeError("route publication owner is unavailable")
        now = _aware(self._clock())
        pending = session.get(RoutePublication, RECIPE_ROUTE_CLAIM_ID)
        ordinal = (_claim_ordinal(pending) or 0) + 1
        marker: dict[str, object] = {"claim_ordinal": ordinal}
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
        """Run the effect with no transaction open, then complete conditionally."""

        try:
            if not self._claim_is_current(publication.claim):
                raise RecipeRouteSuperseded("route publication was superseded")
            generation = publication.effect()
        except RecipeRouteSuperseded:
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
            except (
                RecipeRecoveryDeadlineError,
                RecipeRecoveryPublicationError,
            ) as failure:
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
        try:
            with self.publication_transaction() as session:
                publication = self._withdrawal_publication(
                    session,
                    self.prepare_withdrawal_in_session(session, frozenset({run_id})),
                )
            self._execute(publication)
        # External publishers are plug-ins and may fail with any ordinary
        # exception; the committed intent is retried by maintenance.
        except Exception as error:  # noqa: BLE001
            log_event(
                _LOGGER,
                "recipe.route.withdrawal_deferred",
                service="control-routes",
                run_id=run_id,
                reason=type(error).__name__,
            )

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
            except (
                RecipeRecoveryDeadlineError,
                RecipeRecoveryPublicationError,
            ) as failure:
                committed_error = failure
        if committed_error is not None:
            self._settle_failed_run_withdrawal(committed_error.run_id)
            raise committed_error

    # -- publication of one run ---------------------------------------------

    def _claim_run_publication(self, session: Session, run_id: str) -> _Publication:
        run = session.get(RecipeRun, run_id, with_for_update=True)
        if run is None:
            raise KeyError(run_id)
        if run.state != RunState.RUNNING:
            raise RecipeRouteError("recipe run is not ready for publication")
        self._enforce_recovery_publication_deadline(session, run)
        candidate = self.candidate_in_session(
            session,
            include_run_id=run_id,
            exclude_run_ids=frozenset(),
            lock=True,
        )
        if run_id not in candidate.included:
            raise RecipeRouteError("recipe run is absent from route candidate")
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
            raise RecipeRouteError("recipe run changed during publication")
        recovery = self._recovery_context(session, run)
        if recovery is not None:
            try:
                # External activation can cross the bounded recovery window.
                # Recheck before projecting success or recording the marker.
                self._enforce_recovery_publication_deadline(session, run)
            except RecipeRecoveryDeadlineError as deadline_error:
                self._fail_recovery_publication_in_session(
                    session,
                    run,
                    recovery,
                    deadline_error,
                    generation=generation,
                )
                raise
        self.projection_in_session(
            session, generation, state=RoutePublicationState.COMPLETED
        )
        if recovery is not None:
            result = (
                dict(recovery.job.result)
                if isinstance(recovery.job.result, Mapping)
                else {}
            )
            recovery.job.result = {**result, "recovery_route_published": True}
            recovery.job.updated_at = self._clock()
        for included_id in sorted(candidate.included):
            included = session.get(RecipeRun, included_id, with_for_update=True)
            if included is None or included.state != "running":
                raise RecipeRouteError("recipe run changed during publication")
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
        try:
            self._enforce_recovery_publication_deadline(session, run)
        except RecipeRecoveryDeadlineError as deadline_error:
            failure: RecipeRouteError = deadline_error
        else:
            if publication_is_temporary(publication_error):
                # The route remains unpublished while the worker schedules
                # a bounded retry within the original recovery deadline.
                raise publication_error
            failure = RecipeRecoveryPublicationError(
                "recovery route publication failed: "
                f"{type(publication_error).__name__}",
                run_id=run_id,
            )
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
        """Commit one fail-closed state for activation, ack, or deadline failure.

        The failed run's route is withdrawn from the live bundle by the
        maintenance pass: the owed withdrawal is recorded here as durable
        intent, so no transaction waits for the supervisor.
        """

        if generation is not None:
            self.projection_in_session(
                session, generation, state=RoutePublicationState.WITHDRAWAL_PENDING
            )
        recovery_result = (
            dict(recovery.job.result)
            if isinstance(recovery.job.result, Mapping)
            else {}
        )
        recovery.job.state = "failed"
        recovery.job.result = {
            **recovery_result,
            "recovery_error": str(failure),
        }
        recovery.job.updated_at = self._clock()
        run.state = RunState.FAILED
        run.route_state = RunRouteState.WITHDRAWN
        run.route_error = str(failure)[:512]
        publication = session.get(RoutePublication, RECIPE_ROUTE_AUTHORITY_ID)
        if publication is not None:
            publication.state = RoutePublicationState.WITHDRAWAL_PENDING
        run.updated_at = self._clock()

    def _recovery_job(self, session: Session, run: RecipeRun) -> Job | None:
        """The run's recovery start that still owes its route publication."""

        jobs = session.scalars(
            select(Job)
            .where(Job.kind == "recipe.start")
            .order_by(Job.created_at.desc(), Job.id.desc())
        )
        recovery_job = next(
            (
                job
                for job in jobs
                if job.payload.get("owner_id") == run.id
                and isinstance(job.payload.get("recovery"), Mapping)
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
        recovery = job.payload["recovery"]
        assert isinstance(recovery, Mapping)
        deadline = datetime.fromisoformat(str(recovery["deadline"]))
        return _RecoveryPublication(job, _aware(deadline))

    def _recovery_context(
        self, session: Session, run: RecipeRun
    ) -> _RecoveryPublication | None:
        job = self._recovery_job(session, run)
        return None if job is None else self._recovery_publication(job)

    def _enforce_recovery_publication_deadline(
        self, session: Session, run: RecipeRun
    ) -> _RecoveryPublication | None:
        recovery_job = self._recovery_job(session, run)
        if recovery_job is None:
            return None
        try:
            enforce_recovery_deadline(recovery_job.payload, now=self._clock())
        except DistributedLifecycleError as error:
            run.state = RunState.FAILED
            run.route_state = RunRouteState.WITHDRAWN
            run.route_error = str(error)[:512]
            run.updated_at = self._clock()
            raise RecipeRecoveryDeadlineError(str(error), run_id=run.id) from error
        return self._recovery_publication(recovery_job)

    def withdraw_run(
        self, run_id: str, *, pending: WithdrawalFollowUp | None = None
    ) -> LiteLlmGeneration | None:
        """Withdraw one run's route; the caller returns once a bundle without it is live."""

        return self.withdraw_runs(frozenset({run_id}), pending=pending)

    def withdraw_runs(
        self, run_ids: Iterable[str], *, pending: WithdrawalFollowUp | None = None
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
                return self._withdraw_runs_once(run_ids, reason)
            except RecipeRouteSuperseded:
                # A newer publication replaced this claim. It was claimed after
                # our intent committed, so it excludes these runs too: do not
                # supersede it in turn (two withdrawals would take turns
                # superseding each other), wait for it, and claim again only
                # while the bundle still lists them.
                if self._withdrawn_after_backoff(run_ids, attempt):
                    return None
        return self._withdraw_runs_once(run_ids, reason)

    def _withdrawn_after_backoff(self, run_ids: frozenset[str], attempt: int) -> bool:
        time.sleep(random.uniform(0.0, _WITHDRAWAL_BACKOFF_SECONDS) * (attempt + 1))
        with self.publication_transaction() as session:
            return self.withdrawal_complete_in_session(session, run_ids)

    def _withdraw_runs_once(
        self, run_ids: frozenset[str], reason: str | None = None
    ) -> LiteLlmGeneration:
        with self.publication_transaction() as session:
            for run_id in sorted(run_ids):
                if session.get(RecipeRun, run_id, with_for_update=True) is None:
                    raise KeyError(run_id)

            def record_reason(session: Session) -> None:
                for run_id in sorted(run_ids):
                    run = session.get(RecipeRun, run_id, with_for_update=True)
                    if run is not None and run.state == RunState.RUNNING and reason:
                        run.route_error = reason[:512]

            publication = self._withdrawal_publication(
                session,
                self.prepare_withdrawal_in_session(session, run_ids),
                after=record_reason if reason else None,
            )
        return self._execute(publication)

    def withdrawal_complete_in_session(
        self, session: Session, run_ids: Iterable[str]
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
        self, session: Session, initial_run_ids: frozenset[str]
    ) -> _RecipeWithdrawal:
        excluded = set(initial_run_ids)
        while True:
            try:
                candidate = self.candidate_in_session(
                    session,
                    include_run_id=None,
                    exclude_run_ids=frozenset(excluded),
                    lock=True,
                )
                break
            except RecipeRouteError as error:
                if error.run_id is None or error.run_id in excluded:
                    raise
                excluded.add(error.run_id)
        return _RecipeWithdrawal(
            candidate=candidate,
            excluded=frozenset(excluded),
            initial=initial_run_ids,
        )

    def _withdrawal_effect(self, withdrawal: _RecipeWithdrawal) -> LiteLlmGeneration:
        candidate = withdrawal.candidate
        return (
            self._publish(candidate)
            if candidate.state.aliases
            else self._publish_empty(candidate.state.digest)
        )

    def _withdrawal_publication(
        self,
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
        self,
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
                if run_id in withdrawal.initial:
                    raise RecipeRouteError("recipe run changed during withdrawal")
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

        with self.publication_transaction() as session:
            step = self._maintenance_step_in_session(session)
        if isinstance(step, bool):
            return step
        try:
            self._execute(step)
        except RecipeRouteSuperseded:
            pass
        except RecipeRouteError:
            if not step.contained:
                raise
        return True

    def _restore_abandoned_stop_withdrawals(self, session: Session) -> bool:
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

    def _maintenance_step_in_session(self, session: Session) -> bool | _Publication:
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
                .where(RecipeRun.route_state == RunRouteState.PUBLISHED)
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
            try:
                recovery = self._claim_run_publication(session, run.id)
            except RecipeRouteError:
                continue
            return replace(recovery, contained=True)
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
                raise KeyError(error.run_id) from error
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

            return self._withdrawal_publication(
                session, withdrawal, after=record_reason
            )
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

    def _maintain_empty_routes(self, session: Session) -> bool | _Publication:
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

    def _publish(self, candidate: _RecipeCandidate) -> LiteLlmGeneration:
        return self._publisher.publish_recipe(candidate)

    def _publish_empty(self, route_digest: str) -> LiteLlmGeneration:
        return self._publisher.publish_empty(route_digest)

    def projection_in_session(
        self, session: Session, generation: LiteLlmGeneration, *, state: str
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
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RecipeRouteError("recipe route clock must be timezone-aware")
        aliases: dict[str, str] = {}
        upstream_models: dict[str, str] = {}
        endpoints: dict[str, _RecipeEndpoint] = {}
        included: set[str] = set()
        run_identities: list[dict[str, object]] = []
        run_statement = (
            select(RecipeRun)
            .where(
                RecipeRun.state == RunState.RUNNING,
                or_(
                    RecipeRun.route_state == RunRouteState.PUBLISHED,
                    RecipeRun.id == include_run_id,
                ),
            )
            .order_by(RecipeRun.alias, RecipeRun.id)
        )
        if lock:
            run_statement = run_statement.with_for_update(of=RecipeRun)
        runs = tuple(session.scalars(run_statement))
        candidate_runs = tuple(run for run in runs if run.id not in exclude_run_ids)
        node_statement = (
            select(RunNode)
            .where(RunNode.run_id.in_([run.id for run in candidate_runs]))
            .order_by(RunNode.run_id, RunNode.rank, RunNode.node_id)
        )
        if lock:
            node_statement = node_statement.with_for_update(of=RunNode)
        nodes_by_run: dict[str, list[RunNode]] = {run.id: [] for run in candidate_runs}
        if candidate_runs:
            for node in session.scalars(node_statement):
                nodes_by_run[node.run_id].append(node)
        for run in candidate_runs:
            nodes = tuple(nodes_by_run[run.id])
            # A route that already serves stays published while its ranks
            # run: missing, late or unmatched observations are bookkeeping,
            # not evidence the endpoint is unhealthy. A rank that stopped or
            # stayed unready past its grace period is evidence, and fails
            # below as before. A route not yet published still needs current
            # proof before it is first served.
            serving = (
                run.route_state == RunRouteState.PUBLISHED and run.id != include_run_id
            )
            retained: list[str] = []
            if _ALIAS.fullmatch(run.alias) is None or run.alias in aliases:
                raise RecipeRouteError(
                    "recipe run alias is invalid or duplicated", run_id=run.id
                )
            upstream_model = _primary_model_alias(session, run)
            if not nodes or any(node.state != RunState.RUNNING for node in nodes):
                raise RecipeRouteError(
                    "every recipe rank must be running", run_id=run.id
                )
            if tuple(node.rank for node in nodes) != tuple(range(len(nodes))):
                raise RecipeRouteError("recipe rank set is not exact", run_id=run.id)
            try:
                stored_run_plan = run_plan_document(run.plan)
            except RecipeExecutionContractError as error:
                raise RecipeRouteError(
                    "stored recipe run plan is invalid", run_id=run.id
                ) from error
            expected = stored_run_plan.get("nodes")
            if isinstance(expected, list):
                expected_identity = (
                    {
                        (item.get("node_id"), item.get("rank"), item.get("role"))
                        for item in expected
                    }
                    if all(isinstance(item, Mapping) for item in expected)
                    else set()
                )
                actual_identity = {
                    (node.node_id, node.rank, node.role) for node in nodes
                }
                if len(expected) != len(nodes) or expected_identity != actual_identity:
                    raise RecipeRouteError(
                        "recipe rank set does not match accepted plan",
                        run_id=run.id,
                    )
            exact_observations = stored_run_plan.get("observation_schema_version") == 2
            if len(nodes) > 1 and not exact_observations:
                raise RecipeRouteError(
                    "distributed recipe route requires exact rank observations",
                    run_id=run.id,
                )
            mapping = session.get(ClusterMapping, run.mapping_id)
            entrypoints = [node for node in nodes if node.role == "entrypoint"]
            endpoint_owners = (
                [
                    node
                    for node in nodes
                    if node.node_id == mapping.endpoint_owner_node_id
                ]
                if mapping is not None and mapping.generation == run.mapping_generation
                else []
            )
            if (
                len(entrypoints) != 1
                or len(endpoint_owners) != 1
                or entrypoints[0] is not endpoint_owners[0]
            ):
                raise RecipeRouteError(
                    "recipe run must have exactly one mapped endpoint-owner entrypoint",
                    run_id=run.id,
                )
            endpoint_owner = endpoint_owners[0]
            if exact_observations:
                for node in nodes:
                    if (
                        node.observed_run_generation != run.run_generation
                        or node.observation_observed_at is None
                    ):
                        if not serving:
                            raise RecipeRouteNotReady(
                                "recipe rank is awaiting current exact observation",
                                run_id=run.id,
                            )
                        retained.append(
                            f"rank {node.rank} has no current exact observation"
                        )
                    if node is endpoint_owner:
                        if node.observation_endpoint_ready is not True:
                            if not serving:
                                raise RecipeRouteNotReady(
                                    "recipe endpoint owner is awaiting exact readiness",
                                    run_id=run.id,
                                )
                            retained.append(
                                f"rank {node.rank} endpoint readiness is not proven"
                            )
                    elif node.observation_endpoint_ready is not None:
                        if not serving:
                            raise RecipeRouteError(
                                "headless recipe rank exposed endpoint readiness",
                                run_id=run.id,
                            )
                        retained.append(
                            f"headless rank {node.rank} reported endpoint readiness"
                        )
            for node in nodes:
                observed = _aware(node.updated_at)
                if (
                    observed > now.astimezone(UTC)
                    or now.astimezone(UTC) - observed >= self._maximum_age
                ):
                    if not serving:
                        raise RecipeRouteError(
                            "recipe rank readiness evidence is stale", run_id=run.id
                        )
                    age = int((now.astimezone(UTC) - observed).total_seconds())
                    # A Spark that stopped talking to the Controller at all is
                    # evidence, not bookkeeping: its endpoint is gone too.
                    presence = session.get(AgentPresence, node.node_id)
                    silent = (
                        None
                        if presence is None
                        else int(
                            (
                                now.astimezone(UTC) - _aware(presence.observed_at)
                            ).total_seconds()
                        )
                    )
                    if silent is not None and silent >= SPARK_SILENT_WITHDRAWAL_SECONDS:
                        raise RecipeRouteError(
                            f"Spark {node.node_id} has not reached the Controller "
                            f"for {silent}s",
                            run_id=run.id,
                        )
                    retained.append(f"rank {node.rank} evidence is {age}s old")
            self._note_retained(run.id, retained)
            try:
                endpoint = _endpoint(
                    endpoint_owner,
                    self._management_policy,
                    operation_id=f"recipe:{run.id}:rank:{endpoint_owner.rank}",
                )
            except RecipeRouteError as error:
                # Bind the failure to its run so one bad endpoint withdraws
                # only that route instead of blocking every other one.
                raise RecipeRouteError(str(error), run_id=run.id) from error
            aliases[run.alias] = endpoint.api_base
            upstream_models[run.alias] = upstream_model
            included.add(run.id)
            endpoints[run.alias] = endpoint
            run_identities.append(
                {
                    "run_id": run.id,
                    "alias": run.alias,
                    "plan_digest": run.plan_digest,
                    "run_generation": run.run_generation,
                    "upstream_model": upstream_model,
                    # Observation time stays out of route identity so an
                    # otherwise identical heartbeat never generates a new
                    # bundle.
                    "ranks": [
                        {
                            "node_id": node.node_id,
                            "rank": node.rank,
                            "role": node.role,
                        }
                        for node in nodes
                    ],
                }
            )
        identity = {
            "schema_version": 1,
            "runs": run_identities,
            "aliases": aliases,
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        state = RouteState(aliases=aliases, digest=digest)
        policy = LiteLlmPolicy(
            models={
                alias: {
                    "requests_per_minute": 60,
                    "tokens_per_minute": 1_000_000,
                    "upstream_model": upstream_models[alias],
                }
                for alias in aliases
            }
        )
        return _RecipeCandidate(state, frozenset(included), policy, endpoints)


def _primary_model_alias(session: Session, run: RecipeRun) -> str:
    installation = session.get(RecipeInstallation, run.installation_id)
    revision = (
        session.get(CatalogDocumentRevision, installation.recipe_revision_id)
        if installation is not None
        else None
    )
    if (
        revision is None
        or revision.kind != "recipe"
        or revision.schema_version != 2
        or revision.state != "active"
    ):
        raise RecipeRouteError(
            "recipe runtime interface authority is stale", run_id=run.id
        )
    try:
        recipe = read_recipe(revision.document)
    except (TypeError, ValueError) as error:
        raise RecipeRouteError(
            "recipe runtime interface authority is invalid", run_id=run.id
        ) from error
    interfaces = recipe.model_dump(mode="json").get("interfaces")
    interface = None
    if isinstance(interfaces, list):
        for value in interfaces:
            name = value.get("adapter") if isinstance(value, Mapping) else None
            if not isinstance(name, str):
                continue
            try:
                adapter = interface_adapter(name)
            except InterfaceAdapterError as error:
                raise RecipeRouteError(
                    "recipe runtime interface authority is invalid", run_id=run.id
                ) from error
            if adapter.publication == "litellm":
                interface = value
                break
    if interface is None:
        raise RecipeRouteError(
            "recipe run does not declare a LiteLLM interface", run_id=run.id
        )
    model_aliases = (
        interface.get("model_aliases") if isinstance(interface, Mapping) else None
    )
    primary = (
        model_aliases[0] if isinstance(model_aliases, list) and model_aliases else None
    )
    if not isinstance(primary, str) or _UPSTREAM_MODEL.fullmatch(primary) is None:
        raise RecipeRouteError(
            "recipe runtime model authority is invalid", run_id=run.id
        )
    return primary


def _claim_ordinal(claim_row: RoutePublication | None) -> int | None:
    marker = None if claim_row is None else claim_row.activation_marker
    value = marker.get("claim_ordinal") if isinstance(marker, Mapping) else None
    return value if type(value) is int else None


def _aware(value: datetime) -> datetime:
    return (
        value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    ).astimezone(UTC)


def _endpoint(
    node: RunNode,
    management_policy: ManagementAddressPolicy,
    *,
    operation_id: str,
) -> _RecipeEndpoint:
    try:
        endpoint = parse_stored_run_endpoint(node.endpoint)
    except RecipeExecutionContractError as error:
        raise RecipeRouteError("entrypoint endpoint is invalid") from error
    if endpoint is None:
        raise RecipeRouteError("entrypoint endpoint evidence is missing")
    raw = endpoint.url
    try:
        parsed = urlsplit(raw)
        raw_address = parsed.hostname or ""
        address = ipaddress.ip_address(raw_address)
        port = parsed.port
    except ValueError as error:
        raise RecipeRouteError("entrypoint endpoint is invalid") from error
    if (
        parsed.scheme != "http"
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or port != node.port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") not in {"", "/v1"}
    ):
        raise RecipeRouteError("entrypoint endpoint is outside management policy")
    try:
        management_policy.validate(str(address))
    except PresenceError as error:
        raise RecipeRouteError(
            "entrypoint endpoint is outside management policy"
        ) from error
    assert port is not None
    return _RecipeEndpoint(
        node_id=node.node_id,
        address=str(address),
        port=port,
        observed_at=_aware(node.updated_at),
        operation_id=operation_id,
    )


__all__ = [
    "AtomicRecipeRoutePublisher",
    "RecipeRouteError",
    "RecipeRouteNotReady",
    "RecipeRouteService",
    "RecipeRouteSuperseded",
    "publication_is_temporary",
]
