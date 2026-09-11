"""Recipe-aware runtime admission, with explicit unknown and stale evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from vonk_agent_protocol.runtime_preflight import (
    RuntimePreflightRequest,
    RuntimePreflightResult,
)


@dataclass(frozen=True, slots=True)
class RuntimePreflightBlocker:
    code: str
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
    architecture: Literal["linux-arm64", "linux-amd64"] = "linux-arm64",
) -> RuntimePreflightRequest:
    """Read the canonical topology; engine arguments are not an allowlist."""
    from .recipe_runtime_specs import recipe_fabric

    fabric = recipe_fabric(document)
    return RuntimePreflightRequest(
        schema_version=1,
        architecture=architecture,
        source_build=source_build,
        minimum_free_bytes=minimum_free_bytes,
        fabric_connectivity=fabric.connectivity,
        fabric_minimum_mbps=fabric.minimum_bandwidth_mbps,
        mandatory_capabilities=[],
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
    return tuple(dict.fromkeys((*capabilities, *request.mandatory_capabilities)))


def admission_blockers(
    request: RuntimePreflightRequest,
    result: RuntimePreflightResult | None,
    *,
    current_fingerprint: str | None,
    now: int,
    maximum_age: int = 300,
) -> tuple[RuntimePreflightBlocker, ...]:
    if result is None:
        return (
            RuntimePreflightBlocker(
                "runtime_preflight.required",
                "Spark runtime preflight has not completed.",
            ),
        )
    if current_fingerprint is None or result.fingerprint != current_fingerprint:
        return (
            RuntimePreflightBlocker(
                "runtime_preflight.host_changed",
                "Spark host policy changed or its current fingerprint is unavailable; rerun preflight.",
            ),
        )
    if result.request_sha256 != request_digest(request):
        return (
            RuntimePreflightBlocker(
                "runtime_preflight.requirements_changed",
                "Preflight does not cover this recipe's exact requirements.",
            ),
        )
    if result.observed_at > now or now - result.observed_at > maximum_age:
        return (
            RuntimePreflightBlocker(
                "runtime_preflight.stale",
                "Spark runtime preflight evidence has expired; rerun preflight.",
            ),
        )
    findings = {item.capability: item for item in result.findings}
    blockers = []
    for capability in mandatory_capabilities(request):
        finding = findings.get(capability)
        if finding is None or finding.status == "unknown":
            blockers.append(
                RuntimePreflightBlocker(
                    "runtime_preflight.requirement_unknown",
                    f"Mandatory runtime capability {capability} is unknown.",
                )
            )
        elif finding.status == "failed":
            blockers.append(
                RuntimePreflightBlocker(
                    f"runtime_preflight.{finding.code}",
                    f"Runtime capability {capability} failed: {finding.code}.",
                )
            )
    return tuple(blockers)


def latest_result(session, node_id: str, *, requirements_sha256: str | None = None) -> RuntimePreflightResult | None:
    """Read validated fenced evidence, including failures, without a new store."""
    from sqlalchemy import select

    from .models import AgentOperation, AgentOperationAttempt

    raw = session.scalar(
        select(AgentOperationAttempt.result)
        .join(AgentOperation, AgentOperation.id == AgentOperationAttempt.operation_id)
        .where(
            AgentOperation.node_id == node_id,
            AgentOperation.kind == "runtime.preflight.v1",
            AgentOperationAttempt.attempt == AgentOperation.current_attempt,
            AgentOperation.state == "succeeded",
            *([AgentOperation.payload_digest == requirements_sha256] if requirements_sha256 is not None else []),
        )
        .order_by(AgentOperation.updated_at.desc(), AgentOperation.id.desc())
        .limit(1)
    )
    if raw is None:
        return None
    # AgentOperationAttempt stores the validated result payload.
    return RuntimePreflightResult.model_validate(raw)


def node_fingerprint(capabilities: list[str]) -> str | None:
    """Current host observation arrives with each authenticated agent claim."""
    import re

    fingerprints = [
        value.removeprefix("runtime.preflight.fingerprint.")
        for value in capabilities
        if value.startswith("runtime.preflight.fingerprint.")
    ]
    if len(fingerprints) == 1 and re.fullmatch(r"[0-9a-f]{64}", fingerprints[0]):
        return fingerprints[0]
    return None
