"""Selection for Fleet profiles."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ..admission_locking import (
    AdmissionLockBusy,
    ConstraintDefect,
    ConstraintReplan,
    ConstraintReplay,
    acquire_admission_keys,
    admitting,
    busy_detail,
    is_admission_contention,
    node_admission_key,
)
from ..auth import MUTATION_ROLES, Actor
from ..lifecycle.evidence import Damaged, Residue, read_or_rebuild
from ..models.fleet import FleetProfileApplication, FleetProfileSelection, User
from ..persisted_waits import read_wait
from .capacity import release_unassigned_profile_claims
from .contracts import (
    FleetProfileAdmissionBusy,
    FleetProfileAdmissionDefect,
    FleetProfileAdmissionEffectBusy,
    FleetProfileConflict,
    FleetProfilePermissionDenied,
    FleetProfilePortPromiseConflict,
    _SelectedProfileSnapshot,
)
from .persistence import (
    _intended_profile,
    _persisted_profile_plan,
)
from .projection_support import (
    _aware,
    _roster_digest,
)

if TYPE_CHECKING:
    from .service import FleetProfileService
    from .service import FleetProfileService as _FleetProfileService


def _admission_wait(error: BaseException) -> str:
    """A busy admission's reason: which lock refused it and who held the way."""

    return (
        f"Profile admission is busy ({busy_detail(error)}); the Controller will "
        "retry automatically after the current fleet, catalog, workload or "
        "capacity change completes."
    )


class FleetProfileService:
    def _selected_profile_snapshot(
        self, session: Session
    ) -> _SelectedProfileSnapshot | Residue | None:
        """The selected profile's application and plan, ``None`` when nothing is
        selected, or a :class:`Residue` when the selection's receipt is damaged
        (recorded as unknown; the application worker fails that receipt)."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        selection = session.get(FleetProfileSelection, 1)
        if selection is None:
            return None
        selected_id = selection.application_id
        selected_profile_id = selection.profile_id
        if selected_id is None or selected_profile_id is None:
            return None  # the loaded profile was deleted; only the counter stays

        def read() -> _SelectedProfileSnapshot | Damaged:
            application = session.get(FleetProfileApplication, selected_id)
            if application is None or application.profile_id != selected_profile_id:
                return Damaged("selected profile application is unavailable")
            if application.selection_generation != selection.generation:
                return Damaged("selected profile generation is inconsistent")
            try:
                intended = _intended_profile(application, session=session)
            except FleetProfileConflict as error:
                # The reviewed intent's integrity check refused it.
                return Damaged(str(error))
            plan = _persisted_profile_plan(application)
            if isinstance(intended, Residue) or isinstance(plan, Residue):
                return Damaged("selected profile evidence is unavailable")
            if (
                application.profile_digest != intended.profile_digest
                or tuple(intended.scope.node_ids) != tuple(plan.scope.node_ids)
                or selection.roster_digest != _roster_digest(intended.scope.node_ids)
                or plan.profile_revision != selection.profile_revision
            ):
                return Damaged("selected profile snapshot is inconsistent")
            return _SelectedProfileSnapshot(
                wait=read_wait(selection),
                generation=selection.generation,
                profile_id=selected_profile_id,
                profile_revision=selection.profile_revision,
                application_id=selected_id,
                roster_node_ids=tuple(intended.scope.node_ids),
                roster_digest=selection.roster_digest,
                actor=application.actor,
                intended=intended,
                plan=plan,
            )

        return read_or_rebuild(
            kind="profile-selection",
            subject=selected_id,
            read=read,
        )

    @staticmethod
    def _authorize(
        session: Session,
        actor: str,
        *,
        mutation: bool = True,
    ) -> None:

        user = session.scalar(select(User).where(User.subject == actor))
        if not _user_has_profile_authority(user, mutation=mutation):
            raise FleetProfilePermissionDenied(
                "Current profile authority is unavailable"
            )

    def _release_claims(
        self, session: Session, application: FleetProfileApplication
    ) -> None:
        """After every change of an application's state: release only the claims no
        assignment owns, in the same transaction."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        release_unassigned_profile_claims(
            session, application, now=_aware(self._clock())
        )

    @contextmanager
    def _admission_session(
        self,
        actor: str,
        *,
        node_ids: Sequence[str] = (),
        platform_maintenance: bool = False,
    ) -> Iterator[Session]:
        """Open the short transaction used to accept a reviewed profile plan.

        Node advisory locks and the explicit row locks below serialize the
        effects this transaction owns. A whole-table PostgreSQL fence would
        make routine heartbeat and telemetry writes unrelated fleet blockers;
        exact plan, roster, catalog and capacity checks remain authoritative.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        try:
            with admitting(), self._sessions.begin() as session:
                if not platform_maintenance:
                    self._authorize(session, actor)
                try:
                    acquire_admission_keys(
                        session,
                        tuple(node_admission_key(node_id) for node_id in node_ids),
                        holder="profile-admission",
                    )
                except AdmissionLockBusy as error:
                    raise FleetProfileAdmissionBusy(
                        _admission_wait(error), holder=error.holder
                    ) from error
                except OperationalError as error:
                    if is_admission_contention(error):
                        raise FleetProfileAdmissionBusy(
                            _admission_wait(error)
                        ) from None
                    raise
                try:
                    yield session
                except AdmissionLockBusy as error:
                    raise FleetProfileAdmissionEffectBusy(
                        _admission_wait(error)
                    ) from error
                except OperationalError as error:
                    if is_admission_contention(error):
                        raise FleetProfileAdmissionEffectBusy(
                            _admission_wait(error)
                        ) from None
                    raise
        except ConstraintReplan as error:
            # A port promise (or another plan assumption) was taken, met by the
            # plan's own check or by the database: plan again with it taken.
            raise FleetProfilePortPromiseConflict(str(error)) from error
        except (ConstraintReplay, ConstraintDefect) as error:
            raise FleetProfileAdmissionDefect(str(error)) from error


def _user_has_profile_authority(user: User | None, *, mutation: bool) -> bool:
    """Check current authority at the user-request boundary."""
    if (
        user is None
        or user.disabled_at is not None
        or (
            mutation
            and user.role not in MUTATION_ROLES[("POST", "/api/profile/{number}/load")]
        )
    ):
        return False
    try:
        Actor(user.subject, user.role)
    except ValueError:
        return False
    return True
