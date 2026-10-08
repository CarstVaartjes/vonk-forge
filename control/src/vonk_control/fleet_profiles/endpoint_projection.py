"""Endpoint projection for Fleet profiles."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import cast as _typing_cast

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    DesiredAssignmentState,
    EndpointState,
    InvalidRequestReason,
    LifecycleState,
    ProfileReasonCode,
    RouteState,
    RunState,
)

from ..categorized_errors import MissingRecord
from ..fleet_profile_contract import (
    FleetProfileEndpointAssignmentIntent,
    FleetProfileEndpointIntent,
    FleetProfileEndpointProjectionIssue,
)
from ..lifecycle.evidence import BookkeepingReason, Residue, retire_as_unknown
from ..models import FleetProfile, FleetProfileApplication, FleetProfileSelection
from ..strict_json import stored_document_detail
from .contracts import (
    FleetProfileConflict,
)
from .persistence import (
    _persisted_profile_progress,
    _residue_detail,
)

if TYPE_CHECKING:
    from .service import FleetProfileService
    from .service import FleetProfileService as _FleetProfileService


class FleetProfileService:
    def endpoint_intent(
        self, session: Session, number: int
    ) -> FleetProfileEndpointIntent:
        """Resolve endpoint membership from the currently selected snapshot.

        The saved profile is deliberately not consulted: it may have been
        edited since the application whose workloads are currently loaded.
        The operation projection calls this with its own SQL session so the
        profile/application/run ownership read shares one database snapshot.
        """
        self = _typing_cast("_FleetProfileService", self)  # noqa: PLW0642 -- assembled mixin interface

        if type(number) is not int or number < 1:
            raise MissingRecord(number, reason=InvalidRequestReason.NOT_FOUND)
        profile = session.scalar(
            select(FleetProfile).where(FleetProfile.number == number)
        )
        if profile is None:
            return FleetProfileEndpointIntent(
                number=number,
                profile_id=None,
                application_id=None,
                application_state=None,
                assignments=(),
            )
        selection = session.get(FleetProfileSelection, 1)
        if selection is None or selection.profile_id != profile.id:
            return FleetProfileEndpointIntent(
                number=number,
                profile_id=profile.id,
                application_id=None,
                application_state=None,
                assignments=(),
            )
        application = session.get(FleetProfileApplication, selection.application_id)
        if application is None or application.profile_id != profile.id:
            # The selection names an application that is not stored: it is shown
            # as an unreadable intent (as a damaged plan is below), never refused.
            retire_as_unknown(
                "profile-selection",
                str(selection.application_id),
                BookkeepingReason.ROW_INCOMPLETE,
                "the selected profile application is not stored",
            )
            return FleetProfileEndpointIntent(
                number=number,
                profile_id=profile.id,
                application_id=selection.application_id,
                application_state=None,
                assignments=None,
                projection_issue=FleetProfileEndpointProjectionIssue(
                    code=ProfileReasonCode.APPLICATION_INTENT_INVALID,
                    detail="The selected profile application is unavailable.",
                ),
            )

        try:
            application_state, _next_attempt = self._presented_state(
                application, _persisted_profile_progress(application)
            )
        except ValueError:
            return FleetProfileEndpointIntent(
                number=number,
                profile_id=profile.id,
                application_id=application.id,
                application_state=None,
                assignments=None,
                projection_issue=FleetProfileEndpointProjectionIssue(
                    code=ProfileReasonCode.APPLICATION_INTENT_INVALID,
                    detail="The selected application state is unreadable.",
                ),
            )
        issue_detail: str | None = None
        try:
            intended = self._intended_profile(application, session=session)
        except (FleetProfileConflict, ValidationError) as error:
            cause = error.__cause__
            issue_detail = stored_document_detail(error)
            if issue_detail is None and isinstance(cause, Exception):
                issue_detail = stored_document_detail(cause)
            intended = None
        if isinstance(intended, Residue) or intended is None:
            # Broken historical plan or progress blocks execution, but it
            # should not hide the rest of this read-only endpoint projection.
            # The exact reviewed assignments remain unknown; never reconstruct
            # them from today's mutable saved profile.
            detail = (
                _residue_detail(intended)
                if isinstance(intended, Residue)
                else issue_detail
            )
            return FleetProfileEndpointIntent(
                number=number,
                profile_id=profile.id,
                application_id=application.id,
                application_state=application_state,
                assignments=None,
                projection_issue=FleetProfileEndpointProjectionIssue(
                    code=ProfileReasonCode.APPLICATION_INTENT_INVALID,
                    detail=detail
                    or "Stored application intent is invalid or inconsistent.",
                ),
            )
        projected: list[FleetProfileEndpointAssignmentIntent] = []
        for assignment in intended.assignments:
            if assignment.desired_state == DesiredAssignmentState.INSTALLED:
                projected.append(
                    FleetProfileEndpointAssignmentIntent(
                        assignment_id=assignment.id,
                        recipe_title=assignment.recipe_title,
                        desired_state=DesiredAssignmentState.INSTALLED,
                        alias=assignment.alias,
                        state=EndpointState.INSTALLED_ONLY,
                    )
                )
                continue

            if assignment.alias is None:
                state = EndpointState.UNAVAILABLE
                run_id = None
            else:
                current = self._assignment_state(session, assignment)
                run = current.run
                if (
                    run is not None
                    and run.alias == assignment.alias
                    and run.state == RunState.RUNNING
                    and run.route_state == RouteState.PUBLISHED
                ):
                    state = EndpointState.NOT_PUBLISHED_YET
                    run_id = run.id
                elif run is not None and run.route_state == RouteState.PENDING:
                    state = EndpointState.NOT_PUBLISHED_YET
                    run_id = None
                elif run is not None and run.route_state == RouteState.FAILED:
                    state = EndpointState.UNAVAILABLE
                    run_id = None
                elif application_state == LifecycleState.SUCCEEDED:
                    state = EndpointState.WITHDRAWN
                    run_id = None
                elif application_state in {
                    LifecycleState.FAILED,
                    LifecycleState.CANCELLED,
                    LifecycleState.SUPERSEDED,
                }:
                    state = EndpointState.UNAVAILABLE
                    run_id = None
                else:
                    state = EndpointState.NOT_PUBLISHED_YET
                    run_id = None
            projected.append(
                FleetProfileEndpointAssignmentIntent(
                    assignment_id=assignment.id,
                    recipe_title=assignment.recipe_title,
                    desired_state=DesiredAssignmentState.RUNNING,
                    alias=assignment.alias,
                    state=state,
                    expected_run_id=run_id,
                )
            )
        return FleetProfileEndpointIntent(
            number=number,
            profile_id=profile.id,
            application_id=application.id,
            application_state=application_state,
            assignments=tuple(projected),
        )
