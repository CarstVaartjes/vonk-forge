"""Immutable publication lookup for an exact installed agent binary."""
from typing import Literal

from pydantic import Field

from .package_upgrade import PackageRollbackSource
from .wire_model import WireModel


class AgentPackageSource(WireModel):
    schema_version: Literal[2]
    architecture: Literal["linux-arm64"]
    build_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    package: PackageRollbackSource
    package_bytes: int = Field(strict=True, ge=1, le=1024**3)
    package_url: str = Field(pattern=r"^https://install\.vonkforge\.ai/[A-Za-z0-9._~!$&'()*+,;=:%/-]{1,1900}/vonk-forge-agent\.deb$")

    def object_key(self, channel: str) -> str:
        if channel not in {"dev", "stable"}:
            raise ValueError("package source channel is invalid")
        return f"artifacts/{channel}/agent-builds/{self.build_digest[7:]}/{self.package.binary_sha256}/package.json"
