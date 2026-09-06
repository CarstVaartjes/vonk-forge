from typing import Literal, cast

TelemetryCapabilityMeasurementKind = Literal['configured', 'derived', 'estimated', 'measured']

TELEMETRY_CAPABILITY_MEASUREMENT_KIND_VALUES: set[TelemetryCapabilityMeasurementKind] = { 'configured', 'derived', 'estimated', 'measured',  }

def check_telemetry_capability_measurement_kind(value: str) -> TelemetryCapabilityMeasurementKind:
    if value in TELEMETRY_CAPABILITY_MEASUREMENT_KIND_VALUES:
        return cast(TelemetryCapabilityMeasurementKind, value)
    raise TypeError(f"Unexpected value {value!r}. Expected one of {TELEMETRY_CAPABILITY_MEASUREMENT_KIND_VALUES!r}")
