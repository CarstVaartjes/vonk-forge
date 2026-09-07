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
    TelemetryRequest,
    TelemetryRuntime,
    TelemetrySample,
    TelemetryScope,
    TelemetrySeries,
    TelemetrySupport,
    TelemetryWorkload,
    TelemetryWorkloadState,
)
__all__ = [
    "TelemetryCapability",
    "TelemetryDetails",
    "TelemetryFreshness",
    "TelemetryMeasurementKind",
    "TelemetryMetrics",
    "TelemetryProvenance",
    "TelemetryRequest",
    "TelemetryRuntime",
    "TelemetrySample",
    "TelemetryScope",
    "TelemetrySeries",
    "TelemetrySupport",
    "TelemetryWorkload",
    "TelemetryWorkloadState",
]
