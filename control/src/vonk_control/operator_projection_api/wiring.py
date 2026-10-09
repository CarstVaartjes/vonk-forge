"""Operator projection api: wiring."""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from ..agent_api import AgentApiServices
from ..agent_upgrades import AgentUpgradeService
from .logs import AgentFailureLogProvider
from .services import FleetLogProvider, FleetOperatorServices, _AgentEnrollmentAdapter


def build_fleet_operator_services(
    *,
    agent_services: AgentApiServices | None,
    upgrades: AgentUpgradeService | None,
    logs: FleetLogProvider | None = None,
    sessions: sessionmaker[Session] | None = None,
) -> FleetOperatorServices:
    """Build Fleet action adapters from existing Controller authorities.

    ``logs`` must return retained authenticated evidence when configured.  If
    ``sessions`` is supplied, the helper builds the retained Controller
    evidence provider itself.  Neither
    path synthesizes remote log entries or falls back to SSH.
    """

    enrollment = (
        None if agent_services is None else _AgentEnrollmentAdapter(agent_services)
    )
    retained_logs = logs
    if retained_logs is None and sessions is not None:
        retained_logs = AgentFailureLogProvider(sessions)
    return FleetOperatorServices(
        enrollment=enrollment,
        upgrades=upgrades,
        logs=retained_logs,
        sessions=sessions,
    )
