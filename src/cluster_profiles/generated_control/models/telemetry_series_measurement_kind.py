from typing import Literal

TelemetrySeriesMeasurementKind = Literal['configured', 'derived', 'estimated', 'measured']

TELEMETRY_SERIES_MEASUREMENT_KIND_VALUES: set[TelemetrySeriesMeasurementKind] = { 'configured', 'derived', 'estimated', 'measured',  }

def check_telemetry_series_measurement_kind(value: str) -> TelemetrySeriesMeasurementKind:
    if value in TELEMETRY_SERIES_MEASUREMENT_KIND_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {TELEMETRY_SERIES_MEASUREMENT_KIND_VALUES!r}")
