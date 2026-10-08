"""Selection for Fleet profiles."""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from time import monotonic
from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session
from vonk_agent_protocol.agent_words import (
    ProfileEffectState,
)

from ..admission_locking import (
    AdmissionLockBusy,
    acquire_admission_keys,
    is_admission_contention,
    node_admission_key,
)
from ..auth import MUTATION_ROLES, Actor
from ..fleet_profile_contract import FleetProfileApplicationProgress
from ..lifecycle.evidence import Damaged, Residue, read_or_rebuild
from ..models import FleetProfileApplication, FleetProfileSelection, User
from ..profile_capacity import release_unassigned_profile_claims
from ..settings import DATABASE_WAIT_BUDGETS
from ..user_authority import serialize_user_authority
from .contracts import (
    FleetProfileAdmissionBusy,
    FleetProfileAdmissionEffectBusy,
    FleetProfileAdmissionStorageError,
    FleetProfileConflict,
    FleetProfilePermissionDenied,
    _SelectedProfileSnapshot,
)
from .persistence import (
    _persisted_profile_plan,
    _persisted_profile_progress,
)
from .projection_support import (
    _aware,
    _roster_digest,
)

if TYPE_CHECKING:
    from .service import FleetProfileService
    from .service import FleetProfileService as _FleetProfileService


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

        def read() -> _SelectedProfileSnapshot | Damaged:
            application = session.get(FleetProfileApplication, selection.application_id)
            if application is None or application.profile_id != selection.profile_id:
                return Damaged("selected profile application is unavailable")
            if application.selection_generation != selection.generation:
                return Damaged("selected profile generation is inconsistent")
            try:
                intended = self._intended_profile(application, session=session)
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
                generation=selection.generation,
                profile_id=selection.profile_id,
                profile_revision=selection.profile_revision,
                application_id=selection.application_id,
                roster_node_ids=tuple(intended.scope.node_ids),
                roster_digest=selection.roster_digest,
                actor=application.actor,
                intended=intended,
                plan=plan,
            )

        return read_or_rebuild(
            kind="profile-selection",
            subject=str(selection.application_id),
            read=read,
        )

    @staticmethod
    def _application_is_current_selection(
        session: Session,
        application: FleetProfileApplication,
        progress: FleetProfileApplicationProgress | None = None,
    ) -> bool:
        from .service import FleetProfileService

        if progress is None:
            progress = _persisted_profile_progress(application)
        if FleetProfileService._adopted_application_scope(session, application):
            return True
        generation = application.selection_generation
        selection = session.get(FleetProfileSelection, 1)
        if selection is None:
            return False
        accepted_intent = progress.intended_profile
        if accepted_intent is None:
            return False
        seen: set[str] = set()
        current = application
        current_progress = progress
        deadline = monotonic() + DATABASE_WAIT_BUDGETS.statement_timeout_ms / 1000
        while monotonic() < deadline:
            if current.id in seen:
                return False
            seen.add(current.id)
            current_intent = current_progress.intended_profile
            if current_intent is None or current_intent != accepted_intent:
                return False
            generation = current.selection_generation
            if generation is not None:
                return bool(
                    selection.generation == generation
                    and selection.application_id == current.id
                    and selection.profile_id == current.profile_id
                    and current.profile_id == application.profile_id
                )
            retry_parent_id = current_progress.retry_of_application_id
            if retry_parent_id is None:
                return False
            current = session.get(FleetProfileApplication, retry_parent_id)
            if current is None:
                return False
            try:
                current_progress = _persisted_profile_progress(current)
            except FleetProfileConflict:
                return False
        return False

    @staticmethod
    def _authorize(
        session: Session,
        actor: str,
        *,
        mutation: bool = True,
    ) -> None:
        # Serialize current authority only when accepting a new user mutation.
        from .service import FleetProfileService

        if mutation:
            serialize_user_authority(session)
        user = session.scalar(select(User).where(User.subject == actor))
        if not FleetProfileService._user_has_profile_authority(user, mutation=mutation):
            raise FleetProfilePermissionDenied(
                "Current profile authority is unavailable"
            )

    @staticmethod
    def _user_has_profile_authority(user: User | None, *, mutation: bool) -> bool:
        """Check current authority at the user-request boundary."""
        if (
            user is None
            or user.disabled_at is not None
            or (
                mutation
                and user.role
                not in MUTATION_ROLES[("POST", "/api/profile/{number}/load")]
            )
        ):
            return False
        try:
            Actor(user.subject, user.role)
        except ValueError:
            return False
        return True

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
        with self._sessions.begin() as session:
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
                    "Profile admission is busy"
                    + (
                        f" ({error.holder} is changing a selected Spark)"
                        if error.holder
                        else ""
                    )
                    + "; review again after the current fleet, catalog, workload or capacity change completes",
                    holder=error.holder,
                ) from error
            except OperationalError as error:
                if is_admission_contention(error):
                    raise FleetProfileAdmissionBusy(
                        "Profile admission is busy; review again after the current fleet, catalog, workload or capacity change completes"
                    ) from None
                raise
            try:
                yield session
            except IntegrityError as error:
                diagnostic = getattr(error.orig, "diag", None)
                constraint = getattr(diagnostic, "constraint_name", None)
                sqlstate = getattr(error.orig, "sqlstate", None)
                # Never include SQL, parameters or raw driver error text.
                name = (
                    constraint
                    if isinstance(constraint, str)
                    and re.fullmatch(r"[A-Za-z0-9_]{1,128}", constraint)
                    else "unknown constraint"
                )
                code = (
                    sqlstate
                    if isinstance(sqlstate, str)
                    and re.fullmatch(r"[0-9A-Z]{5}", sqlstate)
                    else ProfileEffectState.UNKNOWN.value
                )
                raise FleetProfileAdmissionStorageError(
                    f"Profile admission database constraint {name} rejected the write (SQLSTATE {code}). "
                    "The database contract must be reconciled; this request is retained and will retry automatically."
                ) from None
            except AdmissionLockBusy as error:
                raise FleetProfileAdmissionEffectBusy(
                    "Profile admission is busy; review again after the current fleet, catalog, workload or capacity change completes"
                ) from error
            except OperationalError as error:
                if is_admission_contention(error):
                    raise FleetProfileAdmissionEffectBusy(
                        "Profile admission is busy; review again after the current fleet, catalog, workload or capacity change completes"
                    ) from None
                raise
