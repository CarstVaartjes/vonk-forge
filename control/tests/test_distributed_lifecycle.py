from __future__ import annotations

from collections.abc import Callable

import pytest
from vonk_control.distributed_lifecycle import (
    DistributedLifecycleError,
    canonical_distributed_readiness,
    recover_distributed_runtime,
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


def _interfaces() -> list[dict[str, object]]:
    return [{"adapter": "openai", "health_path": "/v1/models"}]


def _authority() -> dict[str, object]:
    return {
        "lifecycle": _policy(),
        "topology": _topology(),
        "interfaces": _interfaces(),
    }


def test_rank_loss_withdraws_then_recovers_worker_before_endpoint_owner() -> None:
    events: list[str] = []
    ready = {0: True, 1: False}
    ticks = iter(range(100))

    def start(rank: int) -> None:
        events.append(f"start:{rank}")
        ready[rank] = True

    def stop(rank: int) -> None:
        events.append(f"stop:{rank}")
        ready[rank] = False

    evidence = recover_distributed_runtime(
        **_authority(),
        roles={0: "entrypoint", 1: "worker"},
        failed_rank=1,
        withdraw=lambda: events.append("withdraw"),
        stop=stop,
        start=start,
        ready=lambda rank: ready[rank],
        publish=lambda: events.append("publish"),
        monotonic=lambda: float(next(ticks)),
        sleep=lambda _seconds: None,
    )

    assert events == [
        "withdraw",
        "stop:0",
        "stop:1",
        "start:1",
        "start:0",
        "publish",
    ]
    assert evidence == {
        "failed_rank": 1,
        "full_topology_healthy": True,
        "published_after_recovery": True,
        "recovery_order": [1, 0],
        "route_withdrawn": True,
    }


def test_recovery_never_republishes_when_full_topology_does_not_recover() -> None:
    events: list[str] = []
    now = 0.0

    def monotonic() -> float:
        nonlocal now
        now += 11.0
        return now

    with pytest.raises(DistributedLifecycleError, match="readiness deadline"):
        recover_distributed_runtime(
            **_authority(),
            roles={0: "entrypoint", 1: "worker"},
            failed_rank=1,
            withdraw=lambda: events.append("withdraw"),
            stop=lambda rank: events.append(f"stop:{rank}"),
            start=lambda rank: events.append(f"start:{rank}"),
            ready=lambda _rank: False,
            publish=lambda: events.append("publish"),
            monotonic=monotonic,
            sleep=lambda _seconds: None,
        )

    assert events[:3] == ["withdraw", "stop:0", "stop:1"]
    assert "publish" not in events


def test_recovery_stops_failed_endpoint_owner_only_once() -> None:
    events: list[str] = []
    ready = {0: False, 1: False}

    def start(rank: int) -> None:
        events.append(f"start:{rank}")
        ready[rank] = True

    recover_distributed_runtime(
        **_authority(),
        roles={0: "entrypoint", 1: "worker"},
        failed_rank=0,
        withdraw=lambda: events.append("withdraw"),
        stop=lambda rank: events.append(f"stop:{rank}"),
        start=start,
        ready=ready.__getitem__,
        publish=lambda: events.append("publish"),
        monotonic=lambda: 0.0,
        sleep=lambda _seconds: None,
    )

    assert events == [
        "withdraw",
        "stop:0",
        "stop:1",
        "start:1",
        "start:0",
        "publish",
    ]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda authority: authority["lifecycle"]["failure"].update(
                rank_loss="ignore"
            ),
            "rank-loss policy",
        ),
        (
            lambda authority: authority["lifecycle"]["failure"].update(
                recovery="restart-entrypoint"
            ),
            "recovery policy",
        ),
        (
            lambda authority: authority["topology"]["roles"][0].update(
                endpoint_owner=False
            ),
            "endpoint topology",
        ),
        (
            lambda authority: authority["interfaces"][0].update(
                health_path="models"
            ),
            "readiness path",
        ),
    ],
)
def test_recovery_refuses_unbound_lifecycle_metadata(
    mutation: Callable[[dict[str, object]], None], message: str
) -> None:
    authority = _authority()
    mutation(authority)

    with pytest.raises(DistributedLifecycleError, match=message):
        recover_distributed_runtime(
            **authority,
            roles={0: "entrypoint", 1: "worker"},
            failed_rank=1,
            withdraw=lambda: None,
            stop=lambda _rank: None,
            start=lambda _rank: None,
            ready=lambda _rank: True,
            publish=lambda: None,
            monotonic=lambda: 0.0,
            sleep=lambda _seconds: None,
        )


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
