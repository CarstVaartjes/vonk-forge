"""Recipe-aware runtime admission, with explicit unknown evidence.

A receipt is valid by identity, never by age: it answers the exact
requirements it was probed for (``requirements_sha256``) on the exact host it
was probed on (the agent's host fingerprint: boot id, agent build, runtime and
storage policy). The Controller refreshes the current fingerprint on every
claim and inventory report, so a reboot, upgrade or policy change invalidates
the receipt at once; while neither moves, re-probing would only repeat the
same answer. Disk is admitted separately on fresh inventory (install
admission's floor), and fabric or reachability faults surface at start.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from vonk_agent_protocol import RuntimePreflightCode, RuntimePreflightFindingCode
from vonk_agent_protocol.runtime_preflight import (
    RuntimePreflightFinding,
    RuntimePreflightRequest,
)

from .inventory_items_contract import PreflightInventoryItem


@dataclass(frozen=True, slots=True)
class RuntimePreflightBlocker:
    code: RuntimePreflightCode
    detail: str


def request_digest(request: RuntimePreflightRequest) -> str:
    return hashlib.sha256(
        json.dumps(
            request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def recipe_requirements(
    document: Mapping[str, object],
    *,
    source_build: bool,
    minimum_free_bytes: int,
) -> RuntimePreflightRequest:
    """Read the canonical topology; engine arguments are not an allowlist."""
    from .recipe_runtime_specs import recipe_topology

    fabric = recipe_topology(document)
    return RuntimePreflightRequest(
        source_build=source_build,
        minimum_free_bytes=minimum_free_bytes,
        fabric_connectivity=fabric.fabric_connectivity,
        fabric_minimum_mbps=fabric.fabric_minimum_bandwidth_mbps,
    )


def mandatory_capabilities(request: RuntimePreflightRequest) -> tuple[str, ...]:
    capabilities = [
        "architecture",
        "cache_writable",
        "staging_writable",
        "temporary_directory",
        "disk_reserve",
        "controller_reachable",
        "signed_helper_run",
    ]
    if request.source_build:
        capabilities.extend(("runroot_length", "podman_build", "podman_run"))
    if request.fabric_connectivity != "none":
        capabilities.append("fabric")
    return tuple(capabilities)


def _finding_detail(finding: RuntimePreflightFinding) -> str:
    """The finding's code as the contract spells it.

    An older agent reported free text; it is read through the one legacy adapter.
    A word no member spells stays visible as reported rather than disappearing
    behind ``unclassified``.
    """

    adopted = finding.finding_code
    if adopted is RuntimePreflightFindingCode.UNCLASSIFIED and (
        finding.code != adopted.value
    ):
        return f"{adopted.value} ({finding.code})"
    return adopted.value


def admission_blockers(
    request: RuntimePreflightRequest,
    receipt: PreflightInventoryItem | None,
    *,
    current_fingerprint: str | None,
) -> tuple[RuntimePreflightBlocker, ...]:
    if receipt is None:
        return (
            RuntimePreflightBlocker(
                RuntimePreflightCode.REQUIRED,
                "Spark runtime preflight has not completed.",
            ),
        )
    result = receipt.result
    if receipt.requirements_sha256 != request_digest(request):
        return (
            RuntimePreflightBlocker(
                RuntimePreflightCode.REQUIREMENTS_CHANGED,
                "Runtime requirements changed; rerun preflight.",
            ),
        )
    if current_fingerprint is None or result.fingerprint != current_fingerprint:
        return (
            RuntimePreflightBlocker(
                RuntimePreflightCode.HOST_CHANGED,
                "Spark host policy changed or its current fingerprint is unavailable; rerun preflight.",
            ),
        )
    findings = {item.capability: item for item in result.findings}
    blockers = []
    for capability in mandatory_capabilities(request):
        finding = findings.get(capability)
        if finding is None or finding.status == "unknown":
            blockers.append(
                RuntimePreflightBlocker(
                    RuntimePreflightCode.REQUIREMENT_UNKNOWN,
                    f"Mandatory runtime capability {capability} is unknown.",
                )
            )
        elif finding.status == "failed":
            blockers.append(
                RuntimePreflightBlocker(
                    RuntimePreflightCode.CAPABILITY_FAILED,
                    f"Runtime capability {capability} failed: "
                    f"{_finding_detail(finding)}.",
                )
            )
    return tuple(blockers)


def latest_result(
    session, node_id: str, *, requirements_sha256: str | None = None
) -> PreflightInventoryItem | None:
    """Read the canonical reusable item; attempt results remain operation history."""
    from .inventory_items import inventory_items
    from .models import AgentNode

    items = inventory_items(session.get(AgentNode, node_id))
    results = [
        item
        for item in items.preflights
        if requirements_sha256 is None
        or item.requirements_sha256 == requirements_sha256
    ]
    return max(results, key=lambda item: item.result.observed_at, default=None)
