"""Operation projection for Fleet profiles."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from pydantic import ValidationError
from sqlalchemy import String, cast, func, select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    InvalidRequestReason,
    LifecycleState,
    OperationFailureCode,
    OperationProgress,
    ProfileReasonCode,
    input_state,
)
from vonk_agent_protocol.agent_words import (
    ProfileAction,
    ProfileCancellationCause,
    ProfileChildPhase,
    ProfileOperationKind,
    ProfileProjectionKind,
    ProfileReportedPhase,
)

from .. import fleet_profile_states, job_states
from ..auth import MUTATION_ROLES
from ..categorized_errors import MissingRecord
from ..fleet_profile_adapter_conversion import conversion_observation, needs_conversion
from ..fleet_profile_contract import (
    FleetProfileApplicationProgress,
    FleetProfileApplicationView,
    FleetProfilePreview,
)
from ..lifecycle.evidence import Residue
from ..logging import redact_text
from ..models import FleetProfile, FleetProfileApplication, User
from ..operation_blockers import bound_blockers, make_blocker
from ..operation_contract import OperationFailureEvidence
from ..operation_item_contract import (
    OperationItem,
    OperationOwnerReference,
    OperationResultFacts,
)
from ..strict_json import warn_unreadable_once
from .activity import (
    _profile_activity_state,
    _profile_activity_state_expression,
)
from .contracts import (
    FleetProfileConflict,
    FleetProfilePermissionDenied,
)
from .persistence import (
    _persisted_profile_plan,
    _persisted_profile_progress,
    _persisted_profile_result,
    _persisted_profile_scope,
)
from .projection_support import (
    _application_cancellation_view,
    _aware,
)

if TYPE_CHECKING:
    from .service import FleetProfileService
    from .service import FleetProfileService as _FleetProfileService
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..operation_api import OperationProviderProtocol


class FleetProfileService:
    def operation_provider(self) -> OperationProviderProtocol:
        """Project profile applications into the global Activity provider contract."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        # Keep this import local so the profile domain remains usable by the
        # profile routes when the optional Activity projection is unavailable.
        from ..operation_api import (
            OperationListPage,
            OperationProvider,
            OperationQuery,
            _activity_keyset_filter,
        )

        def list_operations(query: OperationQuery) -> OperationListPage:
            after = query.after
            state = query.state
            node_id = query.node_id
            limit = query.limit
            with self._sessions() as session:
                base_statement = select(FleetProfileApplication).order_by(
                    FleetProfileApplication.created_at.desc(),
                    FleetProfileApplication.id.desc(),
                )
                if isinstance(state, str):
                    # A filter may still name a retired spelling (one release).
                    named = input_state(state)
                    base_statement = base_statement.where(
                        _profile_activity_state_expression().in_(
                            fleet_profile_states.words(named)
                            if named is not None
                            else (state,)
                        )
                    )
                if isinstance(query.request_id, str):
                    base_statement = base_statement.where(
                        FleetProfileApplication.request_key == query.request_id
                    )
                # The plan is canonical JSON. Quoted containment avoids matching
                # a node-id substring while keeping this projection portable across
                # PostgreSQL JSON and SQLite JSON test databases.
                if isinstance(node_id, str):
                    base_statement = base_statement.where(
                        cast(
                            FleetProfileApplication.plan["scope"]["node_ids"],
                            String,
                        ).like(f'%"{node_id}"%')
                    )
                total = int(
                    session.scalar(
                        select(func.count()).select_from(base_statement.subquery())
                    )
                    or 0
                )
                statement = base_statement
                boundary = _activity_keyset_filter(
                    FleetProfileApplication.created_at,
                    FleetProfileApplication.id,
                    "",
                    after,
                )
                if boundary is not None:
                    statement = statement.where(boundary)
                rows = tuple(session.scalars(statement.limit(limit)))
                return OperationListPage(
                    items=[self._activity_item(session, row) for row in rows],
                    next_cursor=None,
                    total=total,
                )

        def get_operation(operation_id: str) -> OperationItem:
            with self._sessions() as session:
                row = session.get(FleetProfileApplication, operation_id)
                if row is None:
                    raise MissingRecord(
                        operation_id, reason=InvalidRequestReason.NOT_FOUND
                    )
                return self._activity_item(session, row)

        return OperationProvider(
            family="fleet-profile",
            list_operations=list_operations,
            get_operation=get_operation,
        )

    def _activity_item(
        self, session: Session, row: FleetProfileApplication
    ) -> OperationItem:
        """One Activity row, with retry facts read once in the row's session."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        try:
            progress = _persisted_profile_progress(row)
            retrying = (
                row.state == LifecycleState.FAILED.value
                and self._recovery_wanted(session, row, progress)
            )
        except (FleetProfileConflict, ValidationError, TypeError, ValueError):
            retrying = False
        return self._operation_item(
            row,
            retry_available=self._retry_eligible(session, row),
            retrying=retrying,
        )

    @staticmethod
    def _operation_scope(plan: FleetProfilePreview) -> tuple[str, ...]:
        return tuple(plan.scope.node_ids)

    @classmethod
    def _operation_phase(
        cls,
        row: FleetProfileApplication,
        plan: FleetProfilePreview,
        progress: FleetProfileApplicationProgress,
    ) -> str:
        cls = _typing_cast("type[_FleetProfileService]", cls)  # noqa: PLW0642 -- assembled mixin interface
        if row.state == LifecycleState.SUCCEEDED.value:
            return ProfileReportedPhase.FINAL_VERIFY.value
        if 0 <= row.current_step < len(plan.steps):
            kind = plan.steps[row.current_step].kind
            if kind == ProfileChildPhase.PREPARE.value:
                return ProfileChildPhase.PREPARE.value
            if kind == ProfileAction.SWITCH.value:
                if progress.child_progress is not None:
                    return progress.child_progress.phase
                return ProfileChildPhase.PREPARE.value
        return ProfileReportedPhase.FINAL_VERIFY.value

    @classmethod
    def _operation_item(
        cls,
        row: FleetProfileApplication,
        *,
        retry_available: bool = False,
        retrying: bool = False,
    ) -> OperationItem:
        """Project profile progress and its operator-visible failure into Activity.

        A single damaged historical row must not fail the whole page and must
        not be hidden as an empty success: its unreadable document becomes an
        explicit failure on that record while every readable record stays
        usable.
        """
        cls = _typing_cast("type[_FleetProfileService]", cls)  # noqa: PLW0642 -- assembled mixin interface

        if needs_conversion(row):
            observed = conversion_observation(row)
            item = cls._unreadable_operation_item(row)
            item.status_reason = observed.detail
            item.next_attempt_at = (
                observed.next_attempt_at.isoformat()
                if observed.next_attempt_at
                else None
            )
            item.blockers = bound_blockers(
                [
                    make_blocker(
                        ProfileReasonCode.RETRY_CONFLICT,
                        observed.detail
                        or "Exact retained child evidence is being reconciled",
                    )
                ]
            )
            return item
        try:
            typed_progress = _persisted_profile_progress(row)
        except (FleetProfileConflict, ValidationError, TypeError, ValueError):
            warn_unreadable_once("profile application", row.id)
            return cls._unreadable_operation_item(row)
        plan = _persisted_profile_plan(row)
        result = _persisted_profile_result(row)
        if isinstance(plan, Residue):
            warn_unreadable_once("profile application", row.id)
            return cls._unreadable_operation_item(row)
        cancellation = _application_cancellation_view(row, plan, typed_progress)
        state = _profile_activity_state(row.state, typed_progress.cancellation)
        if retrying and state == LifecycleState.FAILED.value:
            # The Controller will retry this by itself: it is waiting, not failed.
            state = LifecycleState.QUEUED.value
        next_attempt = None
        if state == LifecycleState.QUEUED.value:
            next_attempt = (
                typed_progress.retry_due_at
                if retrying
                else typed_progress.admission_retry_at
                if typed_progress.admission_pending
                else None
            )
        failure = None
        if state in job_states.words(
            LifecycleState.FAILED, LifecycleState.NEEDS_OPERATOR
        ):
            # A failed row that recorded no reason still shows what is known of it.
            reason = (
                row.status_reason
                if row.status_reason and row.status_reason.strip()
                else f"The profile application ended {state} without a recorded reason"
            )
            failure = OperationFailureEvidence(
                error_code=OperationFailureCode.FLEET_PROFILE_APPLICATION_FAILED,
                summary=(
                    "Profile application needs attention"
                    if state in fleet_profile_states.NEEDS_OPERATOR
                    else "Profile application failed"
                ),
                detail=redact_text(reason),
                retryable=retry_available,
                uncertain=state in fleet_profile_states.NEEDS_OPERATOR,
            )
        return OperationItem(
            id=row.id,
            parent_id=typed_progress.retry_of_application_id,
            node_ids=list(cls._operation_scope(plan)),
            kind=typed_progress.operation_kind or ProfileOperationKind.APPLY.value,
            state=state,
            attempt=typed_progress.attempt,
            progress=OperationProgress.model_validate(
                {"phase": cls._operation_phase(row, plan, typed_progress)}
            ),
            created_at=_aware(row.created_at).isoformat(),
            updated_at=_aware(row.updated_at).isoformat(),
            supported_actions=["retry"] if retry_available else [],
            owner=OperationOwnerReference(
                kind=ProfileProjectionKind.FLEET_APPLICATION.value,
                id=row.id,
                request_id=row.request_key,
            ),
            failure=failure,
            superseded_by=(
                typed_progress.superseded_by
                if state == ProfileCancellationCause.SUPERSEDED.value
                else None
            ),
            reason_code=(
                typed_progress.supersede_code
                if state == ProfileCancellationCause.SUPERSEDED.value
                else None
            ),
            result=OperationResultFacts.of(result),
            cancellation=cancellation,
            status_reason=(
                redact_text(row.status_reason)
                if (
                    cancellation is not None
                    or state
                    in {
                        LifecycleState.QUEUED.value,
                        ProfileCancellationCause.SUPERSEDED.value,
                    }
                )
                and row.status_reason is not None
                else None
            ),
            blockers=(
                list(typed_progress.blockers)
                if state
                in job_states.words(
                    LifecycleState.QUEUED,
                    LifecycleState.RUNNING,
                    LifecycleState.FAILED,
                    LifecycleState.NEEDS_OPERATOR,
                )
                else []
            ),
            next_attempt_at=next_attempt.isoformat()
            if next_attempt is not None
            else None,
        )

    @staticmethod
    def _unreadable_operation_item(row: FleetProfileApplication) -> OperationItem:
        """Project a damaged application instead of letting it break Activity.

        Durable columns stay authoritative for identity and declared scope;
        only the unreadable document is replaced by an explicit failure, and
        no retry is offered because intent cannot be proven.
        """

        return OperationItem(
            id=row.id,
            node_ids=list(_persisted_profile_scope(row) or ()),
            kind=ProfileOperationKind.APPLY.value,
            state=row.state,
            attempt=0,
            created_at=_aware(row.created_at).isoformat(),
            updated_at=_aware(row.updated_at).isoformat(),
            supported_actions=[],
            owner=OperationOwnerReference(
                kind=ProfileProjectionKind.FLEET_APPLICATION.value,
                id=row.id,
                request_id=row.request_key,
            ),
            status_reason="Stored profile application record is unreadable.",
        )

    def application(self, application_id: str) -> FleetProfileApplicationView:
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface
        with self._sessions() as session:
            row = session.get(FleetProfileApplication, application_id)
            if row is None:
                raise MissingRecord(
                    application_id, reason=InvalidRequestReason.NOT_FOUND
                )
            return self._application_view(row)

    def application_cancellation_by_request(
        self,
        application_id: str,
        request_key: str,
        *,
        actor: str,
    ) -> FleetProfileApplicationView:
        """Resolve one accepted cancellation for its exact actor and owner."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        with self._sessions() as session:
            self._authorize(session, actor, mutation=False)
            current_role = session.scalar(
                select(User.role).where(User.subject == actor)
            )
            if (
                current_role
                not in MUTATION_ROLES[
                    ("POST", "/api/profile/applications/{application_id}/cancel")
                ]
            ):
                raise FleetProfilePermissionDenied(
                    "Current profile cancellation authority is unavailable"
                )
            row = session.get(FleetProfileApplication, application_id)
            if row is None:
                raise MissingRecord(
                    application_id, reason=InvalidRequestReason.NOT_FOUND
                )
            intent = _persisted_profile_progress(row).cancellation
            if (
                intent is None
                or intent.cause != ProfileCancellationCause.OPERATOR.value
                or intent.request_key != request_key
                or intent.actor != actor
            ):
                raise MissingRecord(request_key, reason=InvalidRequestReason.NOT_FOUND)
            return self._application_view(row)

    def application_by_request_key(
        self,
        request_key: str,
        *,
        actor: str,
        number: int | None = None,
    ) -> FleetProfileApplicationView:
        """Resolve one accepted submission after its response was lost."""
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        with self._sessions() as session:
            self._authorize(session, actor, mutation=False)
            row = session.scalar(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.request_key == request_key
                )
            )
            if row is None or row.actor != actor:
                raise MissingRecord(request_key, reason=InvalidRequestReason.NOT_FOUND)
            if (
                number is not None
                and session.scalar(
                    select(FleetProfile.number).where(FleetProfile.id == row.profile_id)
                )
                != number
            ):
                raise MissingRecord(request_key, reason=InvalidRequestReason.NOT_FOUND)
            return self._application_view(row)
