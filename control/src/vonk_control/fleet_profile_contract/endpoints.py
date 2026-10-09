"""Fleet profile contract: endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    Field,
    StringConstraints,
    model_validator,
)
from vonk_agent_protocol import (
    DesiredAssignmentState,
    EndpointState,
    ProfileReasonCode,
)

from ..endpoint_contract import EndpointResponse
from ..integer_domains import MAX_DATABASE_INTEGER
from ..strict_json import StrictModel
from .vocabulary import (
    Alias,
    DesiredAssignmentStateField,
    FleetProfileEndpointState,
    FleetProfileOperationState,
    Name,
    UuidId,
)


class FleetProfileEndpointProjectionIssue(StrictModel):
    """Safe diagnostic for immutable profile history that cannot be read."""

    code: Literal[ProfileReasonCode.APPLICATION_INTENT_INVALID]
    detail: Annotated[str, StringConstraints(min_length=1, max_length=512)]


@dataclass(frozen=True)
class FleetProfileEndpointAssignmentIntent:
    """Loaded application assignment and exact current run candidate."""

    assignment_id: str
    recipe_title: str
    desired_state: DesiredAssignmentStateField
    alias: str | None
    state: FleetProfileEndpointState
    expected_run_id: str | None = None


@dataclass(frozen=True)
class FleetProfileEndpointIntent:
    """Loaded profile intent captured from the durable application record."""

    number: int
    profile_id: str | None
    application_id: str | None
    application_state: FleetProfileOperationState | None
    assignments: tuple[FleetProfileEndpointAssignmentIntent, ...] | None
    projection_issue: FleetProfileEndpointProjectionIssue | None = None


class FleetProfileEndpointAssignmentView(StrictModel):
    assignment_id: UuidId
    recipe_title: Name
    desired_state: DesiredAssignmentStateField
    alias: Alias | None = None
    state: FleetProfileEndpointState
    endpoint: EndpointResponse | None = None

    @model_validator(mode="after")
    def endpoint_matches_assignment(self) -> FleetProfileEndpointAssignmentView:
        if self.state == EndpointState.PUBLISHED:
            if self.alias is None or self.endpoint is None:
                raise ValueError("published profile endpoint is incomplete")
            if self.endpoint.alias != self.alias:
                raise ValueError("published profile endpoint alias is inconsistent")
        elif self.endpoint is not None:
            raise ValueError("unpublished profile endpoint contains route data")
        if (
            self.state == EndpointState.INSTALLED_ONLY
            and self.desired_state != DesiredAssignmentState.INSTALLED
        ):
            raise ValueError("installed-only endpoint has a running assignment")
        return self


class FleetProfileEndpointsView(StrictModel):
    number: int = Field(le=MAX_DATABASE_INTEGER, ge=1)
    profile_id: UuidId | None = None
    application_id: UuidId | None = None
    application_state: FleetProfileOperationState | None = None
    observed_at: datetime
    # means the immutable application could not be validated.
    assignments: list[FleetProfileEndpointAssignmentView] | None = Field(max_length=64)
    projection_issue: FleetProfileEndpointProjectionIssue | None = None

    @model_validator(mode="after")
    def application_identity_is_consistent(self) -> FleetProfileEndpointsView:
        if self.application_state is not None and self.application_id is None:
            raise ValueError("profile endpoint application identity is incomplete")
        if (
            self.application_id is not None
            and self.application_state is None
            and self.projection_issue is None
        ):
            raise ValueError(
                "profile endpoint application state is unknown without a reason"
            )
        if self.application_id is not None and self.profile_id is None:
            raise ValueError("profile endpoint application has no profile identity")
        if (self.assignments is None) != (self.projection_issue is not None):
            raise ValueError("profile endpoint membership availability is inconsistent")
        if self.projection_issue is not None and self.application_id is None:
            raise ValueError("profile endpoint projection issue has no application")
        return self
