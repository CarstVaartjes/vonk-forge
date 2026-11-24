"""Agent upgrades: stored."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel

from vonk_agent_protocol.package_source import AgentPackageSource
from vonk_control.agent_upgrades.contract import (
    AgentUpgradePackage,
    AgentUpgradeRolloutResult,
)
from vonk_control.models.jobs import Job
from vonk_control.strict_json import read_stored_model


def _stored_field[T: BaseModel](
    model: type[T], parent: Job, field: str, key: str | None = None
) -> T | None:
    """One document of a stored rollout payload, read on its own.

    A rollout whose payload is damaged in one place (one Spark's rollback
    source) stays usable everywhere else, so each document is read separately
    and a damaged one reads as absent.
    """

    document = (
        parent.payload.get(field) if isinstance(parent.payload, Mapping) else None  # pyright: ignore[reportUnnecessaryIsInstance]  # stored JSON may not have the shape its column type names
    )
    if key is not None:
        document = document.get(key) if isinstance(document, Mapping) else None
    if document is None:
        return None
    try:
        return read_stored_model(model, document)
    except (TypeError, ValueError):
        return None


def _stored_package(parent: Job) -> AgentUpgradePackage | None:
    return _stored_field(AgentUpgradePackage, parent, "package")


def _stored_source(parent: Job, node_id: str) -> AgentPackageSource | None:
    return _stored_field(AgentPackageSource, parent, "sources", node_id)


def _stored_node_order(parent: Job) -> list[str] | None:
    order = (
        parent.payload.get("node_order")
        if isinstance(parent.payload, Mapping)  # pyright: ignore[reportUnnecessaryIsInstance]  # stored JSON may not have the shape its column type names
        else None
    )
    if not isinstance(order, list) or not all(isinstance(node, str) for node in order):
        return None
    return list(order)


def _stored_result(parent: Job) -> AgentUpgradeRolloutResult:
    """What the rollout recorded so far; an unreadable record reads as empty."""

    if parent.result is None:
        return AgentUpgradeRolloutResult()
    try:
        return read_stored_model(AgentUpgradeRolloutResult, parent.result)
    except (TypeError, ValueError):
        return AgentUpgradeRolloutResult()
