"""Round-trip real Controller producer documents through generated clients."""

from __future__ import annotations

import json

import pytest


def test_generated_telemetry_models_consume_current_pydantic_documents() -> None:
    from vonk_control.fleet_projection import (
        TelemetryPoint as PointProducer,
    )

    from cluster_profiles.generated_control.models.telemetry_point import TelemetryPoint

    incomplete_document = {
        "id": "00000000-0000-4000-8000-000000000001",
        "node_id": "spk_" + "1" * 32,
        "boot_id": "00000000-0000-4000-8000-000000000002",
        "observed_at": "2026-09-05T00:00:00Z",
        "received_at": "2026-09-05T00:00:01Z",
        "gap_samples": 0,
        "details": {},
    }
    with pytest.raises(KeyError, match="metrics"):
        TelemetryPoint.from_dict(incomplete_document)

    producer = PointProducer.model_validate_json(
        json.dumps(
            {
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
            }
        )
    )
    rich = TelemetryPoint.from_dict(json.loads(producer.model_dump_json()))
    assert rich.metrics is not None
    assert rich.metrics.schema_version == 2
    assert rich.metrics.series[0].key == "gpu.utilization_percent"
