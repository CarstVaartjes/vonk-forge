"""Resource planning: reasons."""

from __future__ import annotations

from typing import Literal

from .types import ResourceReason


def _same_memory_kind(left: str, right: str) -> bool:
    return (
        {left, right} <= {"unified", "unified-memory"}
        or {left, right} <= {"host", "host-memory"}
        or {left, right} <= {"accelerator", "gpu-memory"}
    )


def _reason(
    code: str,
    detail: str,
    *,
    severity: Literal["blocker", "warning"] = "blocker",
    node_id: str | None = None,
) -> ResourceReason:
    return ResourceReason(code, detail, severity=severity, node_id=node_id)
