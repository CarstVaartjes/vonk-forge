"""Library placement and lifecycle authority.

This module is the narrow authority boundary for Library actions.  Recipe
orchestration remains in :mod:`recipe_operations`; this adapter makes the
operator-facing invariants explicit: placement is deterministic and digest
bound, lifecycle calls are capability checked, and all mutations are delegated
to the durable PostgreSQL-backed operation service.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from vonk_agent_protocol import canonical_message

from .auth import Actor


class LibraryOperationDenied(PermissionError):
    """The caller does not hold the explicit Library operation capability."""


class LibraryPlacementConflict(ValueError):
    """Placement evidence cannot satisfy the requested topology."""


class LifecycleService(Protocol):
    def preview_install(self, mapping_id: str, recipe_build_id: str) -> Any: ...
    def install(
        self, plan: Any, *, plan_digest: str, actor: str, request_id: str
    ) -> Any: ...
    def preview_run(self, installation_id: str, alias: str) -> Any: ...
    def start(
        self, plan: Any, *, plan_digest: str, actor: str, request_id: str
    ) -> Any: ...
    def preview_stop(self, run_id: str) -> Any: ...
    def stop(
        self, run_id: str, *, plan_digest: str, actor: str, request_id: str
    ) -> Any: ...
    def preview_uninstall(self, installation_id: str) -> Any: ...
    def uninstall(
        self, installation_id: str, *, plan_digest: str, actor: str, request_id: str
    ) -> Any: ...
    def get(self, operation_id: str) -> Any: ...
    def retry(self, operation_id: str, *, actor: str, request_id: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class SparkCapacity:
    node_id: str
    free_bytes: int
    free_memory_bytes: int
    capabilities: tuple[str, ...] = ()
    online: bool = True


@dataclass(frozen=True, slots=True)
class PlacementPreview:
    """Immutable, digest-bound placement evidence shown before confirmation."""

    topology: Mapping[str, object]
    nodes: tuple[str, ...]
    eligible: tuple[str, ...]
    rejected: Mapping[str, str]
    required_bytes: int
    required_memory_bytes: int
    placement_digest: str


@dataclass(frozen=True, slots=True)
class LibraryLifecycleAuthority:
    """Authorization and digest confirmation boundary for Library mutations."""

    service: LifecycleService

    @staticmethod
    def require_operate(
        actor: Actor | str, capabilities: Sequence[str] | None = None
    ) -> None:
        """Require ``library:operate``; administrators are implicitly capable."""
        role = actor.role if isinstance(actor, Actor) else actor
        if role == "administrator":
            return
        if capabilities is None or "library:operate" not in set(capabilities):
            raise LibraryOperationDenied("library:operate capability is required")

    @staticmethod
    def preview_placement(
        topology: Mapping[str, object],
        sparks: Sequence[SparkCapacity],
        *,
        required_bytes: int,
        required_memory_bytes: int,
        required_capabilities: Sequence[str] = (),
    ) -> PlacementPreview:
        if required_bytes < 0 or required_memory_bytes < 0:
            raise LibraryPlacementConflict("resource requirements must be non-negative")
        count = topology.get("node_count")
        if type(count) is not int or count < 1:
            raise LibraryPlacementConflict("topology node_count is invalid")
        required = set(required_capabilities)
        eligible = tuple(
            sorted(
                spark.node_id
                for spark in sparks
                if spark.online
                and spark.free_bytes >= required_bytes
                and spark.free_memory_bytes >= required_memory_bytes
                and required <= set(spark.capabilities)
            )
        )
        rejected = {
            spark.node_id: "offline"
            if not spark.online
            else (
                "missing capability"
                if not required <= set(spark.capabilities)
                else "insufficient capacity"
            )
            for spark in sparks
            if spark.node_id not in eligible
        }
        if len(eligible) < count:
            raise LibraryPlacementConflict(
                "fewer eligible Sparks than topology requires"
            )
        nodes = eligible[:count]
        identity = {
            "schema_version": 1,
            "topology": dict(topology),
            "nodes": list(nodes),
            "required_bytes": required_bytes,
            "required_memory_bytes": required_memory_bytes,
            "required_capabilities": sorted(required),
        }
        digest = hashlib.sha256(canonical_message(identity)).hexdigest()
        return PlacementPreview(
            topology=dict(topology),
            nodes=nodes,
            eligible=eligible,
            rejected=rejected,
            required_bytes=required_bytes,
            required_memory_bytes=required_memory_bytes,
            placement_digest=digest,
        )

    @staticmethod
    def confirm(preview: PlacementPreview, digest: str) -> None:
        if digest != preview.placement_digest:
            raise LibraryPlacementConflict("placement preview digest is stale")

    def install(
        self,
        plan: Any,
        *,
        plan_digest: str,
        actor: Actor,
        request_id: str,
        capabilities: Sequence[str] = (),
    ) -> Any:
        self.require_operate(actor, capabilities)
        return self.service.install(
            plan, plan_digest=plan_digest, actor=actor.subject, request_id=request_id
        )

    def run(
        self,
        plan: Any,
        *,
        plan_digest: str,
        actor: Actor,
        request_id: str,
        capabilities: Sequence[str] = (),
    ) -> Any:
        self.require_operate(actor, capabilities)
        return self.service.start(
            plan, plan_digest=plan_digest, actor=actor.subject, request_id=request_id
        )

    def stop(
        self,
        run_id: str,
        *,
        plan_digest: str,
        actor: Actor,
        request_id: str,
        capabilities: Sequence[str] = (),
    ) -> Any:
        self.require_operate(actor, capabilities)
        return self.service.stop(
            run_id, plan_digest=plan_digest, actor=actor.subject, request_id=request_id
        )

    def uninstall(
        self,
        installation_id: str,
        *,
        plan_digest: str,
        actor: Actor,
        request_id: str,
        capabilities: Sequence[str] = (),
    ) -> Any:
        self.require_operate(actor, capabilities)
        return self.service.uninstall(
            installation_id,
            plan_digest=plan_digest,
            actor=actor.subject,
            request_id=request_id,
        )

    def operation(
        self,
        operation_id: str,
        *,
        actor: Actor | None = None,
        capabilities: Sequence[str] = (),
    ) -> Any:
        if actor is not None:
            self.require_operate(actor, capabilities)
        return self.service.get(operation_id)

    def retry(
        self,
        operation_id: str,
        *,
        actor: Actor,
        request_id: str,
        capabilities: Sequence[str] = (),
    ) -> Any:
        self.require_operate(actor, capabilities)
        return self.service.retry(
            operation_id, actor=actor.subject, request_id=request_id
        )


__all__ = [
    "LibraryLifecycleAuthority",
    "LibraryOperationDenied",
    "LibraryPlacementConflict",
    "PlacementPreview",
    "SparkCapacity",
]
