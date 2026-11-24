"""Run-state sets with more than one reader."""

from __future__ import annotations

from vonk_agent_protocol.state_machines import RunState

# Terminal states are candidates only: an ended run can be reopened by a Spark
# that still reports its process, and may still hold reservations. Exact
# current-generation absence is independently required before releasing claims
# and discarding recovery rows.
ENDED_RUN_STATES = frozenset({RunState.FAILED, RunState.STOPPED})

#: A run within its start: planned or starting, owned by its Start operation.
STARTING_RUN_STATES = frozenset({RunState.PLANNED, RunState.STARTING})
#: States that hold a Spark's memory and ports: starting or running.
HOLDING_RUN_STATES = STARTING_RUN_STATES | {RunState.RUNNING}
