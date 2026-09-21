from __future__ import annotations

from vonk_control.distributed_lifecycle import (
    canonical_distributed_readiness,
)


def _policy() -> dict[str, object]:
    return {
        "stop_timeout_seconds": 30,
        "failure": {
            "rank_loss": "withdraw-endpoint",
            "recovery": "restart-worker-then-entrypoint",
        },
    }


def _topology() -> dict[str, object]:
    return {
        "mode": "distributed",
        "roles": [
            {"name": "entrypoint", "count": 1, "endpoint_owner": True},
            {"name": "worker", "count": 1, "endpoint_owner": False},
        ],
    }


def test_readiness_selects_openai_interface_with_job_companion() -> None:
    interfaces = [
        {"adapter": "image-job", "path": "/outputs"},
        {"adapter": "openai", "health_path": "/v1/models"},
    ]

    assert canonical_distributed_readiness(
        topology=_topology(), interfaces=interfaces, lifecycle=_policy()
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
            lifecycle=_policy(),
        )
        is None
    )
