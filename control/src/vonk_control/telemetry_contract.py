"""Shared, bounded telemetry wire and projection models.

The agent reports observations over its authenticated channel.  The
Controller adds node and receive identity when it persists the report.  A
series is deliberately self describing so a consumer never has to infer a
unit, source, or whether a value is measured from a metric name.
"""

from __future__ import annotations

from vonk_agent_protocol.telemetry import (
    TelemetryCapability,
    TelemetryDetails,
    TelemetryFreshness,
    TelemetryMeasurementKind,
    TelemetryMetrics,
    TelemetryProvenance,
    TelemetryReport,
    TelemetryRequest,
    TelemetryRuntime,
    TelemetrySample,
    TelemetryScope,
    TelemetrySeries,
    TelemetrySupport,
    TelemetryWorkload,
    TelemetryWorkloadState,
)
from vonk_agent_protocol.wire_model import WireModel

# Compatibility name for projection consumers. This is an alias, so the
# Controller cannot accidentally create a second wire model hierarchy.
TelemetryContractModel = WireModel

__all__ = [
    "TelemetryCapability",
    "TelemetryContractModel",
    "TelemetryDetails",
    "TelemetryFreshness",
    "TelemetryMeasurementKind",
    "TelemetryMetrics",
    "TelemetryProvenance",
    "TelemetryReport",
    "TelemetryRequest",
    "TelemetryRuntime",
    "TelemetrySample",
    "TelemetryScope",
    "TelemetrySeries",
    "TelemetrySupport",
    "TelemetryWorkload",
    "TelemetryWorkloadState",
]
