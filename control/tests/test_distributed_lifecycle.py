from __future__ import annotations

from vonk_control.distributed_lifecycle import (
    canonical_distributed_readiness,
)
from vonk_forge_contracts.recipe import RecipeTopology


def _topology() -> RecipeTopology:
    role = {
        "resources": {
            "memory": {"peak_bytes": 1, "reserve_bytes": 0},
            "disk": {
                "image_bytes": 0,
                "artifact_bytes": 0,
                "working_bytes": 0,
                "safety_margin_bytes": 0,
            },
        }
    }
    return RecipeTopology.model_validate(
        {
            "name": "dual",
            "node_count": 2,
            "roles": [
                {"name": "entrypoint", "count": 1, "endpoint_owner": True, **role},
                {"name": "worker", "count": 1, "endpoint_owner": False, **role},
            ],
            "parallelism": {"tensor": 2, "pipeline": 1, "data": 1, "backend": "ray"},
            "start_order": ["worker", "entrypoint"],
        }
    )


def test_readiness_selects_openai_interface_with_job_companion() -> None:
    interfaces = [
        {"adapter": "image-job", "path": "/outputs"},
        {"adapter": "openai", "health_path": "/v1/models"},
    ]

    assert canonical_distributed_readiness(
        topology=_topology(), interfaces=interfaces
    ) == {
        "strategy": "endpoint-owner-after-all-ranks",
        "path": "/v1/models",
        "timeout_seconds": 60,
    }


def test_job_only_interface_has_no_http_readiness() -> None:
    assert (
        canonical_distributed_readiness(
            topology=_topology(),
            interfaces=[{"adapter": "image-job", "path": "/outputs"}],
        )
        is None
    )
