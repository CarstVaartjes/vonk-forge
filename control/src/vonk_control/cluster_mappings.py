"""Bind a recipe's exact topology to local GPU node identities and ranks."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from pydantic import ConfigDict, TypeAdapter, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import ClusterMappingCode
from vonk_forge_contracts import RecipeOptionError, read_recipe
from vonk_forge_contracts.recipe import RecipeTopology

from .models import (
    AgentNode,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    NodeInventorySnapshot,
)
from .recipe_runtime_specs import (
    OPTION_CHOICES_KEY,
    RecipeRuntimeSpecError,
    recipe_topology,
    split_option_choices,
)
from .topology import Placement, TopologyError, validate_topology


class ClusterMappingError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(detail)


_MAPPING_PARAMETERS = TypeAdapter(
    dict[str, object], config=ConfigDict(strict=True, allow_inf_nan=False)
)


def validate_mapping_parameters(value: object) -> dict[str, object]:
    """Validate decoded mapping parameters without constraining engine keys."""

    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
        parsed = _MAPPING_PARAMETERS.validate_json(encoded, strict=True)
    except (TypeError, ValueError, ValidationError) as error:
        raise ClusterMappingError(
            ClusterMappingCode.PARAMETERS_INVALID,
            "persisted mapping parameters are invalid",
        ) from error
    return copy.deepcopy(parsed)


def _active_recipe_revision(
    session: Session,
    revision_id: str | None,
    *,
    for_update: bool = False,
) -> CatalogDocumentRevision | None:
    """Load only the active canonical Recipe revision used by a mapping."""

    if not isinstance(revision_id, str) or not revision_id:
        return None
    statement = select(CatalogDocumentRevision).where(
        CatalogDocumentRevision.id == revision_id,
        CatalogDocumentRevision.kind == "recipe",
        CatalogDocumentRevision.state == "active",
    )
    if for_update:
        statement = statement.with_for_update(of=CatalogDocumentRevision)
    return session.scalar(statement)


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


def candidate_placements(
    topology: RecipeTopology, node_ids: tuple[str, ...]
) -> tuple[ClusterMappingPlacement, ...]:
    """Use the mapping authority's deterministic rank and role assignment."""
    if topology.node_count != len(node_ids) or len(set(node_ids)) != len(node_ids):
        raise ClusterMappingError(
            ClusterMappingCode.NODE_COUNT,
            "selected GPU node count does not match the exact topology",
        )
    expanded = [
        (role.name, role.endpoint_owner)
        for role in topology.roles
        for _ in range(role.count)
    ]
    return tuple(
        ClusterMappingPlacement(node_id, rank, role, endpoint_owner)
        for rank, (node_id, (role, endpoint_owner)) in enumerate(
            zip(sorted(node_ids), expanded, strict=True)
        )
    )


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
            revision = _active_recipe_revision(session, recipe_revision_id)
            if revision is None:
                raise KeyError(recipe_revision_id)
            if revision.state != "active" or revision.content_digest is None:
                raise ClusterMappingError(
                    ClusterMappingCode.RECIPE_UNRESOLVED,
                    "only a resolved recipe can be mapped",
                )
            document = copy.deepcopy(revision.document)
            nodes = _active_nodes(session, node_ids)
            capabilities = _topology_capabilities(session, nodes)
        try:
            topology = recipe_topology(document)
        except RecipeRuntimeSpecError as error:
            raise ClusterMappingError(
                ClusterMappingCode.TOPOLOGY_INVALID, str(error)
            ) from error
        effective = _effective_parameters(document, parameters)
        placements = candidate_placements(
            topology, tuple(node.node_id for node in nodes)
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
            revision.content_digest,
            topology.name,
            1,
            effective,
            placements,
        )
        return ClusterMappingPlan(
            recipe_revision_id=revision.id,
            recipe_content_sha256=revision.content_digest,
            topology_name=topology.name,
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
            revision = _active_recipe_revision(
                session, plan.recipe_revision_id, for_update=True
            )
            if (
                revision is None
                or revision.state != "active"
                or revision.content_digest != plan.recipe_content_sha256
            ):
                raise ClusterMappingError(
                    ClusterMappingCode.STALE_PLAN,
                    "recipe changed after mapping preview",
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
            if plan.topology_name != topology.name or plan.placement_digest != _digest(
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
                    ClusterMappingCode.STALE_PLAN, "mapping plan identity is invalid"
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
                    ClusterMappingCode.ENDPOINT_OWNER,
                    "mapping must have one endpoint owner",
                )
            mapping = ClusterMapping(
                recipe_revision_id=plan.recipe_revision_id,
                topology_name=plan.topology_name,
                generation=plan.generation,
                node_count=len(plan.nodes),
                state="ready",
                parameters=validate_mapping_parameters(plan.parameters),
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
            ClusterMappingCode.NODES_INVALID,
            "mapping nodes must be unique and non-empty",
        )
    statement = select(AgentNode).where(AgentNode.node_id.in_(node_ids))
    if lock:
        statement = statement.with_for_update()
    rows = tuple(session.scalars(statement))
    if len(rows) != len(node_ids):
        raise ClusterMappingError(
            ClusterMappingCode.NODE_UNKNOWN, "a selected GPU node is unknown"
        )
    if any(
        row.state != "active"
        or row.revoked_at is not None
        or row.architecture != "linux-arm64"
        for row in rows
    ):
        raise ClusterMappingError(
            ClusterMappingCode.NODE_INCOMPATIBLE,
            "a selected GPU node is inactive or incompatible",
        )
    return rows


def _topology_capabilities(
    session: Session, nodes: tuple[AgentNode, ...]
) -> dict[str, tuple[str, ...]]:
    """Runtime and fabric capabilities from each node's latest inventory."""

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
        inventory = inventories.get(node.node_id)
        result[node.node_id] = (
            () if inventory is None else tuple(sorted(set(inventory.capabilities)))
        )
    return result


def _mapping_actor(actor: str) -> str:
    if not isinstance(actor, str):
        raise ClusterMappingError(ClusterMappingCode.ACTOR, "mapping actor is invalid")
    actor = actor.strip()
    if not actor or len(actor) > 200:
        raise ClusterMappingError(ClusterMappingCode.ACTOR, "mapping actor is invalid")
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


def effective_option_choices(
    document: Mapping[str, object], choices: Mapping[str, str] | None
) -> dict[str, str]:
    """The choice for every option a recipe declares (its default where none
    was given); empty for a recipe without options. An unknown option or value
    raises ``ClusterMappingError`` listing the valid ones."""

    supplied = dict(choices or {})
    if not supplied and not document.get("options"):
        return {}
    try:
        return read_recipe(document).resolve_options(supplied)
    except (RecipeOptionError, ValidationError) as error:
        raise ClusterMappingError(
            ClusterMappingCode.OPTION_INVALID, str(error)
        ) from error


def mapping_option_choices(parameters: Mapping[str, object]) -> dict[str, str]:
    """The option choices a stored mapping carries (none for an older one)."""

    raw = parameters.get(OPTION_CHOICES_KEY)
    return dict(raw) if isinstance(raw, Mapping) else {}


def _effective_parameters(
    document: Mapping[str, object],
    supplied: Mapping[str, object],
) -> dict[str, object]:
    """Setting values plus, when the recipe declares options, the effective
    choice for every option (the recipe default where none was supplied)."""

    try:
        choices, settings = split_option_choices(supplied)
    except RecipeRuntimeSpecError as error:
        raise ClusterMappingError(
            ClusterMappingCode.OPTION_INVALID, str(error)
        ) from error
    resolved = effective_option_choices(document, choices)
    effective = _effective_settings(document, settings)
    if resolved:
        effective[OPTION_CHOICES_KEY] = resolved
    return dict(sorted(effective.items()))


def _effective_settings(
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
                ClusterMappingCode.PARAMETERS_INVALID, "recipe settings are invalid"
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
                ClusterMappingCode.PARAMETER_UNKNOWN,
                "mapping contains an unknown setting",
            )
        effective.update(copy.deepcopy(dict(supplied)))
        return effective
    if not isinstance(raw_parameters, list):
        raise ClusterMappingError(
            ClusterMappingCode.PARAMETERS_INVALID, "recipe parameters are invalid"
        )
    definitions = {
        str(item["name"]): item for item in raw_parameters if isinstance(item, Mapping)
    }
    if set(supplied) - set(definitions):
        raise ClusterMappingError(
            ClusterMappingCode.PARAMETER_UNKNOWN,
            "mapping contains an unknown parameter",
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
                ClusterMappingCode.PARAMETER_TYPE,
                f"parameter {name} has the wrong type",
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
                ClusterMappingCode.PARAMETER_VALUE,
                f"parameter {name} is outside its bounds",
            )
    return dict(sorted(effective.items()))


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
