"""Bounded fail-closed recovery for an exact distributed recipe topology."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any


class DistributedLifecycleError(RuntimeError):
    pass


DEFAULT_DISTRIBUTED_READINESS_TIMEOUT_SECONDS = 60


def canonical_distributed_readiness(
    *,
    topology: Mapping[str, object],
    interfaces: Sequence[object],
    lifecycle: Mapping[str, object],
) -> dict[str, object] | None:
    """Derive collective readiness from the canonical Recipe document.

    Readiness is an execution property of a distributed endpoint.  It is
    therefore derived from the topology owner and the interface health path;
    it is not an authoring field under ``runtime.lifecycle``.
    """

    if topology.get("mode") != "distributed":
        return None
    roles = topology.get("roles")
    if not isinstance(roles, Sequence) or isinstance(roles, (str, bytes)):
        raise DistributedLifecycleError("distributed endpoint topology is invalid")
    owners = tuple(
        role
        for role in roles
        if isinstance(role, Mapping) and role.get("endpoint_owner") is True
    )
    if len(owners) != 1 or owners[0].get("count") != 1:
        raise DistributedLifecycleError("distributed endpoint topology is invalid")
    openai_interfaces = tuple(
        interface
        for interface in interfaces
        if isinstance(interface, Mapping) and interface.get("adapter") == "openai"
    )
    if len(openai_interfaces) > 1:
        raise DistributedLifecycleError("distributed readiness interface is invalid")
    if not openai_interfaces:
        # Job interfaces have filesystem completion semantics and do not
        # expose an HTTP endpoint for collective readiness.
        return None
    interface = openai_interfaces[0]
    path = interface.get("health_path")
    if not isinstance(path, str) or not path.startswith("/"):
        raise DistributedLifecycleError("distributed readiness path is invalid")
    stop_timeout = lifecycle.get("stop_timeout_seconds")
    if type(stop_timeout) is not int or not 1 <= stop_timeout <= 600:
        raise DistributedLifecycleError("distributed readiness timeout is invalid")
    return {
        "strategy": "endpoint-owner-after-all-ranks",
        "path": path,
        "timeout_seconds": DEFAULT_DISTRIBUTED_READINESS_TIMEOUT_SECONDS,
    }


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DistributedLifecycleError(f"distributed {label} policy is invalid")
    return value


def _await_ranks(
    ranks: tuple[int, ...],
    *,
    ready: Callable[[int], bool],
    deadline: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> None:
    while not all(ready(rank) for rank in ranks):
        if monotonic() >= deadline:
            raise DistributedLifecycleError("distributed readiness deadline elapsed")
        sleep(0.1)


def recover_distributed_runtime(
    *,
    lifecycle: Mapping[str, object],
    topology: Mapping[str, object],
    interfaces: Sequence[object],
    roles: Mapping[int, str],
    failed_rank: int,
    withdraw: Callable[[], None],
    stop: Callable[[int], None],
    start: Callable[[int], None],
    ready: Callable[[int], bool],
    publish: Callable[[], None],
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> dict[str, object]:
    """Withdraw, restart in the declared order, and publish after all ranks recover."""

    failure = _mapping(lifecycle.get("failure"), "rank-loss")
    readiness = canonical_distributed_readiness(
        topology=topology,
        interfaces=interfaces,
        lifecycle=lifecycle,
    )
    if readiness is None:
        raise DistributedLifecycleError("distributed readiness policy is unavailable")
    if failure.get("rank_loss") != "withdraw-endpoint":
        raise DistributedLifecycleError("distributed rank-loss policy is invalid")
    if failure.get("recovery") != "restart-worker-then-entrypoint":
        raise DistributedLifecycleError("distributed recovery policy is invalid")
    timeout = readiness.get("timeout_seconds")
    stop_timeout = lifecycle.get("stop_timeout_seconds")
    if (
        type(timeout) is not int
        or not 1 <= timeout <= 3600
        or type(stop_timeout) is not int
        or not 1 <= stop_timeout <= 600
    ):
        raise DistributedLifecycleError("distributed readiness policy is invalid")
    ordered = tuple(sorted(roles))
    if (
        ordered != tuple(range(len(roles)))
        or failed_rank not in roles
        or tuple(rank for rank in ordered if roles[rank] == "entrypoint") != (0,)
    ):
        raise DistributedLifecycleError("distributed rank topology is invalid")
    workers = tuple(rank for rank in ordered if roles[rank] != "entrypoint")
    if not workers:
        raise DistributedLifecycleError("distributed rank topology is invalid")

    withdraw()
    for rank in ordered:
        stop(rank)
    deadline = monotonic() + min(timeout, stop_timeout)
    recovery_order: list[int] = []
    for rank in workers:
        start(rank)
        recovery_order.append(rank)
    _await_ranks(
        workers,
        ready=ready,
        deadline=deadline,
        monotonic=monotonic,
        sleep=sleep,
    )
    start(0)
    recovery_order.append(0)
    _await_ranks(
        ordered,
        ready=ready,
        deadline=deadline,
        monotonic=monotonic,
        sleep=sleep,
    )
    publish()
    return {
        "failed_rank": failed_rank,
        "full_topology_healthy": True,
        "published_after_recovery": True,
        "recovery_order": recovery_order,
        "route_withdrawn": True,
    }


__all__ = [
    "DEFAULT_DISTRIBUTED_READINESS_TIMEOUT_SECONDS",
    "DistributedLifecycleError",
    "canonical_distributed_readiness",
    "recover_distributed_runtime",
]
