"""Agent upgrades: service."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import httpx2
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    InvalidRequestReason,
)

from ..agent_jobs import (
    AgentJobService,
)
from ..categorized_errors import (
    InvalidValue,
)
from ..lifecycle.agent_upgrade import AgentUpgradeAdapter
from .acceptance import AcceptanceMixin
from .dispatch import DispatchMixin
from .eligibility import EligibilityMixin
from .package import PackageMixin
from .recovery import RecoveryMixin
from .resume import ResumeMixin


class AgentUpgradeService(
    PackageMixin,
    AcceptanceMixin,
    ResumeMixin,
    RecoveryMixin,
    DispatchMixin,
    EligibilityMixin,
):
    def __init__(
        self,
        sessions: sessionmaker[Session],
        operations: AgentJobService,
        *,
        clock: Callable[[], datetime],
        channel: str = "dev",
        release_api_url: str = "https://install.vonkforge.ai",
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self._sessions = sessions
        self._operations = operations
        self._clock = clock
        self._rollouts = AgentUpgradeAdapter()
        operations.set_rollout_owner(
            self._advance, self.advance_node, reconcile=self.heal_rollouts
        )
        if channel not in {"dev", "stable"}:
            raise InvalidValue(
                "agent upgrade channel is invalid",
                reason=InvalidRequestReason.MALFORMED,
            )
        self._channel = channel
        self._http = httpx2.Client(
            base_url=release_api_url,
            follow_redirects=False,
            timeout=httpx2.Timeout(15.0, connect=5.0),
            trust_env=False,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()
