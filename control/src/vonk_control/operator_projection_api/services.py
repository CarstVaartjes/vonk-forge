"""Operator projection api: services."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    EnrollmentGrantState,
)

from ..agent_api import AgentApiServices, EnrollmentGrantResponse
from ..agent_upgrade_contract import AgentUpgradePackage, AgentUpgradeRequestIntent
from ..enrollment import (
    MAX_ENROLLMENT_GRANT_TTL_SECONDS,
)
from ..enrollment_bootstrap import accepted_installer_url
from ..enrollment_contract import (
    EnrollmentGrant,
    EnrollmentGrantStatus,
    EnrollmentObservationOutcome,
    EnrollmentRevocationStatus,
)
from .contracts import FleetActionResponse, FleetLogResponse


class FleetLogProvider(Protocol):
    def list(
        self,
        node_id: str,
        *,
        since: datetime | None,
        lines: int,
        recipe: str | None,
        source: str | None,
        follow: bool,
    ) -> FleetLogResponse: ...


class FleetEnrollmentProvider(Protocol):
    def create_named(
        self, *, name: str, actor: str, request_id: str
    ) -> FleetActionResponse: ...

    def create_reenrollment(
        self, node_id: str, actor: str, request_id: str
    ) -> FleetActionResponse: ...

    def revoke_node(self, node_id: str, actor: str) -> EnrollmentRevocationStatus: ...

    def grant_status(self, grant_id: str, *, actor: str) -> EnrollmentGrantStatus: ...

    def revoke_grant(
        self, grant_id: str, *, actor: str
    ) -> EnrollmentGrantStatus | EnrollmentObservationOutcome: ...


class FleetUpgradeProvider(Protocol):
    def current_package(self) -> AgentUpgradePackage: ...

    def get_request(
        self,
        request_id: str,
        *,
        actor: str,
        request_intent: AgentUpgradeRequestIntent,
    ) -> Any | None: ...

    def preview(
        self,
        node_ids: Sequence[str],
        package: AgentUpgradePackage,
        *,
        request_intent: AgentUpgradeRequestIntent,
    ) -> Any: ...

    def apply(
        self,
        node_ids: Sequence[str],
        package: AgentUpgradePackage,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        request_intent: AgentUpgradeRequestIntent,
    ) -> Any: ...


class FleetOperatorServices:
    def __init__(
        self,
        *,
        enrollment: FleetEnrollmentProvider | None = None,
        upgrades: FleetUpgradeProvider | None = None,
        logs: FleetLogProvider | None = None,
        sessions: sessionmaker[Session] | None = None,
    ) -> None:
        self.sessions = sessions
        self.enrollment = enrollment
        self.upgrades = upgrades
        self.logs = logs


class _AgentEnrollmentAdapter:
    """Expose the existing enrollment authority in the operator contract."""

    def __init__(self, services: AgentApiServices) -> None:
        self._services = services

    def _required(self) -> AgentApiServices:
        if self._services.enrollment is None:
            raise RuntimeError("agent enrollment is unavailable")
        if self._services.bootstrap is None:
            raise RuntimeError("agent enrollment bootstrap is unavailable")
        return self._services

    def _response(self, grant: EnrollmentGrant) -> EnrollmentGrantResponse:
        services = self._required()
        bootstrap = services.bootstrap
        assert bootstrap is not None
        return EnrollmentGrantResponse(
            id=grant.id,
            expires_at=grant.expires_at.isoformat(),
            purpose=grant.purpose,
            token=grant.token,
            controller_endpoint=bootstrap.controller_endpoint,
            enrollment_endpoint=bootstrap.enrollment_endpoint,
            ca_fingerprint=bootstrap.ca_fingerprint,
            controller_address=bootstrap.controller_address,
            service_hostnames=list(bootstrap.service_hostnames),
            installer_url=accepted_installer_url(bootstrap.installer_url),
        )

    def create_named(
        self, *, name: str, actor: str, request_id: str
    ) -> FleetActionResponse:
        services = self._required()
        assert services.enrollment is not None
        grant = services.enrollment.create_named(
            name, actor, MAX_ENROLLMENT_GRANT_TTL_SECONDS, request_key=request_id
        )
        if isinstance(grant, EnrollmentObservationOutcome):
            return FleetActionResponse(
                action="enroll", state=grant.state, observation=grant
            )
        if isinstance(grant, EnrollmentGrantStatus):
            return FleetActionResponse(
                action="enroll",
                display_name=name,
                state=grant.state,
                grant_status=grant,
            )
        return FleetActionResponse(
            action="enroll",
            display_name=name,
            state=EnrollmentGrantState.PENDING,
            grant=self._response(grant),
        )

    def create_reenrollment(
        self, node_id: str, actor: str, request_id: str
    ) -> FleetActionResponse:
        services = self._required()
        assert services.enrollment is not None
        grant = services.enrollment.create_reenrollment(
            node_id, actor, MAX_ENROLLMENT_GRANT_TTL_SECONDS, request_key=request_id
        )
        if isinstance(grant, EnrollmentObservationOutcome):
            return FleetActionResponse(
                action="re-enroll",
                node_id=node_id,
                state=grant.state,
                observation=grant,
            )
        if isinstance(grant, EnrollmentGrantStatus):
            return FleetActionResponse(
                action="re-enroll",
                node_id=node_id,
                state=grant.state,
                grant_status=grant,
            )
        return FleetActionResponse(
            action="re-enroll",
            node_id=node_id,
            state=EnrollmentGrantState.PENDING,
            grant=self._response(grant),
        )

    def grant_status(self, grant_id: str, *, actor: str) -> EnrollmentGrantStatus:
        enrollment = self._services.enrollment
        if enrollment is None:
            raise RuntimeError("agent enrollment is unavailable")
        return enrollment.grant_status(grant_id, actor=actor)

    def revoke_grant(
        self, grant_id: str, *, actor: str
    ) -> EnrollmentGrantStatus | EnrollmentObservationOutcome:
        enrollment = self._services.enrollment
        if enrollment is None:
            raise RuntimeError("agent enrollment is unavailable")
        return enrollment.revoke_grant(grant_id, actor=actor)

    def revoke_node(self, node_id: str, actor: str) -> EnrollmentRevocationStatus:
        services = self._required()
        assert services.enrollment is not None
        services.enrollment.revoke_node(node_id, actor)
        return services.enrollment.revocation_status(node_id)
