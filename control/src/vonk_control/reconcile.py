"""Canonical immutable plan construction shared by current package flows."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from types import MappingProxyType
from typing import Any

from .git_policy import GitPolicy
from .orchestration import OperationGraph
from .proposals import ProposalService


class ChangeService:
    """Submit an already-previewed repository change through Git policy."""

    def __init__(self, proposals: ProposalService, policy: GitPolicy) -> None:
        self._proposals = proposals
        self._policy = policy

    def submit(self, digest: str, actor: str, request_id: str) -> dict[str, object]:
        preview = self._proposals.apply(digest)
        return asdict(self._policy.submit(preview, actor=actor, request_id=request_id))


@dataclass(frozen=True)
class ReconciliationPlan:
    """Digest-bound operation graph used by package rollout internals."""

    commit: str
    targets: tuple[str, ...]
    placements: Mapping[str, object]
    routes: Mapping[str, object]
    releases: Mapping[str, object]
    workload_groups: Mapping[str, object]
    input_digests: Mapping[str, str]
    fleet_evidence_digest: str | None
    digest: str
    operation_graph: OperationGraph | None = None
    operation_payloads: Mapping[str, Mapping[str, object]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    agent_protocol_range: tuple[int, int] | None = None


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    frozen: dict[str, Any] = {}
    for key, item in sorted(value.items()):
        if isinstance(item, Mapping):
            frozen[key] = _freeze_mapping(cast_mapping(item))
        elif isinstance(item, list):
            frozen[key] = tuple(item)
        else:
            frozen[key] = item
    return MappingProxyType(frozen)


def cast_mapping(value: Mapping[object, object]) -> Mapping[str, Any]:
    if not all(isinstance(key, str) for key in value):
        raise TypeError("reconciliation mapping keys must be strings")
    return {str(key): item for key, item in value.items()}


def _plan_content(commit: str, values: Mapping[str, object]) -> tuple[dict[str, object], bytes]:
    targets = values.get("targets")
    if not isinstance(targets, list) or not targets or not all(
        isinstance(target, str) and target.strip() for target in targets
    ):
        raise ValueError("reconciliation definitions require nonempty string targets")
    ordered_targets = sorted(set(targets))
    if len(ordered_targets) != len(targets):
        raise ValueError("reconciliation targets must be unique")
    content: dict[str, object] = {
        "commit": commit,
        "targets": ordered_targets,
        "placements": values.get("placements", {}),
        "routes": values.get("routes", {}),
        "releases": values.get("releases", {}),
        "workload_groups": values.get("workload_groups", {}),
        "input_digests": values.get("input_digests", {}),
    }
    for field_name in (
        "placements",
        "routes",
        "releases",
        "workload_groups",
        "input_digests",
    ):
        if not isinstance(content[field_name], Mapping):
            raise TypeError(f"reconciliation {field_name} must be a mapping")
    if "fleet_evidence_digest" in values:
        evidence_digest = values["fleet_evidence_digest"]
        if (
            not isinstance(evidence_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", evidence_digest) is None
        ):
            raise ValueError("reconciliation fleet evidence digest is invalid")
        content["fleet_evidence_digest"] = evidence_digest
    graph = values.get("operation_graph")
    if graph is not None:
        if not isinstance(graph, OperationGraph) or graph.base_commit != commit:
            raise ValueError("reconciliation operation graph is invalid")
        payloads = values.get("operation_payloads")
        protocol_range = values.get("agent_protocol_range")
        if not isinstance(payloads, Mapping):
            raise TypeError("reconciliation operation payloads must be a mapping")
        if (
            not isinstance(protocol_range, tuple)
            or len(protocol_range) != 2
            or not all(isinstance(item, int) for item in protocol_range)
            or protocol_range[0] < 1
            or protocol_range[0] > protocol_range[1]
        ):
            raise ValueError("reconciliation agent protocol range is invalid")
        content["operation_graph"] = graph.document
        content["operation_payloads"] = payloads
        content["agent_protocol_range"] = list(protocol_range)
    encoded = json.dumps(
        _jsonable(content), sort_keys=True, separators=(",", ":")
    ).encode()
    return content, encoded


def resolved_reconciliation_plan(
    *,
    commit: str,
    targets: tuple[str, ...],
    placements: Mapping[str, object],
    routes: Mapping[str, object],
    releases: Mapping[str, object],
    workload_groups: Mapping[str, object],
    input_digests: Mapping[str, str],
    operation_graph: OperationGraph,
    operation_payloads: Mapping[str, Mapping[str, object]],
    agent_protocol_range: tuple[int, int],
    fleet_evidence_digest: str | None = None,
) -> ReconciliationPlan:
    values: dict[str, object] = {
        "targets": list(targets),
        "placements": placements,
        "routes": routes,
        "releases": releases,
        "workload_groups": workload_groups,
        "input_digests": input_digests,
        "operation_graph": operation_graph,
        "operation_payloads": operation_payloads,
        "agent_protocol_range": agent_protocol_range,
    }
    if fleet_evidence_digest is not None:
        values["fleet_evidence_digest"] = fleet_evidence_digest
    _, encoded = _plan_content(commit, values)
    return ReconciliationPlan(
        commit=commit,
        targets=tuple(sorted(targets)),
        placements=_freeze_mapping(cast_mapping(placements)),
        routes=_freeze_mapping(cast_mapping(routes)),
        releases=_freeze_mapping(cast_mapping(releases)),
        workload_groups=_freeze_mapping(cast_mapping(workload_groups)),
        input_digests=MappingProxyType(dict(sorted(input_digests.items()))),
        fleet_evidence_digest=fleet_evidence_digest,
        digest=hashlib.sha256(encoded).hexdigest(),
        operation_graph=operation_graph,
        operation_payloads=_freeze_mapping(cast_mapping(operation_payloads)),
        agent_protocol_range=agent_protocol_range,
    )


__all__ = ["ChangeService", "ReconciliationPlan", "resolved_reconciliation_plan"]
