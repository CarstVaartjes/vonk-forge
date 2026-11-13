"""Terminal presentation for `vonkctl metrics`.

The words, units and measured-or-estimated basis come from the Controller's
catalogue; this module only formats numbers. The number rules are the ones in
`tests/fixtures/metrics-wording.json`, which the dashboard follows too.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..generated_control.models.metric_spec import MetricSpec
from ..generated_control.models.metrics_series_response import MetricsSeriesResponse
from .common import _table

NO_DATA = "No data in this range"

_RANGE_WORDS = {
    "1h": "last hour",
    "6h": "last 6 hours",
    "24h": "last 24 hours",
    "7d": "last 7 days",
    "30d": "last 30 days",
}
_BINARY = (("TiB", 1 << 40), ("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10))
_BLOCKS = "▁▂▃▄▅▆▇█"
_TREND_CELLS = 8


def _size(value: float) -> str:
    for name, divisor in _BINARY:
        if value >= divisor:
            return f"{value / divisor:.1f} {name}"
    return f"{value:.0f} B"


def _short(value: float) -> str:
    """Two decimals below 10, one below 100, whole above; no trailing zeros."""
    magnitude = abs(value)
    text = (
        f"{value:.2f}"
        if magnitude < 10
        else f"{value:.1f}"
        if magnitude < 100
        else f"{value:.0f}"
    )
    return text.rstrip("0").rstrip(".") if "." in text else text


def metric_value_text(unit: str, value: float | None) -> str:
    """One number in its unit, by the shared wording rules; `None` is no data."""
    if value is None:
        return NO_DATA
    match unit:
        case "tokens_per_second":
            return f"{value:.1f} tok/s" if abs(value) < 100 else f"{value:.0f} tok/s"
        case "seconds":
            return f"{value * 1000:.0f} ms" if abs(value) < 1 else f"{value:.2f} s"
        case "watts":
            return f"{value:.0f} W"
        case "watt_hours":
            return (
                f"{value / 1000:.2f} kWh" if abs(value) >= 1000 else f"{value:.0f} Wh"
            )
        case "bytes":
            return _size(value)
        case "bytes_per_second":
            return f"{_size(value)}/s"
        case "celsius":
            return f"{value:.0f} °C"
        case "rpm":
            return f"{value:.0f} rpm"
        case "megahertz":
            return f"{value:.0f} MHz"
        case "percent":
            return f"{value:.1f}%"
        case "ratio":
            return f"{value * 100:.1f}%"
        case "per_second":
            return f"{_short(value)}/s"
        case "count":
            return _short(value)
        case _:
            return f"{value:g} {unit}"


def sparkline(values: Sequence[float | None]) -> str:
    """Eight block cells over the range; a stretch with no data is a blank cell."""
    count = len(values)
    if not count:
        return ""
    cells: list[float | None] = []
    for index in range(_TREND_CELLS):
        chunk = [
            value
            for value in values[
                index * count // _TREND_CELLS : (index + 1) * count // _TREND_CELLS
            ]
            if value is not None
        ]
        cells.append(sum(chunk) / len(chunk) if chunk else None)
    known = [cell for cell in cells if cell is not None]
    low, high = (min(known), max(known)) if known else (0.0, 0.0)
    return "".join(
        " "
        if cell is None
        else _BLOCKS[3]
        if high == low
        else _BLOCKS[round((cell - low) / (high - low) * (len(_BLOCKS) - 1))]
        for cell in cells
    )


def _metric_list(specs: Sequence[MetricSpec]) -> None:
    _table(
        ("METRIC", "TITLE", "UNIT", "SCOPE", "BASIS"),
        [
            (spec.metric, spec.title, spec.unit, spec.scope, spec.basis)
            for spec in specs
        ],
    )
    print("Show one with: vonkctl metrics show METRIC [--range RANGE] [--spark SPARK]")


def _metric_series(response: MetricsSeriesResponse, names: Mapping[str, str]) -> None:
    spec = response.spec
    estimate = " (estimate)" if spec.basis == "estimated" else ""
    print(
        f"{spec.title}, {_RANGE_WORDS.get(response.range_, response.range_)}{estimate}"
    )
    print(spec.hint)
    if not response.series:
        print(NO_DATA)
        return
    rows = []
    for item in response.series:
        # A series has one label (a Spark or a model); the fleet line has none.
        name = next(iter(item.labels.additional_properties.values()), None)
        label = "Fleet" if name is None else names.get(name, name)
        summary = item.summary
        found = (summary.latest, summary.average, summary.peak, summary.total)
        if all(value is None for value in found):
            rows.append((label, NO_DATA, "", "", "", ""))
            continue
        cells = [
            "-" if value is None else metric_value_text(spec.unit, value)
            for value in found
        ]
        trend = sparkline([point.value for point in item.points])
        rows.append((label, *cells, f"{trend}{estimate}"))
    headers = ["SERIES", "NOW", "AVERAGE", "PEAK", "TOTAL", "TREND"]
    if not spec.summary_total:
        headers.pop(4)
        rows = [(*row[:4], row[5]) for row in rows]
    _table(headers, rows)
