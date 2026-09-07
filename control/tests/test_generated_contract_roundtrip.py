"""Round-trip real Controller producer documents through generated clients."""

from __future__ import annotations

import json

import pytest


def test_generated_telemetry_models_consume_current_pydantic_documents() -> None:
    from vonk_control.fleet_projection import (
        TelemetryHistoryResponse as HistoryProducer,
    )
    from vonk_control.fleet_projection import (
        TelemetryPoint as PointProducer,
    )

    from cluster_profiles.generated_control.models.telemetry_history_response import (
        TelemetryHistoryResponse,
    )
    from cluster_profiles.generated_control.models.telemetry_point import TelemetryPoint

    incomplete_document = {
        "id": "00000000-0000-4000-8000-000000000001",
        "node_id": "spk_" + "1" * 32,
        "boot_id": "00000000-0000-4000-8000-000000000002",
        "sequence": 4,
        "observed_at": "2026-09-05T00:00:00Z",
        "received_at": "2026-09-05T00:00:01Z",
        "gap_samples": 0,
        "details": {},
    }
    with pytest.raises(KeyError, match="metrics"):
        TelemetryPoint.from_dict(incomplete_document)

    producer = PointProducer.model_validate_json(
        json.dumps({
            **incomplete_document,
            "metrics": {
                "schema_version": 2,
                "series": [
                    {
                        "aggregation": "instant",
                        "freshness_threshold_seconds": 30,
                        "key": "gpu.utilization_percent",
                        "measurement_kind": "measured",
                        "observed_at": "2026-09-05T00:00:00Z",
                        "scope": "accelerator",
                        "device_id": "0",
                        "source": "fixture",
                        "support_status": "available",
                        "unit": "percent",
                        "value": 75.0,
                    }
                ],
                "capabilities": [],
                "runtimes": [],
                "workloads": [],
                "provenance": {
                    "collector": "fixture",
                    "collector_version": "1",
                },
            },
        })
    )
    rich = TelemetryPoint.from_dict(json.loads(producer.model_dump_json()))
    assert rich.metrics is not None
    assert rich.metrics.schema_version == 2
    assert rich.metrics.series[0].key == "gpu.utilization_percent"

    history_producer = HistoryProducer.model_validate_json(
        json.dumps({
            "schema_version": 1,
            "node_id": rich.node_id,
            "start": "2026-09-05T00:00:00Z",
            "end": "2026-09-05T00:01:00Z",
            "resolution": "raw",
            "maximum_points": 2,
            "points": [rich.to_dict()],
            "metadata": {
                "requested_start": "2026-09-05T00:00:00Z",
                "requested_end": "2026-09-05T00:01:00Z",
                "actual_start": "2026-09-05T00:00:00Z",
                "actual_end": "2026-09-05T00:00:00Z",
                "requested_resolution": "raw",
                "actual_resolution": "raw",
                "point_count": 1,
                "coverage_seconds": 0.0,
                "gap_samples": 0,
                "downsampled": False,
            },
        })
    )
    history = TelemetryHistoryResponse.from_dict(
        json.loads(history_producer.model_dump_json())
    )
    assert history.schema_version == 1
    assert isinstance(history.points[0], TelemetryPoint)
    assert history.points[0].metrics is not None
    assert history.metadata.point_count == 1
    assert HistoryProducer.model_validate_json(json.dumps(history.to_dict())) == history_producer
