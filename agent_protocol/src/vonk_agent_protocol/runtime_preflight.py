"""Current recipe runtime preflight wire contract; no raw host diagnostics."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .wire_model import WireModel


class RuntimePreflightRequest(WireModel):
    """Check this linux/arm64 host can run one recipe runtime."""

    source_build: bool
    minimum_free_bytes: int = Field(ge=0, le=2**64 - 1)
    fabric_connectivity: Literal["none", "connected"]
    fabric_minimum_mbps: int = Field(ge=0, le=2**64 - 1)


class RuntimePreflightFinding(WireModel):
    capability: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,95}$")
    status: Literal["passed", "failed", "unknown"]
    code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,95}$")


class RuntimePreflightResult(WireModel):
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: int = Field(ge=0, le=2**64 - 1)
    findings: list[RuntimePreflightFinding] = Field(max_length=96)

    @model_validator(mode="after")
    def unique_findings(self) -> RuntimePreflightResult:
        if len({item.capability for item in self.findings}) != len(self.findings):
            raise ValueError("preflight capability findings must be unique")
        return self
