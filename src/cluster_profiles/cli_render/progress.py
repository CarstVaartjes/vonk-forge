"""Terminal presentation for progress responses.

One renderer serves the one-shot view and the live follow view; rate and ETA are
the Controller's own figures, never derived here.
"""

from __future__ import annotations

from collections.abc import Mapping

from .common import _bytes, _optional, _text
from .phase_words import Meter, measured, phase_words, wait_words

_BAR_WIDTH = 10


def progress_line(observed: Mapping[str, object]) -> str:
    """Describe measured work, and what it waits for when it is waiting."""
    line = _measured_progress(observed)
    residue = _optional(observed.get("residue"), "evidence residue")
    if residue:
        return f"{line} | evidence unavailable: {_text(residue.get('reason'))}"
    blockers = observed.get("blockers")
    first = blockers[0] if isinstance(blockers, list) and blockers else None
    if isinstance(first, Mapping):
        behind = first.get("operation")
        nodes = first.get("node_ids")
        if isinstance(behind, Mapping) and isinstance(nodes, list) and nodes:
            return (
                f"{line} (waiting: {_text(behind.get('kind'))} "
                f"{_text(behind.get('id'))[:8]} on {_text(nodes[0])})"
            )
        return f"{line} (waiting for {wait_words(observed, first)})"
    return line


def _measured_progress(observed: Mapping[str, object]) -> str:
    """Describe measured work; a missing or explicitly unknown total stays unknown."""
    meter = measured(observed).overall
    words = phase_words(meter.phase)
    if meter.completed is not None:
        pace = _pace(meter)
        if meter.total is not None:
            percent = (
                f" ({meter.completed * 100 // meter.total}%)" if meter.total > 0 else ""
            )
            return (
                f"{words} | {_bytes(meter.completed)} / {_bytes(meter.total)}"
                f"{percent}{pace}"
            )
        return f"{words} | {_bytes(meter.completed)}; total unknown{pace}"
    if meter.label is not None:
        return f"{words} | {_text(meter.label)}"
    if meter.items is not None:
        return f"{words} | items {_text(meter.items[0])} / {_text(meter.items[1])}"
    return words + " | progress unavailable"


def live_lines(observed: object) -> list[str]:
    """The live view: the overall meter, then one bar per Spark when reported."""
    seen = measured(observed)
    lines = [_bar_line(seen.overall, phase_words(seen.phase))]
    if len(seen.members) > 1:
        lines.extend(
            _bar_line(member, f"  {(member.member_id or 'spark')[:14]}")
            for member in seen.members
        )
    return lines


def phase_key(observed: object) -> str:
    """What identifies the step being shown; a new key starts a new line."""
    return measured(observed).phase


def _bar_line(meter: Meter, title: str) -> str:
    if meter.completed is None:
        return f"{title}  {_text(meter.label)}" if meter.label else title
    if meter.total is None or meter.total <= 0:
        return f"{title}  {_compact(meter.completed)} so far{_pace(meter)}"
    fraction = min(1.0, meter.completed / meter.total)
    filled = int(fraction * _BAR_WIDTH)
    bar = "█" * filled + "░" * (_BAR_WIDTH - filled)
    sizes = _sizes(meter.completed, meter.total)
    return (
        f"{title}  [{bar}] {int(fraction * 100)}% {sizes}{_pace(meter, separator=' ')}"
    )


def _pace(meter: Meter, separator: str = " | ") -> str:
    parts = []
    if meter.rate is not None:
        parts.append(f"{_compact(int(meter.rate))}/s")
    if meter.eta_seconds is not None:
        parts.append(f"~{_duration(meter.eta_seconds)}")
    return separator + " ".join(parts) if parts else ""


_UNITS = (("TiB", 1 << 40), ("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10))


def _unit(reference: int) -> tuple[str, int]:
    return next(((n, d) for n, d in _UNITS if reference >= d), ("B", 1))


def _compact(value: int) -> str:
    name, divisor = _unit(value)
    return f"{value / divisor:.1f} {name}"


def _sizes(done: int, total: int) -> str:
    """``done/total unit`` in the total's unit, so the pair reads at a glance."""
    name, divisor = _unit(total)
    return f"{done / divisor:.1f}/{total / divisor:.1f} {name}"


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{int(seconds)} s"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)} min"
    return f"{int(seconds // 3600)} h {round(seconds % 3600 / 60)} min"
