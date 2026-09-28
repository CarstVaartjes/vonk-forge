"""Canonical current-agent identity and work-claim request."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field, model_validator

from .package_upgrade import PackageActivationReceipt
from .wire_model import Digest, WireModel


class AgentRuntimeIdentity(WireModel):
    architecture: Literal["linux-amd64", "linux-arm64"]
    semantic_version: str = Field(
        pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
    )
    build_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    binary_digest: Digest
    observation_receipt_public_key: Digest
    package_activation: PackageActivationReceipt | None = None


#: The single Controller<->agent protocol gate.  A Controller refuses a claim
#: from any other version; the Spark agent is reinstalled in lockstep.
AGENT_PROTOCOL_VERSION = 4


class ClaimRequest(WireModel):
    hostname: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        pattern=(
            r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
            r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
        ),
    )
    #: The fingerprint of the host runtime the last preflight observed, when
    #: the agent has one; it tells the Controller whether that proof is current.
    preflight_fingerprint: Digest | None = None
    protocol_version: int = Field(strict=True, ge=1, le=2**31 - 1)
    runtime_identity: AgentRuntimeIdentity
    wait_seconds: int = Field(ge=0, le=60)

    @model_validator(mode="before")
    @classmethod
    def supported_protocol(cls, value: Any) -> Any:
        # Checked before any field so an older agent gets this one actionable
        # error instead of a list of unknown fields.
        if isinstance(value, Mapping):
            version = value.get("protocol_version")
            if version != AGENT_PROTOCOL_VERSION or isinstance(version, bool):
                raise ValueError(
                    f"agent protocol {version} is not supported by this "
                    "Controller; reinstall the Spark agent"
                )
        return value
