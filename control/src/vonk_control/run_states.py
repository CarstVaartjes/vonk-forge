"""Run-state sets with more than one reader."""

from __future__ import annotations

from vonk_agent_protocol import RunState

# A run in either state holds no claim and runs nothing the Controller plans.
ENDED_RUN_STATES = frozenset({RunState.FAILED, RunState.STOPPED})
