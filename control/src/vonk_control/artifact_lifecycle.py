"""Coordinate exact managed-object references with cooperative removal.

The SQL gate owns only a short-lived serialization point and a durable
deletion fence. Bytes and availability receipts remain owned by managed
storage; request, profile, workload, and distribution rows remain the owners of
their exact references.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    ArtifactLifecycleCode,
    InvalidRequestReason,
    LifecycleState,
    SecurityRefusalError,
    SecurityRefusalReason,
    UnknownOutcomeError,
    WaitReason,
)

from . import job_states, model_cache_states
from .categorized_errors import InvalidValue
from .models import ArtifactLifecycleGate, Job, ModelCacheOperation

ArtifactKind = Literal["model-set", "model-object", "runtime-image"]
RemovalOwnerKind = Literal["model-cache-operation", "recipe-image-job"]
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_RETRYABLE_SQLSTATES = frozenset({"55P03", "40P01", "40001"})


@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
    kind: ArtifactKind
    sha256: str

    def __post_init__(self) -> None:
        if self.kind not in {"model-set", "model-object", "runtime-image"}:
            raise InvalidValue(
                "artifact lifecycle kind is invalid",
                reason=InvalidRequestReason.UNSUPPORTED,
            )
        if not isinstance(self.sha256, str) or _DIGEST.fullmatch(self.sha256) is None:
            raise InvalidValue(
                "artifact lifecycle digest is invalid",
                reason=InvalidRequestReason.MALFORMED,
            )


def _identity_order(identity: ArtifactIdentity) -> tuple[int, str]:
    return (
        {"model-set": 0, "model-object": 1, "runtime-image": 2}[identity.kind],
        identity.sha256,
    )


def _sqlstate(error: DBAPIError) -> str | None:
    original = error.orig
    state = getattr(original, "sqlstate", None)
    if not isinstance(state, str):
        state = getattr(original, "pgcode", None)
    return state if isinstance(state, str) else None


def retryable_artifact_database_error(
    error: DBAPIError,
) -> ArtifactLifecycleError | None:
    """Translate only PostgreSQL lock/transaction contention failures."""

    state = _sqlstate(error)
    if state in _RETRYABLE_SQLSTATES:
        return ArtifactReferenceUnsettled(
            ArtifactLifecycleCode.REFERENCE_BUSY,
            f"artifact reference transaction conflicted with PostgreSQL ({state}); retry the operation",
            retryable=True,
        )
    if state == "57014":
        diagnostic = getattr(error.orig, "diag", None)
        primary = getattr(diagnostic, "message_primary", None)
        if isinstance(primary, str) and "statement timeout" in primary.lower():
            return ArtifactReferenceUnsettled(
                ArtifactLifecycleCode.REFERENCE_TIMEOUT,
                "artifact reference SQL exceeded the caller's statement time budget; retry the operation",
                retryable=True,
            )
    return None


def _reference_sql[Result](query: Callable[[], Result]) -> Result:
    """Execute one gate statement, preserving non-contention DBAPI failures."""

    try:
        return query()
    except DBAPIError as error:
        translated = retryable_artifact_database_error(error)
        if translated is None:
            raise
        raise translated from error


class ArtifactLifecycleError(RuntimeError):
    """A gate is busy, fenced for deletion, or cannot be safely inspected."""

    def __init__(self, code: str, detail: str, *, retryable: bool = False) -> None:
        self.code = code
        self.detail = detail[:512]
        self.retryable = retryable
        super().__init__(self.detail)


class ArtifactReferenceUnsettled(UnknownOutcomeError, ArtifactLifecycleError):
    """A gate is busy, reserved for removal, or cannot be read right now: the
    owner observes and retries, nothing is refused."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        retryable: bool = False,
        reason: WaitReason = WaitReason.OBSERVATION_UNAVAILABLE,
    ) -> None:
        ArtifactLifecycleError.__init__(self, code, detail, retryable=retryable)
        self.typed_reason = reason


class ArtifactReferenceUnverified(UnknownOutcomeError, ArtifactLifecycleError):
    """An incomplete local reference observation: retain bytes and re-observe.

    Accepted removal owners retry through their bounded lifecycle. An unknown
    inventory never authorizes unlinking and never implies no references.
    """

    def __init__(
        self,
        code: ArtifactLifecycleCode,
        detail: str,
        *,
        retryable: bool = True,
        reason: WaitReason = WaitReason.OBSERVATION_UNAVAILABLE,
    ) -> None:
        ArtifactLifecycleError.__init__(self, code, detail, retryable=retryable)
        self.typed_reason = reason


class ArtifactReferenceIdentityStale(UnknownOutcomeError, ArtifactLifecycleError):
    """Stored membership differs from accepted content; re-observe without effects."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        retryable: bool = True,
        reason: WaitReason = WaitReason.SCOPE_CHANGED,
    ) -> None:
        ArtifactLifecycleError.__init__(self, code, detail, retryable=retryable)
        self.typed_reason = reason


class ArtifactRemovalFenceLost(SecurityRefusalError, ArtifactLifecycleError):
    """The removal fence is no longer this owner's: it must not release it."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        retryable: bool = False,
        reason: SecurityRefusalReason = SecurityRefusalReason.STALE_FENCE,
    ) -> None:
        ArtifactLifecycleError.__init__(self, code, detail, retryable=retryable)
        self.typed_reason = reason


def lock_reference_gates(
    session: Session,
    identities: Iterable[ArtifactIdentity],
    *,
    now: datetime,
) -> tuple[ArtifactLifecycleGate, ...]:
    """Serialize one short SQL reference transaction by sorted exact identity.

    PostgreSQL's nonblocking transaction-scoped advisory locks cover first-row
    creation as well as existing gate rows. They end with the caller's
    transaction and are never held across storage inspection or transfer.
    """

    ordered = tuple(sorted(set(identities), key=_identity_order))
    if not ordered:
        return ()
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        for identity in ordered:
            key_bytes = hashlib.sha256(
                f"{identity.kind}:{identity.sha256}".encode("ascii")
            ).digest()[:8]
            key = int.from_bytes(key_bytes, "big", signed=True)
            acquired = _reference_sql(
                lambda key=key: session.scalar(
                    select(func.pg_try_advisory_xact_lock(key))
                )
            )
            if acquired is not True:
                raise ArtifactReferenceUnsettled(
                    ArtifactLifecycleCode.REFERENCE_BUSY,
                    "artifact reference ownership is changing; retry the operation",
                    retryable=True,
                )

    rows: list[ArtifactLifecycleGate] = []
    for identity in ordered:
        row = _reference_sql(
            lambda identity=identity: session.get(
                ArtifactLifecycleGate,
                {
                    "artifact_kind": identity.kind,
                    "artifact_sha256": identity.sha256,
                },
                populate_existing=True,
            )
        )
        if row is None:
            row = ArtifactLifecycleGate(
                artifact_kind=identity.kind,
                artifact_sha256=identity.sha256,
                removal_owner_kind=None,
                removal_owner_id=None,
                removal_fence=None,
                updated_at=now,
            )
            session.add(row)
            _reference_sql(session.flush)
        locked = _reference_sql(
            lambda identity=identity: session.scalar(
                select(ArtifactLifecycleGate)
                .where(
                    ArtifactLifecycleGate.artifact_kind == identity.kind,
                    ArtifactLifecycleGate.artifact_sha256 == identity.sha256,
                )
                .execution_options(populate_existing=True)
                .with_for_update(nowait=True)
            )
        )
        if locked is None:
            raise ArtifactReferenceUnsettled(
                ArtifactLifecycleCode.REFERENCE_UNAVAILABLE,
                "artifact reference ownership could not be read safely",
                retryable=True,
            )
        rows.append(locked)
    return tuple(rows)


def has_pending_removal(
    session: Session, identities: Iterable[ArtifactIdentity]
) -> bool:
    """Observe a wait dependency without acquiring an effect authority.

    Claim loops already own their operation rows. This read takes no gate lock;
    effect publication independently revalidates the exact storage gate.
    """
    wanted = tuple(identities)
    if not wanted:
        return False
    return (
        session.scalar(
            select(ArtifactLifecycleGate.removal_owner_id)
            .where(
                or_(
                    *(
                        and_(
                            ArtifactLifecycleGate.artifact_kind == item.kind,
                            ArtifactLifecycleGate.artifact_sha256 == item.sha256,
                        )
                        for item in wanted
                    )
                ),
                ArtifactLifecycleGate.removal_owner_id.is_not(None),
            )
            .limit(1)
        )
        is not None
    )


def require_reference_open(
    session: Session,
    identities: Iterable[ArtifactIdentity],
    *,
    now: datetime,
) -> None:
    """Take short reference gates and refuse identities reserved for deletion."""

    for row in lock_reference_gates(session, identities, now=now):
        if row.removal_owner_id is not None:
            raise ArtifactReferenceUnsettled(
                ArtifactLifecycleCode.DELETION_IN_PROGRESS,
                f"{row.artifact_kind} {row.artifact_sha256} is reserved for removal",
                retryable=True,
            )


def lock_removal_fences(
    session: Session,
    identities: Iterable[ArtifactIdentity],
    *,
    owner_kind: RemovalOwnerKind,
    owner_id: str,
    fence: str,
    now: datetime,
) -> bool:
    """Lock an exact deletion scope in common order and verify every fence."""

    rows = lock_reference_gates(session, identities, now=now)
    return bool(rows) and all(
        row.removal_owner_kind == owner_kind
        and row.removal_owner_id == owner_id
        and row.removal_fence == fence
        for row in rows
    )


def removal_fences_match(
    session: Session,
    assignments: Iterable[tuple[ArtifactIdentity, RemovalOwnerKind, str, str]],
    *,
    now: datetime,
) -> bool:
    """Verify a heterogeneous parent/child fence assignment in common order."""

    expected = {
        identity: (kind, owner_id, fence)
        for identity, kind, owner_id, fence in assignments
    }
    if not expected:
        return False
    rows = lock_reference_gates(session, expected, now=now)
    return all(
        (
            row.removal_owner_kind,
            row.removal_owner_id,
            row.removal_fence,
        )
        == expected[
            ArtifactIdentity(cast(ArtifactKind, row.artifact_kind), row.artifact_sha256)
        ]
        for row in rows
    )


def reserve_removal(
    session: Session,
    identities: Iterable[ArtifactIdentity],
    *,
    owner_kind: RemovalOwnerKind,
    owner_id: str,
    fence: str,
    now: datetime,
) -> None:
    """Persist one removal owner's exclusive identity fences atomically."""
    reserve_removal_owners(
        session,
        ((identity, owner_kind, owner_id, fence) for identity in identities),
        now=now,
    )


def reserve_removal_owners(
    session: Session,
    assignments: Iterable[tuple[ArtifactIdentity, RemovalOwnerKind, str, str]],
    *,
    now: datetime,
) -> None:
    """Reserve related removals in one globally ordered gate acquisition.

    A recipe removal may own image gates itself while its exact model-removal
    child owns model-set and model-object gates. Acquire the combined identity
    set once in the shared kind/digest order, then assign each already-locked
    row to its durable owner before either owner is committed.
    """

    by_identity: dict[ArtifactIdentity, tuple[RemovalOwnerKind, str, str]] = {}
    for identity, owner_kind, owner_id, fence in assignments:
        if owner_kind not in {"model-cache-operation", "recipe-image-job"}:
            raise InvalidValue(
                "artifact removal owner kind is invalid",
                reason=InvalidRequestReason.UNSUPPORTED,
            )
        if not owner_id or not fence:
            raise InvalidValue(
                "artifact removal owner identity is incomplete",
                reason=InvalidRequestReason.INCOMPLETE,
            )
        owner = (owner_kind, owner_id, fence)
        previous = by_identity.setdefault(identity, owner)
        if previous != owner:
            raise InvalidValue(
                "one artifact identity has multiple removal owners",
                reason=InvalidRequestReason.CONFLICT,
            )

    rows = lock_reference_gates(session, by_identity, now=now)
    for row in rows:
        identity = ArtifactIdentity(
            cast(ArtifactKind, row.artifact_kind), row.artifact_sha256
        )
        owner_kind, owner_id, fence = by_identity[identity]
        same_owner = (
            row.removal_owner_kind == owner_kind
            and row.removal_owner_id == owner_id
            and row.removal_fence == fence
        )
        if row.removal_owner_id is not None and not same_owner:
            raise ArtifactReferenceUnsettled(
                ArtifactLifecycleCode.DELETION_BUSY,
                f"{row.artifact_kind} {row.artifact_sha256} has another removal owner",
                retryable=True,
            )
        row.removal_owner_kind = owner_kind
        row.removal_owner_id = owner_id
        row.removal_fence = fence
        row.updated_at = now


def check_removal_fence_nowait(
    session: Session,
    identity: ArtifactIdentity,
    *,
    owner_kind: RemovalOwnerKind,
    owner_id: str,
    fence: str,
) -> bool:
    """Recheck the committed fence while one nonblocking object lock is held.

    This uses only a short ``FOR UPDATE NOWAIT`` read. It never waits on the
    transaction-scoped admission lock while a managed-storage lock is held.
    """

    row = _reference_sql(
        lambda: session.scalar(
            select(ArtifactLifecycleGate)
            .where(
                ArtifactLifecycleGate.artifact_kind == identity.kind,
                ArtifactLifecycleGate.artifact_sha256 == identity.sha256,
            )
            .execution_options(populate_existing=True)
            .with_for_update(nowait=True)
        )
    )
    return bool(
        row is not None
        and row.removal_owner_kind == owner_kind
        and row.removal_owner_id == owner_id
        and row.removal_fence == fence
    )


def reference_gate_is_open_nowait(session: Session, identity: ArtifactIdentity) -> bool:
    """Read one committed deletion gate without waiting under a file lock.

    Known PostgreSQL contention is surfaced as a retryable domain error. Other
    database errors propagate unchanged so callers cannot mistake a failed
    reference check for a closed or busy gate.
    """

    row = _reference_sql(
        lambda: session.scalar(
            select(ArtifactLifecycleGate)
            .where(
                ArtifactLifecycleGate.artifact_kind == identity.kind,
                ArtifactLifecycleGate.artifact_sha256 == identity.sha256,
            )
            .execution_options(populate_existing=True)
            .with_for_update(nowait=True)
        )
    )
    return row is None or row.removal_owner_id is None


def dead_removal_identities(
    session: Session,
    *,
    owner_kind: RemovalOwnerKind,
    limit: int,
    after: tuple[str, str] | None = None,
) -> tuple[ArtifactIdentity, ...]:
    """Bounded orphan inventory; no storage lock is acquired in this read."""
    model = ModelCacheOperation if owner_kind == "model-cache-operation" else Job
    terminal = (
        model_cache_states.words(
            LifecycleState.SUCCEEDED, LifecycleState.FAILED, LifecycleState.CANCELLED
        )
        if owner_kind == "model-cache-operation"
        else job_states.words(
            LifecycleState.SUCCEEDED,
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
            LifecycleState.SUPERSEDED,
        )
    )
    statement = (
        select(
            ArtifactLifecycleGate.artifact_kind, ArtifactLifecycleGate.artifact_sha256
        )
        .outerjoin(model, model.id == ArtifactLifecycleGate.removal_owner_id)
        .where(
            ArtifactLifecycleGate.removal_owner_kind == owner_kind,
            ArtifactLifecycleGate.removal_owner_id.is_not(None),
            or_(model.id.is_(None), model.state.in_(terminal)),
        )
        .order_by(
            ArtifactLifecycleGate.artifact_kind, ArtifactLifecycleGate.artifact_sha256
        )
        .limit(limit)
    )
    if after is not None:
        kind, digest = after
        statement = statement.where(
            or_(
                ArtifactLifecycleGate.artifact_kind > kind,
                and_(
                    ArtifactLifecycleGate.artifact_kind == kind,
                    ArtifactLifecycleGate.artifact_sha256 > digest,
                ),
            )
        )
    rows = session.execute(statement)
    return tuple(
        ArtifactIdentity(cast(ArtifactKind, kind), digest) for kind, digest in rows
    )


def supersede_removal_nowait(
    session: Session,
    identity: ArtifactIdentity,
    *,
    owner_kind: RemovalOwnerKind,
    request_id: str,
    validate: Callable[
        [ModelCacheOperation | Job, ModelCacheOperation | Job, str], bool
    ],
    cancel: Callable[[ModelCacheOperation | Job, str], None],
    now: datetime,
) -> bool:
    """Reconcile a persisted newer request under this identity's storage lock.

    The caller owns exactly one nonblocking storage lock outside this short SQL
    transaction. Other deletion gates stay fenced, including any effect already
    executing on another object; each is released under its own storage lock.
    """
    gate = _reference_sql(
        lambda: session.scalar(
            select(ArtifactLifecycleGate)
            .where(
                ArtifactLifecycleGate.artifact_kind == identity.kind,
                ArtifactLifecycleGate.artifact_sha256 == identity.sha256,
            )
            .execution_options(populate_existing=True)
            .with_for_update(nowait=True)
        )
    )
    if (
        gate is None
        or gate.removal_owner_kind != owner_kind
        or gate.removal_owner_id is None
        or not gate.removal_fence
        or gate.removal_owner_id == request_id
    ):
        return False
    model = ModelCacheOperation if owner_kind == "model-cache-operation" else Job
    locked: dict[str, ModelCacheOperation | Job] = {}
    for row_id in sorted((request_id, gate.removal_owner_id)):
        row = _reference_sql(
            lambda row_id=row_id: session.scalar(
                select(model)
                .where(model.id == row_id)
                .execution_options(populate_existing=True)
                .with_for_update(nowait=True)
            )
        )
        if row is None:
            return False
        locked[row_id] = row
    requester, remover = locked[request_id], locked[gate.removal_owner_id]
    live = (
        model_cache_states.LIVE
        if model is ModelCacheOperation
        else job_states.words(
            LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.BACKOFF
        )
    )
    if requester.state not in live or remover.state not in live:
        return False
    requested_at = requester.created_at
    removed_at = remover.created_at
    requested_at = (
        requested_at if requested_at.tzinfo else requested_at.replace(tzinfo=UTC)
    )
    removed_at = removed_at if removed_at.tzinfo else removed_at.replace(tzinfo=UTC)
    if requested_at < removed_at or not validate(
        requester, remover, gate.removal_fence
    ):
        return False
    cancel(remover, requester.id)
    return release_dead_removal_nowait(
        session, identity, owner_kind=owner_kind, now=now
    )


def release_dead_removal_nowait(
    session: Session,
    identity: ArtifactIdentity,
    *,
    owner_kind: RemovalOwnerKind,
    now: datetime,
) -> bool:
    """Fence a dead remover while holding its exact nonblocking storage lock.

    The caller first releases its inventory transaction, then obtains the same
    object lock as the deletion executor. Thus an executor that already checked
    its fence has finished its filesystem step; a delayed executor will observe
    the cleared fence before deleting. Lease expiry alone is never proof of this.
    No availability is invented: normal admission still observes managed bytes.
    """
    row = _reference_sql(
        lambda: session.scalar(
            select(ArtifactLifecycleGate)
            .where(
                ArtifactLifecycleGate.artifact_kind == identity.kind,
                ArtifactLifecycleGate.artifact_sha256 == identity.sha256,
            )
            .execution_options(populate_existing=True)
            .with_for_update(nowait=True)
        )
    )
    if (
        row is None
        or row.removal_owner_kind != owner_kind
        or row.removal_owner_id is None
    ):
        return False
    model = ModelCacheOperation if owner_kind == "model-cache-operation" else Job
    owner = _reference_sql(
        lambda: session.scalar(
            select(model)
            .where(model.id == row.removal_owner_id)
            .execution_options(populate_existing=True)
            .with_for_update(nowait=True)
        )
    )
    if owner is not None:
        terminal = (
            model_cache_states.words(
                LifecycleState.SUCCEEDED,
                LifecycleState.FAILED,
                LifecycleState.CANCELLED,
            )
            if owner_kind == "model-cache-operation"
            else job_states.words(
                LifecycleState.SUCCEEDED,
                LifecycleState.FAILED,
                LifecycleState.CANCELLED,
                LifecycleState.SUPERSEDED,
            )
        )
        if owner.state not in terminal:
            return False
    row.removal_owner_kind = None
    row.removal_owner_id = None
    row.removal_fence = None
    row.updated_at = now
    return True


def clear_removal(
    session: Session,
    identities: Iterable[ArtifactIdentity],
    *,
    owner_kind: RemovalOwnerKind,
    owner_id: str,
    fence: str,
    now: datetime,
) -> None:
    """Release deletion gates only after the owner's storage effects are done."""

    for row in lock_reference_gates(session, identities, now=now):
        if (
            row.removal_owner_kind != owner_kind
            or row.removal_owner_id != owner_id
            or row.removal_fence != fence
        ):
            raise ArtifactRemovalFenceLost(
                ArtifactLifecycleCode.DELETION_FENCE_LOST,
                "artifact removal fence changed before it could be released",
            )
        row.removal_owner_kind = None
        row.removal_owner_id = None
        row.removal_fence = None
        row.updated_at = now


__all__ = [
    "ArtifactIdentity",
    "ArtifactKind",
    "ArtifactLifecycleError",
    "ArtifactReferenceIdentityStale",
    "ArtifactReferenceUnsettled",
    "ArtifactReferenceUnverified",
    "ArtifactRemovalFenceLost",
    "RemovalOwnerKind",
    "check_removal_fence_nowait",
    "clear_removal",
    "dead_removal_identities",
    "has_pending_removal",
    "lock_reference_gates",
    "reference_gate_is_open_nowait",
    "release_dead_removal_nowait",
    "removal_fences_match",
    "require_reference_open",
    "reserve_removal",
    "reserve_removal_owners",
    "supersede_removal_nowait",
]
