"""The documents of an agent upgrade rollout: package, repair, intent, plan, result.

``jobs.payload`` and ``jobs.result`` of an ``agent-upgrade`` job hold these.
They live apart from :mod:`vonk_control.job_documents` so the operation
projection can read them without importing every job family.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StringConstraints, model_validator
from vonk_agent_protocol.package_source import AgentPackageSource

from .strict_json import StrictJSONModel

UuidText = Annotated[
    str,
    StringConstraints(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
]
NodeText = Annotated[str, StringConstraints(min_length=1, max_length=128)]
DigestText = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class _UpgradeDocument(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


_UPGRADE_URL = (
    r"^https://install\.vonkforge\.ai/[A-Za-z0-9._~!$&'()*+,;=:%/-]{1,1900}"
    r"/vonk-forge-agent\.deb$"
)


class AgentUpgradePackage(_UpgradeDocument):
    """The signed package a rollout installs on every Spark it targets."""

    architecture: Literal["linux-arm64"]
    package_bytes: int = Field(ge=1, le=1024**3)
    package_sha256: DigestText
    package_signature: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{128}$")]
    package_url: Annotated[str, StringConstraints(pattern=_UPGRADE_URL)]
    package_version: Annotated[
        str, StringConstraints(pattern=r"^[0-9A-Za-z][0-9A-Za-z.+~-]{0,127}$")
    ]
    schema_version: Literal[1]
    target_binary_digest: DigestText
    target_build_digest: Annotated[
        str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")
    ]


class AgentUpgradeRepairManifest(_UpgradeDocument):
    """The repair capsule's authority, bound to one Spark and the package."""

    authority_sha256: DigestText
    kind: Literal["agent-upgrade-repair"]
    node_id: Annotated[str, StringConstraints(pattern=r"^spk_[0-9a-f]{32}$")]
    package: AgentUpgradePackage
    schema_version: Literal[2]


class AgentUpgradeRequestIntent(_UpgradeDocument):
    """Which Sparks the operator asked for: all of them, or an explicit list."""

    all: bool
    selectors: list[Annotated[str, StringConstraints(min_length=1)]] | None = Field(
        max_length=64
    )

    @model_validator(mode="after")
    def _scope_is_all_or_explicit(self) -> AgentUpgradeRequestIntent:
        if self.all != (self.selectors is None) or self.selectors == []:
            raise ValueError("an upgrade request names all Sparks or explicit ones")
        return self


class AgentUpgradeRolloutPayload(_UpgradeDocument):
    """An upgrade rollout: the package, the order, and each Spark's rollback source."""

    node_order: list[NodeText] = Field(max_length=64)
    package: AgentUpgradePackage
    request_intent: AgentUpgradeRequestIntent
    sources: dict[NodeText, AgentPackageSource]
    repair_manifest: AgentUpgradeRepairManifest | None = None


class AgentUpgradeRolloutResult(_UpgradeDocument):
    """What a rollout skipped, and the newer rollout that replaced it."""

    skipped: dict[NodeText, str] | None = None
    superseded_by: UuidText | None = None
