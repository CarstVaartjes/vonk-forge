"""Deleting a saved Fleet profile; running workloads are never touched."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import cast as _typing_cast
from uuid import NAMESPACE_OID, uuid5

from sqlalchemy import delete, select, update

from vonk_agent_protocol.lifecycle_vocabulary import InvalidRequestReason
from vonk_control.admission_locking import AdmissionRowLock, lock_admission_rows
from vonk_control.categorized_errors import MissingRecord
from vonk_control.fleet_profile_contract.definitions import FleetProfileDeletion
from vonk_control.fleet_profile_contract.vocabulary import FLEET_PROFILE_ENDED_STATES
from vonk_control.fleet_profiles import states as fleet_profile_states
from vonk_control.fleet_profiles.contracts import (
    FleetProfileInvalid,
    FleetProfileRevisionConflict,
)
from vonk_control.models.fleet import (
    FleetProfile,
    FleetProfileApplication,
    FleetProfileRecipe,
    FleetProfileSelection,
)

if TYPE_CHECKING:
    from vonk_control.fleet_profiles.service import (
        FleetProfileService as _FleetProfileService,
    )


def _stale(expected: int | None, current: int) -> None:
    if expected is not None and expected != current:
        raise FleetProfileRevisionConflict(
            f"profile revision conflict: expected {expected}, current {current}",
            reason=InvalidRequestReason.CONFLICT,
        )


class FleetProfileService:
    def delete_number(
        self, number: int, *, actor: str, expected_revision: int | None = None
    ) -> FleetProfileDeletion:
        """Remove the saved profile and its application history.

        Applications still loading are cancelled through the one cancel path.
        Their stop is driven by the worker, so while any application is not yet
        ended the answer is ``cancelling`` and nothing is removed; the same
        request sent again finishes the delete. Nothing running is stopped or
        changed by the delete itself, and the loaded-profile pointer clears (the selection row and its
        generation stay).
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        with self._sessions() as session:
            row = session.scalar(
                select(FleetProfile).where(FleetProfile.number == number)
            )
            if row is None:
                return FleetProfileDeletion(number=number, outcome="already-gone")
            _stale(expected_revision, row.revision)
            profile_id = row.id
            loading = tuple(
                session.scalars(
                    select(FleetProfileApplication.id).where(
                        FleetProfileApplication.profile_id == profile_id,
                        FleetProfileApplication.state.in_(fleet_profile_states.LIVE),
                    )
                )
            )
        for application_id in loading:
            try:
                self.cancel(
                    application_id,
                    profile_number=number,
                    # One key per application: a repeated delete replays it.
                    request_key=str(
                        uuid5(NAMESPACE_OID, f"profile-delete:{application_id}")
                    ),
                    actor=actor,
                )
            except FleetProfileInvalid, MissingRecord:
                # Already ended, already cancelling, or gone: nothing to cancel.
                continue

        # The one profile lock a load also takes closes the load-vs-delete
        # race; contention is the existing busy answer, retried by the caller.
        with self._admission_session(actor) as session:
            found = lock_admission_rows(
                session,
                (
                    AdmissionRowLock(
                        "deleted-profile",
                        FleetProfile,
                        select(FleetProfile).where(FleetProfile.id == profile_id),
                    ),
                ),
            )["deleted-profile"]
            if not found:
                return FleetProfileDeletion(number=number, outcome="already-gone")
            row = found[0]
            _stale(expected_revision, row.revision)
            unfinished = session.scalar(
                select(FleetProfileApplication.id)
                .where(
                    FleetProfileApplication.profile_id == profile_id,
                    FleetProfileApplication.state.not_in(FLEET_PROFILE_ENDED_STATES),
                )
                .limit(1)
            )
            if unfinished is not None:
                return FleetProfileDeletion(number=number, outcome="cancelling")
            # The selection row owns the generation Sparks fence clock and swap
            # on: it is kept, its pointer clears and the generation advances so
            # Sparks adopt the default policy. One conditional statement: a
            # selection that already moved to another profile is left alone.
            session.execute(
                update(FleetProfileSelection)
                .where(FleetProfileSelection.profile_id == profile_id)
                .values(
                    profile_id=None,
                    application_id=None,
                    generation=FleetProfileSelection.generation + 1,
                    wait_evidence=None,  # what clear_wait sets
                    next_action_at=None,
                )
            )
            # Explicit, in order: no database cascade is relied on.
            session.execute(
                delete(FleetProfileApplication).where(
                    FleetProfileApplication.profile_id == profile_id
                )
            )
            session.execute(
                delete(FleetProfileRecipe).where(
                    FleetProfileRecipe.profile_id == profile_id
                )
            )
            session.execute(delete(FleetProfile).where(FleetProfile.id == profile_id))
        return FleetProfileDeletion(number=number, outcome="deleted")
