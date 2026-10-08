"""Constants for digest-bound recipe operations."""

from __future__ import annotations

from vonk_agent_protocol import AgentOperation as WireAgentOperation

_BLOCKER_REASON_CHARS = 200


def _bounded_blocker_reason(code: str, detail: str) -> str:
    """Keep the innermost cause visible when a blocker reason is bounded.

    Blocker details compose as "outer context: inner cause", so a prefix-only
    bound discards the actionable part: a live refusal reported
    "...is unavailable: runtime image receipt iden" and nothing else, leaving
    the failing rule invisible on every operator surface.  The head names the
    check; the tail carries the cause.
    """

    rendered = f"{code}: {detail}"
    if len(rendered) <= _BLOCKER_REASON_CHARS:
        return rendered
    keep = _BLOCKER_REASON_CHARS - 3
    head = keep // 2
    return f"{rendered[:head]}...{rendered[-(keep - head) :]}"


_INITIAL_OBSERVATION_GRACE_SECONDS = 120


_STOP_WITHDRAWAL_ATTEMPTS = 3


_MEMORY_RESERVATION_KINDS = frozenset({"unified-memory", "host-memory", "gpu-memory"})


_MAX_ACTION_NODES = 1024


_MAX_ACTIVE_RUNS = 128


_WORKLOAD_INTENT_KINDS = frozenset(
    {
        WireAgentOperation.RECIPE_INSTALL.value,
        WireAgentOperation.RECIPE_START.value,
        WireAgentOperation.RECIPE_STOP.value,
        WireAgentOperation.RECIPE_UNINSTALL.value,
        WireAgentOperation.RECIPE_RECONCILE.value,
        WireAgentOperation.RECIPE_JOB_RUN.value,
    }
)
