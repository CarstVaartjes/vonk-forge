from __future__ import annotations

from collections.abc import Callable

import pytest
from vonk_control.distributed_lifecycle import (
    DistributedLifecycleError,
    recover_distributed_runtime,
)


def _policy() -> dict[str, object]:
    return {
        "stop_timeout_seconds": 30,
        "readiness": {
            "strategy": "endpoint-owner-after-all-ranks",
            "path": "/v1/models",
            "timeout_seconds": 20,
        },
        "failure": {
            "rank_loss": "withdraw-endpoint",
            "recovery": "restart-worker-then-entrypoint",
        },
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
        lifecycle=_policy(),
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
            lifecycle=_policy(),
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
        lifecycle=_policy(),
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
            lambda policy: policy["failure"].update(rank_loss="ignore"),
            "rank-loss policy",
        ),
        (
            lambda policy: policy["failure"].update(recovery="restart-entrypoint"),
            "recovery policy",
        ),
        (
            lambda policy: policy["readiness"].update(strategy="endpoint-owner"),
            "readiness policy",
        ),
    ],
)
def test_recovery_refuses_unbound_lifecycle_metadata(
    mutation: Callable[[dict[str, object]], None], message: str
) -> None:
    policy = _policy()
    mutation(policy)

    with pytest.raises(DistributedLifecycleError, match=message):
        recover_distributed_runtime(
            lifecycle=policy,
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
