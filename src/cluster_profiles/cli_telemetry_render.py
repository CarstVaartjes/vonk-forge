"""Render independent GPU sensor readings without inventing missing metrics.

The Fleet words here are shared with the Controller web (`tests/fixtures/fleet-wording-*.json`):
one separator, one "Not reported", and one rounding rule (half up) on both sides.
"""

import math

NOT_REPORTED = "Not reported"


def round_half_up(value: float) -> int:
    """Round .5 up, as the web's Math.round does; Python's round() goes to even."""
    return math.floor(value + 0.5)


def gpu_readings(value: object, temperature: object, reason: object) -> str:
    readings = []
    if isinstance(value, (int, float)):
        readings.append(f"{round_half_up(value)}%")
    if type(temperature) is int:
        readings.append(f"{temperature} °C")
    if readings:
        return ", ".join(readings)
    return f"{NOT_REPORTED} ({reason})" if isinstance(reason, str) else NOT_REPORTED


def size(value: object) -> str:
    """A short human size for tables; the exact byte count stays in --json."""
    if type(value) is not int or value < 0:
        return "unavailable"
    for unit, divisor in (("TiB", 1 << 40), ("GiB", 1 << 30), ("MiB", 1 << 20)):
        if value >= divisor:
            return f"{round_half_up(value / divisor * 10) / 10:.1f} {unit}"
    return f"{value} B"
