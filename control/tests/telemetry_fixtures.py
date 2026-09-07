from __future__ import annotations

from vonk_agent_protocol.telemetry import TelemetryMetrics, TelemetryProvenance


def telemetry_metrics() -> TelemetryMetrics:
    return TelemetryMetrics(
        series=[],
        capabilities=[],
        runtimes=[],
        workloads=[],
        provenance=TelemetryProvenance(collector="test", collector_version="1"),
    )


def telemetry_metrics_document() -> dict[str, object]:
    return telemetry_metrics().model_dump(mode="json")
