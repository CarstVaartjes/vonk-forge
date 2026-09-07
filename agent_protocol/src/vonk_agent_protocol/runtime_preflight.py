"""Current recipe runtime preflight wire contract; no raw host diagnostics."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .wire_model import WireModel


class RuntimePreflightRequest(WireModel):
    schema_version: Literal[1]
    architecture: Literal["linux-arm64", "linux-amd64"]
    source_build: bool
    minimum_free_bytes: int = Field(ge=0, le=2**64 - 1)
    fabric_connectivity: Literal["none", "connected", "full_mesh", "switch"]
    fabric_minimum_mbps: int = Field(ge=0, le=2**64 - 1)
    mandatory_capabilities: list[
        Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,95}$")]
    ] = Field(max_length=64)

    @model_validator(mode="after")
    def unique_requirements(self) -> RuntimePreflightRequest:
        if len(set(self.mandatory_capabilities)) != len(self.mandatory_capabilities):
            raise ValueError("mandatory preflight capabilities must be unique")
        if any(not value or len(value) > 96 for value in self.mandatory_capabilities):
            raise ValueError("invalid mandatory preflight capability")
        return self


class RuntimePreflightFinding(WireModel):
    capability: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,95}$")
    status: Literal["passed", "failed", "unknown"]
    code: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,95}$")


class RuntimePreflightResult(WireModel):
    schema_version: Literal[1]
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: int = Field(ge=0, le=2**64 - 1)
    duration_ms: int = Field(ge=0, lt=60000)
    cached: bool
    findings: list[RuntimePreflightFinding] = Field(max_length=96)

    @model_validator(mode="after")
    def unique_findings(self) -> RuntimePreflightResult:
        if len({item.capability for item in self.findings}) != len(self.findings):
            raise ValueError("preflight capability findings must be unique")
        return self
