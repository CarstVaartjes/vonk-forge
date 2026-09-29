"""Bounded fail-closed recovery for an exact distributed recipe topology."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from vonk_forge_contracts.recipe import RecipeTopology


class DistributedLifecycleError(RuntimeError):
    pass


# Health-probe budget. Initial start and durable recovery retain the accepted
# operator startup deadline; this field is not a recipe-authored startup limit.
DEFAULT_DISTRIBUTED_READINESS_TIMEOUT_SECONDS = 60


def canonical_distributed_readiness(
    *,
    topology: RecipeTopology,
    interfaces: Sequence[object],
) -> dict[str, object] | None:
    """Derive collective readiness from the canonical Recipe document.

    Readiness is an execution property of a distributed endpoint.  It is
    therefore derived from the topology owner and the interface health path;
    it is not an authoring field under ``runtime.lifecycle``.
    """

    if not topology.distributed:
        return None
    owners = tuple(role for role in topology.roles if role.endpoint_owner)
    if len(owners) != 1 or owners[0].count != 1:
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
    path = openai_interfaces[0].get("health_path")
    if not isinstance(path, str) or not path.startswith("/"):
        raise DistributedLifecycleError("distributed readiness path is invalid")
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
