"""Round-trip real Controller producer documents through generated clients."""

from __future__ import annotations

import json


def test_generated_telemetry_models_consume_current_pydantic_documents() -> None:
    from vonk_control.fleet_projection import (
        TelemetryPoint as PointProducer,
    )

    from cluster_profiles.generated_control.models.telemetry_point import TelemetryPoint

    producer = PointProducer.model_validate_json(
        json.dumps(
            {
                "id": "00000000-0000-4000-8000-000000000001",
                "node_id": "spk_" + "1" * 32,
                "boot_id": "00000000-0000-4000-8000-000000000002",
                "observed_at": "2026-09-05T00:00:00Z",
                "received_at": "2026-09-05T00:00:01Z",
                "gpu_utilization_percent": 75.0,
            }
        )
    )
    point = TelemetryPoint.from_dict(json.loads(producer.model_dump_json()))
    assert point.gpu_utilization_percent == 75.0
