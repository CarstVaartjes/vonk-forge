"""Bounded fail-closed recovery for an exact distributed recipe topology."""

from __future__ import annotations

from collections.abc import Mapping, Sequence


class DistributedLifecycleError(RuntimeError):
    pass


# Health-probe budget. Initial start and durable recovery retain the accepted
# operator startup deadline; this field is not a recipe-authored startup limit.
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


__all__ = [
    "DEFAULT_DISTRIBUTED_READINESS_TIMEOUT_SECONDS",
    "DistributedLifecycleError",
    "canonical_distributed_readiness",
]
