"""Liveness properties of the world, checked after every round.

They are judged from what the system shows, not from its internals: the stored
state of every accepted profile load, every order, run, installation and
claim, and what the worker logs when an owner raised. Waiting is legitimate
while a fault is active (a Spark down, late or short of disk), so the stall
and refusal-loop clocks only run while every Spark is healthy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from vonk_agent_protocol import (
    InvalidRequestError,
    SecurityRefusalError,
    UnknownOutcomeError,
)
from vonk_control.fleet_profiles import FleetProfileConflict
from vonk_control.models import (
    AgentOperation,
    FleetProfileApplication,
    Job,
    RecipeBuild,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
)
from vonk_control.reservation_owners import OWNER_SETTLE_GRACE

from .fleet_match import fleet_mismatch
from .sparks import Sparks
from .world import World

#: A request may wait this long, with every Spark healthy and nothing in the
#: world changing, before it is stuck. Longer than any retry backoff.
STALL_WINDOW = timedelta(hours=2)
#: The same refusal this many times in a row, with nothing else changing, is a loop.
REFUSAL_REPEATS = 6
#: A claim outlives its owner when it is still held this long after the owner ended.
CLAIM_GRACE = OWNER_SETTLE_GRACE + timedelta(minutes=2)
ENDED = frozenset({"succeeded", "failed", "cancelled", "superseded"})
_DEAD_OWNER_STATES = {
    "fleet-profile": ENDED,
    "run": frozenset({"stopped"}),
    "installation": frozenset({"uninstalled"}),
    "recipe-build": frozenset({"failed", "cancelled"}),
}
_OWNERS = {
    "fleet-profile": FleetProfileApplication,
    "run": RecipeRun,
    "installation": RecipeInstallation,
    "recipe-build": RecipeBuild,
}
_NUMBERS = re.compile(r"\d+")
_IDENTITIES = re.compile(r"\b(?:[0-9a-f]{8}-[0-9a-f-]{27}|spk_[0-9a-f]{32})\b")


@dataclass(frozen=True)
class Violation:
    invariant: str
    detail: str

    @property
    def signature(self) -> str:
        """The violation without its identities and times, to group seeds by bug."""

        detail = _IDENTITIES.sub("<id>", self.detail)
        return f"{self.invariant}: {_NUMBERS.sub('#', detail)}"


@dataclass(frozen=True)
class _Request:
    state: str
    reason: str
    bookkeeping: str
    #: When the load was accepted: any load accepted at or after it is newer intent.
    accepted_at: datetime


@dataclass(frozen=True)
class _Snapshot:
    requests: dict[str, _Request]
    core: frozenset[tuple[str, ...]]
    dead_claims: frozenset[str]


def typed_refusal(error: BaseException) -> bool:
    """Whether an operator request was refused in the contract's vocabulary."""

    return isinstance(
        error,
        (
            InvalidRequestError,
            UnknownOutcomeError,
            SecurityRefusalError,
            FleetProfileConflict,
        ),
    )


def _snapshot(world: World) -> _Snapshot:
    requests: dict[str, _Request] = {}
    core: set[tuple[str, ...]] = set()
    with world.sessions() as session:
        for row in session.scalars(
            select(FleetProfileApplication).order_by(
                FleetProfileApplication.created_at, FleetProfileApplication.request_key
            )
        ):
            progress = row.progress if isinstance(row.progress, dict) else {}
            attempt = (
                progress.get("attempt"),
                progress.get("admission_attempt"),
                row.next_action_at,
            )
            requests[row.id] = _Request(
                row.state,
                _NUMBERS.sub("#", row.status_reason or ""),
                f"{row.status_reason}|{attempt}",
                row.created_at,
            )
            core.add(
                (
                    "application",
                    row.id,
                    row.state,
                    str(row.current_step),
                    str(row.current_operation_id),
                )
            )
        for job in session.scalars(select(Job).order_by(Job.created_at, Job.id)):
            result = job.result if isinstance(job.result, dict) else {}
            phase = f"{result.get('phase')}/{result.get('subphase')}"
            core.add(("job", job.id, job.state, phase))
        for operation in session.scalars(
            select(AgentOperation).order_by(
                AgentOperation.created_at, AgentOperation.id
            )
        ):
            core.add(("order", operation.id, operation.state))
        for run in session.scalars(
            select(RecipeRun).order_by(RecipeRun.created_at, RecipeRun.id)
        ):
            core.add(("run", run.id, run.state, str(run.route_state)))
        for installation in session.scalars(
            select(RecipeInstallation).order_by(
                RecipeInstallation.created_at, RecipeInstallation.id
            )
        ):
            core.add(("installation", installation.id, installation.state))
        dead: set[str] = set()
        for claim in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.state.in_(("active", "promised"))
            )
        ):
            core.add(("claim", claim.id, claim.state))
            model = _OWNERS.get(claim.owner_kind)
            if model is None:
                continue
            owner = session.get(model, claim.owner_id)
            if owner is None or owner.state in _DEAD_OWNER_STATES[claim.owner_kind]:
                state = "missing" if owner is None else owner.state
                dead.add(
                    f"{claim.kind} claim {claim.id} on {claim.node_id} held by"
                    f" {state} {claim.owner_kind} {claim.owner_id}"
                )
    return _Snapshot(requests, frozenset(core), frozenset(dead))


@dataclass
class Monitor:
    world: World
    sparks: Sparks
    _last: _Snapshot | None = None
    _changed_at: datetime | None = None
    _healthy_since: datetime | None = None
    #: Loads an operator asked to cancel.
    cancel_requested: set[str] = field(default_factory=set)
    _repeats: dict[str, int] = field(default_factory=dict)
    _dead_since: dict[str, datetime] = field(default_factory=dict)

    def snapshot(self) -> _Snapshot:
        return _snapshot(self.world)

    def unfinished(self) -> dict[str, str]:
        assert self._last is not None
        return {
            request_id: f"{request.state}: {request.reason}"
            for request_id, request in self._last.requests.items()
            if request.state not in ENDED
        }

    def stable(self) -> bool:
        """Nothing changed in the last round."""

        return self._changed_at is not None and self._changed_at < self.world.clock()

    def observe(self) -> list[Violation]:
        now = self.world.clock()
        current = self.snapshot()
        previous, self._last = self._last, current
        violations = [
            Violation("no worker tick raises", raised)
            for raised in self.world.raised_ticks.raised
        ]
        self.world.raised_ticks.raised.clear()
        core_changed = previous is None or previous.core != current.core
        if core_changed:
            self._changed_at = now
        healthy = self.sparks.healthy()
        if not healthy:
            self._healthy_since = None
            self._repeats.clear()
        elif self._healthy_since is None:
            self._healthy_since = now
        violations += self._loops(previous, current, core_changed, healthy)
        violations += self._stalls(current, now, healthy)
        violations += self._claims(current, now)
        violations += self._unrequested_endings(previous, current)
        violations += self._succeeded_loads(previous, current)
        return violations

    def _loops(
        self,
        previous: _Snapshot | None,
        current: _Snapshot,
        core_changed: bool,
        healthy: bool,
    ) -> list[Violation]:
        violations: list[Violation] = []
        for request_id, request in current.requests.items():
            before = previous.requests.get(request_id) if previous else None
            if (
                not healthy
                or core_changed
                or before is None
                or request.state in ENDED
                or before.reason != request.reason
            ):
                self._repeats.pop(request_id, None)
                continue
            if before.bookkeeping == request.bookkeeping:
                continue
            repeats = self._repeats.get(request_id, 0) + 1
            self._repeats[request_id] = repeats
            if repeats > REFUSAL_REPEATS:
                violations.append(
                    Violation(
                        "no refusal repeats without a change elsewhere",
                        f"load {request_id} {request.state} {repeats} times:"
                        f" {request.reason}",
                    )
                )
        return violations

    def _stalls(
        self, current: _Snapshot, now: datetime, healthy: bool
    ) -> list[Violation]:
        if not healthy or self._changed_at is None or self._healthy_since is None:
            return []
        quiet_since = max(self._changed_at, self._healthy_since)
        if now - quiet_since < STALL_WINDOW:
            return []
        return [
            Violation(
                "every request ends or progresses",
                f"load {request_id} stuck {request.state} since {quiet_since}:"
                f" {request.reason}",
            )
            for request_id, request in current.requests.items()
            if request.state not in ENDED
        ]

    def _unrequested_endings(
        self, previous: _Snapshot | None, current: _Snapshot
    ) -> list[Violation]:
        """A load ends ``cancelled`` only when someone asked: an operator's
        cancel, or newer intent (a load accepted at or after it). It ends
        ``superseded`` only by newer intent."""

        violations: list[Violation] = []
        for request_id, request in current.requests.items():
            before = previous.requests.get(request_id) if previous else None
            if request.state not in {"cancelled", "superseded"} or (
                before is not None and before.state == request.state
            ):
                continue
            newer = any(
                other_id != request_id and other.accepted_at >= request.accepted_at
                for other_id, other in current.requests.items()
            )
            if newer:
                continue
            if request.state == "superseded":
                violations.append(
                    Violation(
                        "no load ends superseded without a newer load",
                        f"load {request_id} superseded: {request.reason}",
                    )
                )
            elif request_id not in self.cancel_requested:
                violations.append(
                    Violation(
                        "no load ends cancelled without a cancel request",
                        f"load {request_id} cancelled: {request.reason}",
                    )
                )
        return violations

    def _succeeded_loads(
        self, previous: _Snapshot | None, current: _Snapshot
    ) -> list[Violation]:
        """A load that ends ``succeeded`` as the selected profile is what the
        fleet runs (``fleet_match``)."""

        violations: list[Violation] = []
        for request_id, request in current.requests.items():
            before = previous.requests.get(request_id) if previous else None
            if request.state != "succeeded" or (
                before is not None and before.state == "succeeded"
            ):
                continue
            mismatch = fleet_mismatch(self.world, request_id)
            if mismatch is not None:
                violations.append(
                    Violation(
                        "a succeeded profile load matches the fleet",
                        f"load {request_id} succeeded while {mismatch}",
                    )
                )
        return violations

    def _claims(self, current: _Snapshot, now: datetime) -> list[Violation]:
        self._dead_since = {
            claim: self._dead_since.get(claim, now) for claim in current.dead_claims
        }
        return [
            Violation("no claim outlives its owner", claim)
            for claim, since in sorted(self._dead_since.items())
            if now - since > CLAIM_GRACE
        ]
