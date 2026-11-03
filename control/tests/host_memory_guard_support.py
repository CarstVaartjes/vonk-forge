"""The diagnostics a host-memory-guard kill really carries.

Built from the shared vector the Rust helper is tested against
(``host-memory-guard-summary-v1.json``), with its summary split into facts the
way the agent splits an exit summary (``name=value`` tokens, the facts before
any workload exception), so a test cannot invent a field the agent never writes.
"""

from __future__ import annotations

import json
from importlib.resources import files

from vonk_agent_protocol.failure_evidence import (
    FailureDiagnostics,
    FailureLogTail,
    FailureProperty,
)


def guard_summary_facts() -> list[FailureProperty]:
    vector = json.loads(
        files("vonk_agent_protocol")
        .joinpath("vectors/host-memory-guard-summary-v1.json")
        .read_text(encoding="utf-8")
    )
    return [
        FailureProperty(name=name, value=value)
        for name, _, value in (
            token.partition("=") for token in vector["summary"].split() if "=" in token
        )
    ]


def guard_diagnostics(collected_at: str) -> FailureDiagnostics:
    empty = FailureLogTail(
        text="", truncated=False, dropped_bytes=None, dropped_lines=None
    )
    return FailureDiagnostics(
        collected_at=collected_at,
        phase="workload.host_memory_guard",
        category="capacity",
        stdout=empty,
        stderr=empty,
        versions=[],
        sandbox=[],
        storage=[],
        collector_errors=[],
        preflight=guard_summary_facts(),
    )
