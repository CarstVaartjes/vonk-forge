"""The NVIDIA clock event (throttle) reasons in words, one table shared with the Controller web.

The vectors are ``tests/fixtures/fleet-wording-throttle.json``; the web renders the same words.
"""

from __future__ import annotations

# NVML nvmlClocksEventReason* bits (nvml.h), in bit order.
_REASON_WORDS = (
    (0x1, "GPU idle"),
    (0x2, "application clocks setting"),
    (0x4, "software power cap"),
    (0x8, "hardware slowdown"),
    (0x10, "sync boost"),
    (0x20, "software thermal slowdown"),
    (0x40, "hardware thermal slowdown"),
    (0x80, "hardware power brake slowdown"),
    (0x100, "display clock setting"),
)
_KNOWN = sum(bit for bit, _ in _REASON_WORDS)


def throttle_words(reasons: int) -> str:
    """Zero says none active; known bits in bit order; any bit outside the table adds ``state unknown``."""
    if reasons == 0:
        return "none active"
    words = [word for bit, word in _REASON_WORDS if reasons & bit]
    if reasons & ~_KNOWN:
        words.append("state unknown")
    return ", ".join(words)
