"""Terminal presentation for progress responses."""

from __future__ import annotations

from collections.abc import Mapping

from .common import _bytes, _optional, _text


def progress_line(observed: Mapping[str, object]) -> str:
    """Describe measured work, and what it waits for when it is waiting."""
    line = _measured_progress(observed)
    residue = _optional(observed.get("residue"), "evidence residue")
    if residue:
        return f"{line} | evidence unavailable: {_text(residue.get('reason'))}"
    blockers = observed.get("blockers")
    first = blockers[0] if isinstance(blockers, list) and blockers else None
    if isinstance(first, Mapping):
        return f"{line} (waiting: {_text(first.get('code'))})"
    return line


def _measured_progress(observed: Mapping[str, object]) -> str:
    """Describe measured work; a missing or explicitly unknown total stays unknown."""
    progress = _optional(observed.get("progress"), "progress")
    nested = progress.get("operation")
    if isinstance(nested, Mapping):
        progress = nested
    phase = _text(
        progress.get("phase", observed.get("phase", observed.get("state", "working")))
    )
    completed = progress.get("completed_bytes")
    if type(completed) is int:
        total = progress.get("total_bytes")
        if type(total) is int and progress.get("total_bytes_known") is not False:
            percent = f" ({completed * 100 // total}%)" if total > 0 else ""
            return f"{phase} | {_bytes(completed)} / {_bytes(total)}{percent}"
        return f"{phase} | {_bytes(completed)}; total unknown"
    if progress.get("current_label") is not None:
        return f"{phase} | {_text(progress['current_label'])}"
    if progress.get("completed_items") is not None:
        return f"{phase} | items {_text(progress['completed_items'])} / {_text(progress.get('total_items'))}"
    return phase + " | progress unavailable"
