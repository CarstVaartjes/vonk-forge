"""Agent operation kinds and the result bodies the outcome envelope shares."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .failure_evidence import FailureDiagnostics
from .lifecycle_vocabulary import WaitReason
from .optional_evidence import EvidenceDrop
from .package_upgrade import PackageActivationReceipt
from .wire_model import DigestText, ErrorCode, WireEnum, WireModel


class AgentOperation(StrEnum):
    RUNTIME_PREFLIGHT = "runtime.preflight.v1"
    AGENT_UPGRADE = "agent.upgrade.v1"
    ARTIFACT_DISTRIBUTION = "artifact.distribution.v1"
    RECIPE_BUILD = "recipe.build.v1"
    RECIPE_BUILD_CLEANUP = "recipe.build.cleanup.v1"
    RECIPE_INSTALL = "recipe.install"
    RECIPE_START = "recipe.start"
    RECIPE_JOB_RUN = "recipe.job.run.v1"
    RECIPE_STOP = "recipe.stop"
    RECIPE_UNINSTALL = "recipe.uninstall"
    RECIPE_RECONCILE = "recipe.reconcile"


class AgentInstallResult(WireModel):
    installed_bytes: int = Field(strict=True, ge=0, le=16 * 1024**4)


class AgentUpgradeStatus(WireEnum):
    UPGRADED = "upgraded"


class AgentUpgradeResult(WireModel):
    """Evidence emitted after the Rust agent reports an exact upgrade."""

    architecture: Literal["linux-arm64"]
    binary_digest: DigestText
    build_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    package_sha256: DigestText
    package_version: Annotated[
        str, Field(pattern=r"^[0-9A-Za-z][0-9A-Za-z.+~-]{0,127}$")
    ]
    status: AgentUpgradeStatus
    activation_receipt: PackageActivationReceipt


class RecipeStartResult(WireModel):
    """The serving rank reports its endpoint; every other rank reports ``{}``."""

    endpoint: str | None = None


class ArtifactDistributionResult(WireModel):
    downloaded_bytes: int = Field(strict=True, ge=0, le=16 * 1024**4)


class AgentFailureKind(StrEnum):
    TEMPORARY_DEPENDENCY = "temporary-dependency"
    UNCERTAIN_EFFECT = "uncertain-effect"
    INVALID_AUTHORITY = "invalid-authority"
    INVALID_CONTRACT = "invalid-contract"
    INTEGRITY_FAILURE = "integrity-failure"
    RESOURCE_PREREQUISITE = "resource-prerequisite"


class AgentFailureResult(WireModel):
    failure_kind: AgentFailureKind | None = None
    retry_after_seconds: int | None = Field(
        default=None, strict=True, ge=0, le=2**32 - 1
    )
    diagnostics: FailureDiagnostics | None = None
    package_activation: PackageActivationReceipt | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=1024)
    error_code: ErrorCode | None = None
    summary: str | None = Field(default=None, min_length=1, max_length=1024)
    uncertain: bool | None = None
    recovery: str | None = Field(default=None, min_length=1, max_length=128)
    wait_reason: WaitReason | None = None
    status: Literal["failed"] | None = None
    operation: AgentOperation | None = None
    stage: str | None = Field(default=None, min_length=1, max_length=128)
    diagnostic: str | None = Field(default=None, min_length=1, max_length=512)
    helper_error_code: str | None = Field(default=None, min_length=1, max_length=128)
    helper_exit_code: int | None = Field(default=None, strict=True, ge=0, le=255)
    #: The optional evidence the Controller dropped from the agent's report,
    #: each with the field that refused it: a lost group is never silent.
    dropped_evidence: list[EvidenceDrop] | None = Field(default=None, max_length=8)

    @model_validator(mode="after")
    def requires_failure_identity(self) -> AgentFailureResult:
        if self.reason is None and self.error_code is None:
            raise ValueError("failure result requires reason or error_code")
        return self
