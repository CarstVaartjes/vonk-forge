"""Bind a recipe's exact topology to local GPU node identities and ranks."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    AgentNode,
    ClusterMapping,
    ClusterMappingNode,
    LocalRecipeRevision,
    NodeInventorySnapshot,
)
from .recipe_contract import RecipeContractError, recipe_topology
from .topology import Placement, TopologyError, validate_topology


class ClusterMappingError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class ClusterMappingPlacement:
    node_id: str
    rank: int
    role: str
    endpoint_owner: bool


@dataclass(frozen=True, slots=True)
class ClusterMappingPlan:
    recipe_revision_id: str
    recipe_content_sha256: str
    topology_name: str
    generation: int
    parameters: dict[str, object]
    nodes: tuple[ClusterMappingPlacement, ...]
    placement_digest: str


class ClusterMappingService:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def preview(
        self,
        recipe_revision_id: str,
        node_ids: tuple[str, ...],
        parameters: Mapping[str, object],
        actor: str,
    ) -> ClusterMappingPlan:
        _mapping_actor(actor)
        with self._sessions() as session:
            revision = session.get(LocalRecipeRevision, recipe_revision_id)
            if revision is None:
                raise KeyError(recipe_revision_id)
            if revision.lifecycle != "resolved" or revision.content_sha256 is None:
                raise ClusterMappingError(
                    "mapping.recipe_unresolved", "only a resolved recipe can be mapped"
                )
            document = copy.deepcopy(revision.document)
            nodes = _active_nodes(session, node_ids)
            capabilities = _topology_capabilities(session, nodes)
        try:
            topology = recipe_topology(document)
        except RecipeContractError as error:
            raise ClusterMappingError("mapping.topology_invalid", str(error)) from error
        expected_count = topology.get("node_count")
        if expected_count != len(nodes):
            raise ClusterMappingError(
                "mapping.node_count",
                "selected GPU node count does not match the exact topology",
            )
        effective = _effective_parameters(document, parameters)
        roles = topology.get("roles")
        if not isinstance(roles, list):
            raise ClusterMappingError(
                "mapping.topology_invalid", "topology roles are invalid"
            )
        expanded: list[tuple[str, bool]] = []
        for raw_role in roles:
            if not isinstance(raw_role, Mapping):
                raise ClusterMappingError(
                    "mapping.topology_invalid", "topology role is invalid"
                )
            expanded.extend(
                (str(raw_role["name"]), bool(raw_role["endpoint_owner"]))
                for _ in range(int(raw_role["count"]))
            )
        ordered_nodes = sorted(node.node_id for node in nodes)
        placements = tuple(
            ClusterMappingPlacement(node_id, rank, role, endpoint_owner)
            for rank, (node_id, (role, endpoint_owner)) in enumerate(
                zip(ordered_nodes, expanded, strict=True)
            )
        )
        try:
            validate_topology(
                document,
                tuple(
                    Placement(
                        item.node_id,
                        item.rank,
                        item.role,
                        item.endpoint_owner,
                    )
                    for item in placements
                ),
                capabilities,
            )
        except TopologyError as error:
            raise ClusterMappingError(error.code, str(error)) from error
        identity = _plan_identity(
            revision.id,
            revision.content_sha256,
            str(topology["name"]),
            1,
            effective,
            placements,
        )
        return ClusterMappingPlan(
            recipe_revision_id=revision.id,
            recipe_content_sha256=revision.content_sha256,
            topology_name=str(topology["name"]),
            generation=1,
            parameters=effective,
            nodes=placements,
            placement_digest=_digest(identity),
        )

    def materialize(
        self, plan: ClusterMappingPlan, *, actor: str, now: datetime
    ) -> str:
        actor = _mapping_actor(actor)
        with self._sessions.begin() as session:
            revision = session.get(
                LocalRecipeRevision, plan.recipe_revision_id, with_for_update=True
            )
            if (
                revision is None
                or revision.lifecycle != "resolved"
                or revision.content_sha256 != plan.recipe_content_sha256
            ):
                raise ClusterMappingError(
                    "mapping.stale_plan", "recipe changed after mapping preview"
                )
            nodes = _active_nodes(
                session, tuple(item.node_id for item in plan.nodes), lock=True
            )
            capabilities = _topology_capabilities(session, nodes)
            document = copy.deepcopy(revision.document)
            try:
                validate_topology(
                    document,
                    tuple(
                        Placement(
                            item.node_id,
                            item.rank,
                            item.role,
                            item.endpoint_owner,
                        )
                        for item in plan.nodes
                    ),
                    capabilities,
                )
            except TopologyError as error:
                raise ClusterMappingError(error.code, str(error)) from error
            topology = recipe_topology(document)
            if plan.topology_name != str(
                topology["name"]
            ) or plan.placement_digest != _digest(
                _plan_identity(
                    plan.recipe_revision_id,
                    plan.recipe_content_sha256,
                    plan.topology_name,
                    plan.generation,
                    plan.parameters,
                    plan.nodes,
                )
            ):
                raise ClusterMappingError(
                    "mapping.stale_plan", "mapping plan identity is invalid"
                )
            existing = session.scalar(
                select(ClusterMapping).where(
                    ClusterMapping.placement_digest == plan.placement_digest
                )
            )
            if existing is not None:
                return existing.id
            endpoint = [item.node_id for item in plan.nodes if item.endpoint_owner]
            if len(endpoint) != 1:
                raise ClusterMappingError(
                    "mapping.endpoint_owner", "mapping must have one endpoint owner"
                )
            mapping = ClusterMapping(
                recipe_revision_id=plan.recipe_revision_id,
                topology_name=plan.topology_name,
                generation=plan.generation,
                node_count=len(plan.nodes),
                state="ready",
                parameters=copy.deepcopy(plan.parameters),
                placement_digest=plan.placement_digest,
                endpoint_owner_node_id=endpoint[0],
                created_by=actor,
                created_at=now,
                updated_at=now,
            )
            session.add(mapping)
            session.flush()
            session.add_all(
                ClusterMappingNode(
                    mapping_id=mapping.id,
                    node_id=item.node_id,
                    rank=item.rank,
                    role=item.role,
                    endpoint_owner=item.endpoint_owner,
                    created_at=now,
                )
                for item in plan.nodes
            )
            mapping_id = mapping.id
        return mapping_id


def _active_nodes(
    session: Session, node_ids: tuple[str, ...], *, lock: bool = False
) -> tuple[AgentNode, ...]:
    if not node_ids or len(node_ids) != len(set(node_ids)):
        raise ClusterMappingError(
            "mapping.nodes_invalid", "mapping nodes must be unique and non-empty"
        )
    statement = select(AgentNode).where(AgentNode.node_id.in_(node_ids))
    if lock:
        statement = statement.with_for_update()
    rows = tuple(session.scalars(statement))
    if len(rows) != len(node_ids):
        raise ClusterMappingError(
            "mapping.node_unknown", "a selected GPU node is unknown"
        )
    if any(
        row.state != "active"
        or row.revoked_at is not None
        or row.architecture != "linux-arm64"
        for row in rows
    ):
        raise ClusterMappingError(
            "mapping.node_incompatible",
            "a selected GPU node is inactive or incompatible",
        )
    return rows


def _topology_capabilities(
    session: Session, nodes: tuple[AgentNode, ...]
) -> dict[str, tuple[str, ...]]:
    """Combine negotiated runtime support with latest inventory fabric evidence.

    ``AgentNode.capabilities`` is refreshed by the claim lane and therefore only
    contains protocol and operation capabilities accepted by the control plane.
    Fabric capabilities are reported independently in authenticated inventory
    snapshots.  Treating the claim row as hardware authority makes every real
    distributed mapping fail even while Library correctly ranks the same nodes
    from inventory evidence.
    """

    node_ids = tuple(node.node_id for node in nodes)
    ranked = (
        select(
            NodeInventorySnapshot.id.label("inventory_id"),
            func.row_number()
            .over(
                partition_by=NodeInventorySnapshot.node_id,
                order_by=(
                    NodeInventorySnapshot.observed_at.desc(),
                    NodeInventorySnapshot.received_at.desc(),
                    NodeInventorySnapshot.id.desc(),
                ),
            )
            .label("inventory_rank"),
        )
        .where(NodeInventorySnapshot.node_id.in_(node_ids))
        .subquery()
    )
    inventories = {
        item.node_id: item
        for item in session.scalars(
            select(NodeInventorySnapshot)
            .join(ranked, ranked.c.inventory_id == NodeInventorySnapshot.id)
            .where(ranked.c.inventory_rank == 1)
        )
    }
    result: dict[str, tuple[str, ...]] = {}
    for node in nodes:
        negotiated = {
            capability
            for capability in node.capabilities
            if not capability.startswith("fabric.")
        }
        inventory = inventories.get(node.node_id)
        if inventory is not None:
            negotiated.update(inventory.capabilities)
        result[node.node_id] = tuple(sorted(negotiated))
    return result


def _mapping_actor(actor: str) -> str:
    if not isinstance(actor, str):
        raise ClusterMappingError("mapping.actor", "mapping actor is invalid")
    actor = actor.strip()
    if not actor or len(actor) > 200:
        raise ClusterMappingError("mapping.actor", "mapping actor is invalid")
    return actor


def _plan_identity(
    recipe_revision_id: str,
    recipe_content_sha256: str,
    topology_name: str,
    generation: int,
    parameters: Mapping[str, object],
    nodes: tuple[ClusterMappingPlacement, ...],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "recipe_revision_id": recipe_revision_id,
        "recipe_content_sha256": recipe_content_sha256,
        "topology_name": topology_name,
        "generation": generation,
        "parameters": dict(parameters),
        "nodes": [
            {
                "node_id": item.node_id,
                "rank": item.rank,
                "role": item.role,
                "endpoint_owner": item.endpoint_owner,
            }
            for item in nodes
        ],
    }


def _effective_parameters(
    document: Mapping[str, object],
    supplied: Mapping[str, object],
) -> dict[str, object]:
    raw_parameters = document.get("parameters")
    if raw_parameters is None:
        # Canonical RecipeDefinition v2 carries launch-affecting settings in
        # ``settings``.  Mapping identity must bind those values directly;
        # the retired schema-one parameter list is not an authority for a
        # canonical recipe.
        raw_settings = document.get("settings")
        if not isinstance(raw_settings, Mapping):
            raise ClusterMappingError(
                "mapping.parameters_invalid", "recipe settings are invalid"
            )
        effective: dict[str, object] = {}
        for name in ("context_tokens", "concurrency", "max_batch_tokens"):
            setting = raw_settings.get(name)
            if isinstance(setting, Mapping) and "value" in setting:
                effective[name] = copy.deepcopy(setting["value"])
        knobs = raw_settings.get("knobs")
        if isinstance(knobs, Mapping):
            for name, setting in knobs.items():
                if isinstance(setting, Mapping) and "value" in setting:
                    effective[str(name)] = copy.deepcopy(setting["value"])
        unknown = set(supplied) - set(effective)
        if unknown:
            raise ClusterMappingError(
                "mapping.parameter_unknown", "mapping contains an unknown setting"
            )
        effective.update(copy.deepcopy(dict(supplied)))
        return effective
    if not isinstance(raw_parameters, list):
        raise ClusterMappingError(
            "mapping.parameters_invalid", "recipe parameters are invalid"
        )
    definitions = {
        str(item["name"]): item for item in raw_parameters if isinstance(item, Mapping)
    }
    if set(supplied) - set(definitions):
        raise ClusterMappingError(
            "mapping.parameter_unknown", "mapping contains an unknown parameter"
        )
    effective = {
        name: copy.deepcopy(definition["default"])
        for name, definition in definitions.items()
    }
    effective.update(copy.deepcopy(dict(supplied)))
    for name, value in effective.items():
        definition = definitions[name]
        kind = definition["type"]
        valid_type = (
            kind == "integer"
            and isinstance(value, int)
            and not isinstance(value, bool)
            or kind == "boolean"
            and isinstance(value, bool)
            or kind in {"string", "enum"}
            and isinstance(value, str)
        )
        if not valid_type:
            raise ClusterMappingError(
                "mapping.parameter_type", f"parameter {name} has the wrong type"
            )
        minimum = definition.get("minimum")
        maximum = definition.get("maximum")
        allowed = definition.get("allowed_values")
        pattern = definition.get("pattern")
        if (
            isinstance(minimum, int)
            and isinstance(value, int)
            and value < minimum
            or isinstance(maximum, int)
            and isinstance(value, int)
            and value > maximum
            or isinstance(allowed, list)
            and value not in allowed
            or isinstance(pattern, str)
            and isinstance(value, str)
            and re.fullmatch(pattern, value) is None
        ):
            raise ClusterMappingError(
                "mapping.parameter_value", f"parameter {name} is outside its bounds"
            )
    return dict(sorted(effective.items()))


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
