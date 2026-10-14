"""The CLI's stall window: a local process ends only when its progress stops.

The Controller's ``vonk_control.stall_window`` is the same rule for durable
work; the CLI cannot import it, so it keeps this monotonic twin.
"""

from __future__ import annotations


def stall_deadline(started: float, progressed: float | None, window: float) -> float:
    """The monotonic instant work with no progress since ``progressed`` stalls."""

    return max(started, progressed if progressed is not None else started) + window
