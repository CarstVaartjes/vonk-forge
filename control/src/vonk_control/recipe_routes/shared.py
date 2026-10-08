"""Validated atomic LiteLLM publication for database-authoritative recipe runs."""

from __future__ import annotations

import ipaddress
import logging
import re
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    SecurityRefusalError,
    SecurityRefusalReason,
    UnknownOutcomeError,
    WaitReason,
)
from vonk_agent_protocol.enrollment import NodeId

from ..bounded_retry import bounded_attempts
from ..litellm import (
    LiteLlmGeneration,
    LiteLlmPolicy,
    RouteState,
)
from ..models import (
    Job,
    RoutePublication,
    RoutePublicationOwner,
)
from ..route_bundle_contract import (
    RouteEndpointDocument,
)
from ..route_runtime import (
    ActivationMarker,
)

_NODE_ID = TypeAdapter(NodeId)
_ALIAS = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}\Z")
_UPSTREAM_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,119}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_HEALTH_RECOVERY_ERROR = "recipe rank health requires recovery"
# A serving route is kept while its Sparks keep talking to the Controller,
# even when their rank reports are missing; a Spark silent this long is gone.
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


class RecipeRankStopped(RecipeRouteError):
    """A durable rank reports a stopped or failed workload, requiring recovery."""


class RecipeEndpointAuthorityRefused(SecurityRefusalError, RecipeRouteError):
    """A route violates the enforced network or node-revocation boundary."""

    def __init__(self, message: str, *, run_id: str | None = None) -> None:
        SecurityRefusalError.__init__(
            self, message, reason=SecurityRefusalReason.FORBIDDEN
        )
        self.run_id = run_id


class RecipeRouteNotReady(UnknownOutcomeError, RecipeRouteError):
    """Bounded observation of missing or unmatched publication evidence."""

    def __init__(self, message: str, *, run_id: str | None = None) -> None:
        UnknownOutcomeError.__init__(
            self, message, reason=WaitReason.OBSERVATION_UNAVAILABLE
        )
        self.run_id = run_id


class RecipeRouteSuperseded(RecipeRouteNotReady):
    """A newer route change replaced this publication before it completed.

    Nothing was recorded for the superseded publication; the next attempt
    reads the current state again, so this is never a failed attempt.
    """

    def __init__(self, message: str, *, run_id: str | None = None) -> None:
        UnknownOutcomeError.__init__(self, message, reason=WaitReason.SCOPE_CHANGED)
        self.run_id = run_id


class RecipeRecoveryDeadlineError(UnknownOutcomeError, RecipeRouteError):
    """The bounded recovery scope ended before publication was confirmed."""

    def __init__(self, message: str, *, run_id: str | None = None) -> None:
        UnknownOutcomeError.__init__(self, message, reason=WaitReason.SCOPE_CHANGED)
        self.run_id = run_id


class _ActivatedRecipeRouteError(UnknownOutcomeError, RecipeRouteError):
    def __init__(self, message: str, *, generation: LiteLlmGeneration) -> None:
        UnknownOutcomeError.__init__(
            self, message, reason=WaitReason.RUNTIME_EFFECT_UNCONFIRMED
        )
        self.run_id = None
        self.generation = generation


def publication_is_temporary(error: BaseException) -> bool:
    """Unknown peer/local outcomes are reconciled within the owning bound."""
    return not isinstance(error, SecurityRefusalError)


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

    def route_document(self) -> RouteEndpointDocument:
        return RouteEndpointDocument(
            address=self.address,
            node_id=self.node_id,
            observed_at=self.observed_at.isoformat(),
            operation_id=self.operation_id,
            path="/v1",
            port=self.port,
            scheme="http",
        )


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
        .with_for_update(of=RoutePublicationOwner, nowait=True)
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
        raise RecipeRouteNotReady("route publication owner is unavailable")
    return owner


@contextmanager
def route_publication_transaction(
    sessions: sessionmaker[Session],
) -> Iterator[Session]:
    """Reobserve acquisition in fresh transactions; never retry the caller's body."""
    last: RecipeRouteNotReady | None = None
    for _attempt in bounded_attempts():
        stack = ExitStack()
        try:
            session = stack.enter_context(sessions())
            if session.get_bind().dialect.name == "sqlite":
                if not _SQLITE_ROUTE_PUBLICATION_LOCK.acquire(blocking=False):
                    raise RecipeRouteNotReady("route publication owner is busy")
                stack.callback(_SQLITE_ROUTE_PUBLICATION_LOCK.release)
            stack.enter_context(session.begin())
            lock_route_publication_owner_in_session(session)
            break
        except RecipeRouteNotReady as error:
            last = error
            session.rollback()
            stack.close()
        except SQLAlchemyError:
            last = RecipeRouteNotReady("route publication owner is unavailable")
            session.rollback()
            stack.close()
    else:
        assert last is not None
        raise last
    with stack:
        yield session


def _claim_ordinal(claim_row: RoutePublication | None) -> int | None:
    marker = None if claim_row is None else claim_row.activation_marker
    value = marker.get("claim_ordinal") if isinstance(marker, Mapping) else None
    return value if type(value) is int else None


def _aware(value: datetime) -> datetime:
    return (
        value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    ).astimezone(UTC)
