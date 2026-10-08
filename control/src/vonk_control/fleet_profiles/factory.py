"""Factory for Fleet profiles."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session, sessionmaker

from ..model_cache_contract import CacheResolution
from ..run_switch_operations import RunSwitchOperationService

if TYPE_CHECKING:
    from .service import FleetProfileService


def build_production_fleet_profile_service(
    sessions: sessionmaker[Session],
    *,
    clock: Callable[[], datetime],
    run_switch_operations: RunSwitchOperationService,
    cache_resolver: Callable[..., CacheResolution] | None = None,
) -> FleetProfileService:
    """Compose the Controller's Fleet profile service and its authority.

    The Run/Switch adapter is both the apply boundary and the preparation
    provider.  Composing them in one place means a profile preview and the
    child operation it later queues always resolve the exact same model,
    recipe revision, target group and runtime image identity.  It also makes
    the preparation binding impossible to drop by accident: a deployment
    cannot construct this service without a provider that can attest the
    assets a plan requires.
    """

    from .run_switch_adapter import RunSwitchFleetProfileAdapter
    from .service import FleetProfileService

    adapter = RunSwitchFleetProfileAdapter(sessions, run_switch_operations)
    return FleetProfileService(
        sessions,
        clock=clock,
        switch_adapter=adapter,
        cache_resolver=cache_resolver,
        assessment_provider=adapter.assess,
    )
