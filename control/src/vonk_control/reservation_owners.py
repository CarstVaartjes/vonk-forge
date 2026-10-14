"""Who owns a resource reservation, and whether that owner can still use it.

A reservation (disk, port, memory) is a promise that some operation is about to
write, bind or allocate. It is worth holding only while its owner can still do
that. Whether it can is ``owner_liveness.owner()``, the one predicate for every
claim; this module names a reservation's owner for explanations and releases
the reservations whose owner ended, so an explanation and a release never
disagree. The liveness names are re-exported for existing callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import ReservationState

from .models import ResourceReservation
from .owner_liveness import (
    ADOPTION_WINDOW,
    OWNER_SETTLE_GRACE,
    SPARK_PRESENCE_WINDOW,
    Owner,
    OwnerKind,
    OwnerRef,
    owner,
    run_has_live_operation,
)

__all__ = [
    "ADOPTION_WINDOW",
    "OWNER_SETTLE_GRACE",
    "SPARK_PRESENCE_WINDOW",
    "Owner",
    "OwnerKind",
    "OwnerRef",
    "ReservationOwner",
    "owner",
    "release_dead_owner_reservations",
    "reservation_owner",
    "run_has_live_operation",
]

_NOUNS = {
    OwnerKind.FLEET_PROFILE: "profile application",
    OwnerKind.RUN: "run",
    OwnerKind.INSTALLATION: "installation",
    OwnerKind.RECIPE_BUILD: "recipe build",
}
# Their arm's reason says more than the state does.
_DETAILED = frozenset({OwnerKind.INSTALLATION, OwnerKind.RECIPE_BUILD})


@dataclass(frozen=True, slots=True)
class ReservationOwner:
    kind: str
    owner_id: str
    state: str
    dead: bool
    noun: str
    detail: str = ""

    def describe(self) -> str:
        """``cancelled profile application 3f68e2f4``; a gone owner is ``missing``."""

        text = f"{self.state} {self.noun} {self.owner_id[:8]}"
        return f"{text} ({self.detail})" if self.detail else text


def reservation_owner(
    session: Session,
    owner_kind: str,
    owner_id: str,
    *,
    now: datetime | None = None,
) -> ReservationOwner:
    """Classify one owner. Anything uncertain is live: a release needs proof.

    Without ``now`` the settling grace and adoption window are not applied, which
    is how an explanation names an owner; the reconciler always passes ``now``.
    """

    kind = next((kind for kind in _NOUNS if kind.value == owner_kind), None)
    if kind is None:
        return ReservationOwner(owner_kind, owner_id, "unrecognised", False, "owner")
    found = owner(session, OwnerRef(kind, owner_id), now)
    return ReservationOwner(
        owner_kind,
        owner_id,
        found.state,
        not found.live,
        _NOUNS[kind],
        found.why if kind in _DETAILED and found.state != "missing" else "",
    )


def release_dead_owner_reservations(
    session: Session, now: datetime
) -> tuple[ReservationOwner, ...]:
    """Release every active or promised claim whose owner is dead.

    Candidates are found without locks. Only a candidate's rows are locked, and
    a row another transaction holds (an admission handing it off, a profile
    re-reserving it) is skipped until the next tick instead of waited for, so
    this never blocks or fails the writer it races. The owner is classified
    again once its rows are held. Releasing an already released claim is a
    no-op, and an owner that resumes (a profile retry, an install retry)
    takes its claim back.
    """

    released: list[ReservationOwner] = []
    owners = session.execute(
        select(ResourceReservation.owner_kind, ResourceReservation.owner_id)
        .where(
            ResourceReservation.state.in_(
                (ReservationState.ACTIVE, ReservationState.PROMISED)
            )
        )
        .distinct()
    ).all()
    for owner_kind, owner_id in sorted(owners):
        if not reservation_owner(session, owner_kind, owner_id, now=now).dead:
            continue
        claims = session.scalars(
            select(ResourceReservation)
            .where(
                ResourceReservation.owner_kind == owner_kind,
                ResourceReservation.owner_id == owner_id,
                ResourceReservation.state.in_(
                    (ReservationState.ACTIVE, ReservationState.PROMISED)
                ),
            )
            .order_by(ResourceReservation.id)
            .with_for_update(skip_locked=True)
        ).all()
        if not claims:
            continue
        ended = reservation_owner(session, owner_kind, owner_id, now=now)
        if not ended.dead:
            continue
        for claim in claims:
            claim.state = ReservationState.RELEASED
            claim.released_at = now
        released.append(ended)
    return tuple(released)
