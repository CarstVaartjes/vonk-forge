"""Run-state sets with more than one reader."""

from __future__ import annotations

from vonk_agent_protocol import RunState

# Terminal states are candidates only: an ended run can be reopened by a Spark
# that still reports its process, and may still hold reservations. Exact
# current-generation absence is independently required before releasing claims
# and discarding recovery rows.
ENDED_RUN_STATES = frozenset({RunState.FAILED, RunState.STOPPED})
