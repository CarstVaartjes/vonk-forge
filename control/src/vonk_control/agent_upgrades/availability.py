"""Agent upgrades: whether a Spark has an upgrade to take, for the fleet read.

The answer is the rollout's own: a Spark is current when it runs the release's
build and binary digest, and is skipped when it can never take the package. The
release is read off the request path, so a slow or unreachable channel never
delays a fleet read; until it has been read, the answer is unknown.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timedelta

from vonk_control.agent_upgrades.contract import AgentUpgradePackage
from vonk_control.agent_upgrades.eligibility import EligibilityMixin
from vonk_control.agent_upgrades.errors import AgentUpgradeConflict
from vonk_control.inventory.items import runtime_value
from vonk_control.models.fleet import AgentNode

REFRESH_AFTER = timedelta(seconds=60)


class UpgradeAvailability:
    """``reader()(node)``: True, False, or None while it cannot be told."""

    def __init__(
        self,
        clock: Callable[[], datetime],
        *,
        spawn: Callable[[Callable[[], None]], None] = lambda work: threading.Thread(
            target=work, daemon=True
        ).start(),
    ) -> None:
        self._clock = clock
        self._spawn = spawn
        self._read_package: Callable[[], AgentUpgradePackage] | None = None
        self._package: AgentUpgradePackage | None = None
        self._read_at: datetime | None = None
        self._reading = threading.Lock()

    def bind(self, read_package: Callable[[], AgentUpgradePackage]) -> None:
        self._read_package = read_package

    def _refresh(self) -> None:
        if self._read_package is None or not self._reading.acquire(blocking=False):
            return
        try:
            self._package = self._read_package()
        except AgentUpgradeConflict:
            # Keep the last release read; the next stale read tries again.
            pass
        finally:
            self._read_at = self._clock()
            self._reading.release()

    def package(self) -> AgentUpgradePackage | None:
        now = self._clock()
        if self._read_at is None or now - self._read_at > REFRESH_AFTER:
            self._spawn(self._refresh)
        return self._package

    def reader(self) -> Callable[[AgentNode], bool | None]:
        """The answer for one fleet read: the release is looked at once."""

        package = self.package()

        def available(node: AgentNode) -> bool | None:
            if package is None:
                return None
            if runtime_value(node, lambda identity: identity.architecture) is None:
                # The Spark has not reported what it runs.
                return None
            if EligibilityMixin._at_target(node, package):
                return False
            return EligibilityMixin._permanent_ineligible_reason(node, package) is None

        return available
