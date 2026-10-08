"""Identity helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import (
    TypeGuard,
)

from vonk_agent_protocol import (
    WaitReason,
    canonical_message,
)
from vonk_forge_contracts.recipe import RecipeDefinition

from ..cluster_mappings import (
    ClusterMappingPlacement,
)
from ..recipe_operations import (
    RecipeOperationView,
)
from ..run_switch_contract import (
    SparkGroupNode,
)
from .errors import RunSwitchRetryLater


def _stated_bytes(value: object) -> int | None:
    """A recorded size, or ``None`` when the plan does not state one."""

    return value if type(value) is int and value > 0 else None


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _is_hex_digest(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_oci_digest(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and _is_hex_digest(value[7:])
    )


def _recipe_definition(document: object) -> RecipeDefinition | None:
    try:
        return (
            document
            if isinstance(document, RecipeDefinition)
            else RecipeDefinition.model_validate_json(
                canonical_message(document), strict=True
            )
        )
    except (TypeError, ValueError):
        return None


def _primary_model_digest(document: object) -> str | None:
    recipe = _recipe_definition(document)
    return (
        recipe.models[0].model.content_sha256
        if recipe is not None and recipe.models
        else None
    )


def _normalise_architecture(value: str) -> str:
    return {
        "linux-arm64": "linux/arm64",
        "linux/aarch64": "linux/arm64",
        "aarch64": "linux/arm64",
        "arm64": "linux/arm64",
    }.get(value.lower(), value.lower())


def _node_missing_bytes(
    by_node: Mapping[str, int] | None,
    node_id: str,
    total: int | None,
    target_count: int,
) -> int | None:
    """Bytes missing on ONE node, from per-node evidence only.

    ``total`` is a sum over targets and is never divided: presence is uneven
    in general (one Spark may already hold the model or image).  Without
    per-node evidence only a zero total (all present) or a single target
    (the total is that node's) is exact; otherwise return None, which callers
    treat as "missing here" so distribution copies or verifies it.
    """

    if by_node is not None and node_id in by_node:
        return by_node[node_id]
    if total == 0:
        return 0
    if total is not None and target_count == 1:
        return total
    return None


def _required_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise RunSwitchRetryLater(
            "run-switch persisted identity is invalid",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    return value


def _started_operation_id(value: object) -> str:
    """Read the identity of a started child operation without assuming its class."""

    operation_id = value.id if isinstance(value, RecipeOperationView) else None
    if not operation_id:
        raise RunSwitchRetryLater(
            "run-switch child operation identity is invalid",
            reason=WaitReason.OBSERVATION_UNAVAILABLE,
        )
    return operation_id


def _mapping_node(node: SparkGroupNode) -> ClusterMappingPlacement:
    from ..cluster_mappings import ClusterMappingPlacement

    return ClusterMappingPlacement(
        node_id=node.node_id,
        rank=node.rank,
        role=node.role,
        endpoint_owner=node.endpoint_owner,
    )


_DIGEST = re.compile(r"[0-9a-f]{64}")
