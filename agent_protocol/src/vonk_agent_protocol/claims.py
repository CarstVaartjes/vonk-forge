"""Canonical current-agent identity and work-claim request."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .package_upgrade import PackageActivationReceipt
from .wire_model import Digest, WireModel


class AgentRuntimeIdentity(WireModel):
    architecture: Literal["linux-amd64", "linux-arm64"]
    semantic_version: str = Field(
        pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
    )
    build_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    binary_digest: Digest
    self_test_passed: Literal[True]
    observation_receipt_public_key: Digest
    package_activation: PackageActivationReceipt | None = None


class ClaimRequest(WireModel):
    capabilities: list[str] = Field(max_length=128)
    hostname: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        pattern=(
            r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
            r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
        ),
    )
    lease_seconds: int = Field(ge=1, le=300)
    node_id: str = Field(pattern=r"^spk_[0-9a-f]{32}$")
    protocol_version: Literal[3]
    runtime_identity: AgentRuntimeIdentity
    wait_seconds: int = Field(ge=0, le=60)
