"""Complete current installer publication graphs, shared by publisher and setup."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, RootModel

from .wire_model import WireModel

InstallerDigest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
InstallerImage = Annotated[
    str,
    Field(
        pattern=r"^[a-z0-9][a-z0-9./_-]*:[A-Za-z0-9][A-Za-z0-9._-]*@sha256:[0-9a-f]{64}$"
    ),
]
InstallerVersion = Annotated[
    str, Field(pattern=r"^[0-9A-Za-z](?:[0-9A-Za-z.+:~-]{0,126}[0-9A-Za-z])?$")
]


class InstallerAcceptanceNetworkMode(StrEnum):
    FULL = "full"
    DISABLED = "disabled"


class InstallerNasAcceptanceModes(WireModel):
    """Truthful network proof scope for both candidate NAS lanes."""

    native: InstallerAcceptanceNetworkMode
    docker_29_4_3: InstallerAcceptanceNetworkMode = Field(alias="docker-29.4.3")


class InstallerReleaseObject(WireModel):
    path: str = Field(pattern=r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
    sha256: InstallerDigest
    size: int = Field(ge=1, le=1024 * 1024 * 1024)


class InstallerPackageArtifact(InstallerReleaseObject):
    architecture: Literal["linux-arm64"]
    host_signature: str = Field(pattern=r"^[0-9a-f]{128}$")
    package_version: InstallerVersion
    target_binary_digest: InstallerDigest
    target_build_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class InstallerReleaseImages(WireModel):
    api: InstallerImage
    worker: InstallerImage
    hermes: InstallerImage
    litellm: InstallerImage
    ca: InstallerImage


class InstallerBaselineArtifacts(WireModel):
    agent_package_linux_arm64: InstallerReleaseObject = Field(
        alias="agent-package-linux-arm64"
    )
    spark_setup_linux_arm64: InstallerReleaseObject = Field(
        alias="spark-setup-linux-arm64"
    )
    spark_setup_signature_linux_arm64: InstallerReleaseObject = Field(
        alias="spark-setup-signature-linux-arm64"
    )


class InstallerCandidateArtifacts(WireModel):
    cli_wheel: InstallerReleaseObject = Field(alias="cli-wheel")
    agent_package_linux_arm64: InstallerPackageArtifact = Field(
        alias="agent-package-linux-arm64"
    )
    agent_package_signature_linux_arm64: InstallerReleaseObject = Field(
        alias="agent-package-signature-linux-arm64"
    )
    spark_setup_linux_arm64: InstallerReleaseObject = Field(
        alias="spark-setup-linux-arm64"
    )
    spark_setup_signature_linux_arm64: InstallerReleaseObject = Field(
        alias="spark-setup-signature-linux-arm64"
    )
    nas_payload: InstallerReleaseObject = Field(alias="nas-payload")
    nas_setup_darwin_amd64: InstallerReleaseObject = Field(
        alias="nas-setup-darwin-amd64"
    )
    nas_setup_darwin_arm64: InstallerReleaseObject = Field(
        alias="nas-setup-darwin-arm64"
    )
    nas_setup_linux_amd64: InstallerReleaseObject = Field(alias="nas-setup-linux-amd64")
    nas_setup_linux_arm64: InstallerReleaseObject = Field(alias="nas-setup-linux-arm64")


class InstallerBaselineBootstraps(WireModel):
    spark: InstallerReleaseObject


class InstallerCandidateBootstraps(InstallerBaselineBootstraps):
    nas: InstallerReleaseObject


class InstallerReleaseIdentity(WireModel):
    channel: Literal["dev", "stable"]
    generation: InstallerDigest
    images: InstallerReleaseImages
    schema_version: Literal[2]
    source_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    version: InstallerVersion


class InstallerCandidateRelease(InstallerReleaseIdentity):
    artifacts: InstallerCandidateArtifacts
    bootstraps: InstallerCandidateBootstraps


class InstallerAcceptanceBaselineRelease(InstallerReleaseIdentity):
    acceptance_only: Literal[True]
    artifacts: InstallerBaselineArtifacts
    bootstraps: InstallerBaselineBootstraps


class InstallerReleaseManifest(
    RootModel[InstallerCandidateRelease | InstallerAcceptanceBaselineRelease]
):
    """Candidate and acceptance-only baseline are the two current graph roles."""


class CliWheelProjection(InstallerReleaseObject):
    """Validate wheel identity and bounds while allowing signed descriptor metadata."""

    model_config = ConfigDict(extra="ignore")


class CliReleaseArtifacts(WireModel):
    """Only the artifact consumed by the self-updater; other artifacts may grow."""

    model_config = ConfigDict(extra="ignore")
    cli_wheel: CliWheelProjection = Field(alias="cli-wheel")


class CliReleaseProjection(WireModel):
    """Signed updater ingress, independent of the complete installer graph.

    Future installer schemas remain updateable through this stable projection.
    Ignored fields stay covered by the original bytes' signature and digest.
    """

    model_config = ConfigDict(extra="ignore")
    schema_version: int = Field(ge=2)
    channel: Literal["dev", "stable"]
    generation: InstallerDigest
    version: InstallerVersion
    source_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    artifacts: CliReleaseArtifacts
