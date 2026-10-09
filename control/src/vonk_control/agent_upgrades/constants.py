"""Agent upgrades: constants."""

from __future__ import annotations

import re
from datetime import timedelta

from vonk_agent_protocol import (
    LifecycleState,
)

from .. import job_states
from ..agent_jobs import (
    AGENT_UPGRADE_RECOVERY_FENCE,
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


_ONLINE_WINDOW = timedelta(seconds=150)


# An ambiguous install result can leave durable apt/dpkg recovery in progress.
# Every automatic retry waits through this controller safety window.
_AGENT_UPGRADE_RECOVERY_FENCE = AGENT_UPGRADE_RECOVERY_FENCE


_ACTIVE_ROLLOUT_STATES = job_states.words(
    LifecycleState.QUEUED, LifecycleState.RUNNING, LifecycleState.NEEDS_OPERATOR
)


_ALREADY_CURRENT = "already runs the requested agent build"
