"""Pending application for Fleet profiles."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import InvalidRequestReason, LifecycleState, ProfileReasonCode
from vonk_agent_protocol.agent_words import ProfileReasonSeverity

from ..categorized_errors import MissingRecord
from ..fleet_profile_contract import (
    FleetProfileApplicationProgress,
    FleetProfileApplicationView,
    FleetProfileIntendedConfiguration,
    FleetProfileOperationKind,
    FleetProfilePreview,
    FleetProfileScope,
)
from ..lifecycle.evidence import Residue
from ..lifecycle.fleet_profile import FleetProfileAdapter
from ..lifecycle.types import State as _LifecycleState
from ..models import FleetProfile, FleetProfileApplication
from ..operation_blockers import OperationBlocker, make_blocker
from ..settings import (
    PROFILE_ADMISSION_OBSERVATION_WAIT_SECONDS,
    STORAGE_ADMISSION_RETRY_SECONDS,
    STORAGE_ADMISSION_WAIT_SECONDS,
)
from ..storage_demands import (
    STORAGE_EVICTION_TIMED_OUT,
    STORAGE_INSUFFICIENT,
    StorageRelief,
)
from .assessment_support import (
    _progress_with_blockers,
    _relief_blocker,
)
from .contracts import (
    FleetProfileAdmissionEffectBusy,
    FleetProfileInvalid,
    FleetProfileStalePlanConflict,
    _SelectedProfileSnapshot,
)
from .dependencies import _INSTALLATION_POLICY_ADAPTER, _LOGGER, _STORAGE_CODES
from .persistence import (
    _next_profile_acceptance_time,
    _owns_pending_admission,
    _persisted_profile_progress,
)
from .projection_support import (
    _aware,
    _digest,
    _profile_document,
)

if TYPE_CHECKING:
    from .service import FleetProfileService
    from .service import FleetProfileService as _FleetProfileService


class FleetProfileService:
    def _matching_application(
        self,
        row: FleetProfileApplication,
        *,
        session: Session,
        profile_id: str,
        reviewed_digest: str | None,
        actor: str,
        retry_of_application_id: str | None = None,
    ) -> FleetProfileApplicationView:
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        progress = _persisted_profile_progress(row)
        intended = self._intended_profile(row, session=session)
        if (
            row.profile_id != profile_id
            or row.actor != actor
            or progress.retry_of_application_id != retry_of_application_id
            or (
                reviewed_digest is not None
                and not isinstance(intended, Residue)
                and intended.reviewed_plan_digest != reviewed_digest
            )
        ):
            raise FleetProfileInvalid(
                "Fleet profile request key was reused for another plan",
                reason=InvalidRequestReason.CONFLICT,
            )
        return self._application_view(row)

    def _load_replay(
        self,
        profile_id: str,
        *,
        request_key: str,
        actor: str,
    ) -> FleetProfileApplicationView | None:
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        with self._sessions.begin() as session:
            self._authorize(session, actor)
            existing = session.scalar(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.request_key == request_key
                )
            )
            return (
                self._matching_application(
                    existing,
                    session=session,
                    profile_id=profile_id,
                    reviewed_digest=None,
                    actor=actor,
                )
                if existing is not None
                else None
            )

    def _create_pending_application(
        self,
        preview: FleetProfilePreview,
        *,
        request_key: str,
        actor: str,
        operation_kind: FleetProfileOperationKind,
        select_profile: bool = False,
        selection_precondition: _SelectedProfileSnapshot | None = None,
    ) -> FleetProfileApplicationView:
        """Persist reviewed intent before admission locks are reacquired.

        The row owns no workload ordinal, reservation, or child effect until
        the normal admission transaction binds it.  This lets the reconciler
        retry after a transient owner clears without holding a SQL lock or
        asking the operator to resubmit the same reviewed request.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        now = _aware(self._clock())
        application_id = str(uuid.uuid4())
        # The application identity is unique even while admission is parked.
        # Bind that execution identity before the first durable insert so two
        # concurrent reviewed loads cannot collide on the unique plan digest,
        # and so the receipt cannot change identity after a caller observes it.
        pending_plan_digest = _digest(
            {
                "schema_version": 2,
                "reconciliation_digest": preview.plan_digest,
                "retry_of_application_id": None,
                "request_key": request_key,
            }
        )
        pending_preview = preview.model_copy(
            update={"plan_digest": pending_plan_digest}
        )
        with self._sessions.begin() as session:
            # A selection precondition binds maintenance to existing accepted
            # intent; a new user request must still be authorized now.
            if selection_precondition is None:
                self._authorize(session, actor)
            existing = session.scalar(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.request_key == request_key
                )
            )
            if existing is not None:
                return self._matching_application(
                    existing,
                    session=session,
                    profile_id=preview.profile_id,
                    reviewed_digest=preview.plan_digest,
                    actor=actor,
                )
            profile = session.get(FleetProfile, preview.profile_id)
            if profile is None:
                raise MissingRecord(
                    preview.profile_id, reason=InvalidRequestReason.NOT_FOUND
                )
            if selection_precondition is None:
                if _digest(_profile_document(profile)) != preview.profile_digest:
                    raise FleetProfileStalePlanConflict(
                        "Fleet profile changed before its admission intent was persisted"
                    )
                installation_policy = _INSTALLATION_POLICY_ADAPTER.validate_python(
                    profile.installation_policy, strict=True
                )
            else:
                current = self._selected_profile_snapshot(session)
                if (
                    not select_profile
                    or current is None
                    or isinstance(current, Residue)
                    or current.generation != selection_precondition.generation
                    or current.application_id != selection_precondition.application_id
                    or current.roster_digest != selection_precondition.roster_digest
                    or selection_precondition.profile_id != preview.profile_id
                    or preview.profile_digest
                    != selection_precondition.intended.profile_digest
                    or preview.profile_revision
                    != selection_precondition.profile_revision
                ):
                    raise FleetProfileStalePlanConflict(
                        "Selected profile or fleet membership changed during reconciliation"
                    )
                installation_policy = (
                    selection_precondition.intended.installation_policy
                )
            intended = FleetProfileIntendedConfiguration(
                profile_digest=preview.profile_digest,
                reviewed_plan_digest=preview.plan_digest,
                reviewed_application_id=application_id,
                installation_policy=installation_policy,
                scope=FleetProfileScope(node_ids=list(preview.scope.node_ids)),
                assignments=list(preview.resolved_assignments),
            )
            created_at = _next_profile_acceptance_time(session, now)
            next_retry = FleetProfileAdapter.next_retry(application_id, 1, now)
            row = FleetProfileAdapter.new_application(
                id=application_id,
                request_key=request_key,
                profile_id=preview.profile_id,
                profile_digest=preview.profile_digest,
                plan_digest=pending_plan_digest,
                # Keep the short synchronous admission attempt visible as a
                # normal queued application.  If it cannot bind, the defer
                # path records the next automatic attempt before returning.
                state=LifecycleState.QUEUED.value,
                plan=pending_preview.model_dump(mode="json"),
                selection_generation=(
                    selection_precondition.generation
                    if selection_precondition is not None
                    else None
                ),
                current_step=0,
                current_operation_id=None,
                progress=FleetProfileApplicationProgress(
                    operation_kind=operation_kind,
                    admission_pending=True,
                    admission_attempt=0,
                    admission_retry_at=next_retry,
                    intended_profile=intended,
                    completed_steps=0,
                    total_steps=len(preview.steps),
                ).model_dump(mode="json"),
                result=None,
                status_reason=(
                    "Profile admission is busy; reviewed intent was accepted and "
                    f"will retry automatically after {next_retry.isoformat()}."
                )[:512],
                actor=actor,
                created_at=created_at,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            # Pending applications are not authority. In particular, keeping
            # the prior selection in place makes failed admission cleanup FK-safe.
            return self._application_view(row)

    def _defer_pending_application(
        self,
        application_id: str,
        reason: str,
        *,
        retry_delay: timedelta | None = None,
        blockers: Sequence[OperationBlocker] | None = None,
        code: str = ProfileReasonCode.ADMISSION_BUSY,
        storage: FleetProfileAdmissionEffectBusy | None = None,
        wait_for_space: bool = False,
    ) -> FleetProfileApplicationView:
        """Record bounded retry state after a nonblocking admission refusal.

        A refusal for disk (``storage``) asks the storage collector to free it
        and records what that can do: the wait is bounded and ends in a typed
        refusal when nothing more can be freed or the space does not come.

        ``wait_for_space`` is the load whose plan is still blocked (by disk
        among other things): space may also appear without eviction (a stop, a
        removal), so it keeps waiting while the bound allows and only then ends
        with the reason that holds the space, or ``storage.eviction_timed_out``.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        now = _aware(self._clock())
        relieved: list[tuple[str, StorageRelief]] = []
        if storage is not None:
            with self._sessions() as session:
                row = session.get(FleetProfileApplication, application_id)
                profile_id = row.profile_id if row is not None else application_id
            for node_id, needed in storage.shortfalls:
                found = self._ask_storage_relief(node_id, needed, profile_id)
                if found is not None:
                    relieved.append((node_id, found))
            if relieved:
                relief_blockers = [
                    _relief_blocker(node_id, item) for node_id, item in relieved
                ]
                if blockers is None or not wait_for_space:
                    blockers = relief_blockers
                else:
                    # The plan's other blockers stay; the disk ones are replaced
                    # by what the collector just said about them.
                    blockers = [
                        item for item in blockers if item.code not in _STORAGE_CODES
                    ] + relief_blockers
            retry_delay = timedelta(seconds=STORAGE_ADMISSION_RETRY_SECONDS)
        with self._sessions.begin() as session:
            row = session.get(
                FleetProfileApplication, application_id, with_for_update=True
            )
            if row is None:
                raise MissingRecord(
                    application_id, reason=InvalidRequestReason.NOT_FOUND
                )
            progress = _persisted_profile_progress(row)
            if not _owns_pending_admission(row, progress):
                return self._application_view(row)
            # The immutable acceptance timestamp is the durable general
            # observation budget. Preview/retry/restart cannot extend it.
            if now >= _aware(row.created_at) + timedelta(
                seconds=PROFILE_ADMISSION_OBSERVATION_WAIT_SECONDS
            ):
                row.progress = _progress_with_blockers(
                    progress,
                    list(blockers) if blockers is not None else progress.blockers,
                    admission_pending=False,
                    admission_retry_at=None,
                ).model_dump(mode="json")
                # Pending admission has issued no child effects. Ending as
                # cancelled keeps automatic failed-load recovery from starting
                # a new episode under this expired acceptance.
                self._lifecycle.cancelled(row, reason, now, session=session)
                return self._application_view(row)
            if self._end_exhausted_preparation(row, blockers or (), session):
                return self._application_view(row)
            if storage is not None:
                since = _aware(progress.storage_wait_since or now)
                final = next(
                    (
                        (node_id, item)
                        for node_id, item in relieved
                        if item.code == STORAGE_INSUFFICIENT
                        and (wait_for_space or not item.paused)
                    ),
                    None,
                )
                expired = now - since > timedelta(
                    seconds=STORAGE_ADMISSION_WAIT_SECONDS
                )
                if wait_for_space and not expired:
                    final = None
                if final is not None or expired:
                    # Nothing more can be freed (or the space did not come in
                    # time): end with the reason, not another silent retry.
                    node_id, item = final or (
                        storage.shortfalls[0][0],
                        relieved[0][1] if relieved else None,
                    )
                    waited = (
                        f"Disk did not free within {STORAGE_ADMISSION_WAIT_SECONDS}s"
                        " of waiting for eviction. "
                    )
                    detail = (
                        item.detail
                        if final is not None and item is not None
                        else waited
                        + (item.detail if item is not None else str(storage))
                    )
                    refusal = make_blocker(
                        STORAGE_INSUFFICIENT
                        if final is not None
                        else STORAGE_EVICTION_TIMED_OUT,
                        detail,
                        severity=ProfileReasonSeverity.ERROR.value,
                        node_ids=(node_id,),
                    )
                    row.progress = _progress_with_blockers(
                        progress,
                        [refusal],
                        admission_pending=False,
                        admission_retry_at=None,
                        storage_wait_since=since.isoformat(),
                    ).model_dump(mode="json")
                    _LOGGER.warning(
                        "profile application %s refused for disk: %s: %s",
                        application_id,
                        refusal.code,
                        refusal.detail,
                    )
                    self._lifecycle.fail(
                        row, f"{refusal.code}: {refusal.detail}", now, session=session
                    )
                    return self._application_view(row)
                storage_wait_since = since.isoformat()
            else:
                storage_wait_since = None
            attempt = progress.admission_attempt + 1
            next_retry = (
                FleetProfileAdapter.next_retry(application_id, attempt, now)
                if retry_delay is None
                else now + retry_delay
            )
            current_blockers = (
                list(blockers) if blockers is not None else [make_blocker(code, reason)]
            )
            row.progress = _progress_with_blockers(
                progress,
                current_blockers,
                admission_pending=True,
                admission_attempt=attempt,
                admission_retry_at=next_retry.isoformat(),
                storage_wait_since=storage_wait_since,
            ).model_dump(mode="json")
            if {(item.code, tuple(item.node_ids)) for item in current_blockers} != {
                (item.code, tuple(item.node_ids)) for item in progress.blockers
            }:
                _LOGGER.log(
                    logging.WARNING
                    if any(
                        item.severity == ProfileReasonSeverity.ERROR.value
                        for item in current_blockers
                    )
                    else logging.INFO,
                    "profile application %s is waiting to be admitted: %s; "
                    "next attempt %s",
                    application_id,
                    "; ".join(
                        f"{item.code}: {item.detail}" for item in current_blockers[:4]
                    ),
                    next_retry.isoformat(),
                )
            # Admission retries itself at ``admission_retry_at``; no operator
            # action is required, so the row stays queued with its next due time.
            self._lifecycle.project(
                row,
                now,
                state=_LifecycleState.QUEUED,
                reason=f"{reason} Next attempt: {next_retry.isoformat()}.",
                session=session,
            )
            session.flush()
            return self._application_view(row)

    def _discard_pending_application(self, application_id: str) -> None:
        """Delete only a still-owned pending receipt after submitter failure."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        with self._sessions.begin() as session:
            row = session.get(
                FleetProfileApplication, application_id, with_for_update=True
            )
            if row is None:
                return
            progress = _persisted_profile_progress(row)
            if _owns_pending_admission(row, progress):
                session.delete(row)
