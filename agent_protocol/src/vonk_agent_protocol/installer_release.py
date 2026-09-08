"""Complete current installer publication graphs, shared by publisher and setup."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, RootModel

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
