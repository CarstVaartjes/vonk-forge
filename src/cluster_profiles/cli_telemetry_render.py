"""Render independent GPU sensor readings without inventing missing metrics."""


def gpu_readings(value: object, temperature: object, reason: object) -> str:
    readings = []
    if isinstance(value, (int, float)):
        readings.append(f"{round(value)}%")
    if type(temperature) is int:
        readings.append(f"{temperature} °C")
    if readings:
        return ", ".join(readings)
    return f"unavailable ({reason})" if isinstance(reason, str) else "unavailable"


def size(value: object) -> str:
    """A short human size for tables; the exact byte count stays in --json."""
    if type(value) is not int or value < 0:
        return "unavailable"
    for unit, divisor in (("TiB", 1 << 40), ("GiB", 1 << 30), ("MiB", 1 << 20)):
        if value >= divisor:
            return f"{value / divisor:.1f} {unit}"
    return f"{value} B"
