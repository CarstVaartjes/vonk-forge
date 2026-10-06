"""Exact source-bound rollback authority for the current package transaction."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .wire_model import Digest, WireEnum, WireModel


class PackageRollbackSource(WireModel):
    package_sha256: Digest
    package_signature: Annotated[str, Field(pattern=r"^[0-9a-f]{128}$")]
    package_version: Annotated[
        str, Field(pattern=r"^[0-9A-Za-z][0-9A-Za-z.+~:-]{0,127}$")
    ]
    binary_sha256: Digest
    helper_sha256: Digest


class PackageRollbackAuthority(WireModel):
    source: PackageRollbackSource
    attempt_nonce: Digest
    activation_deadline: int = Field(
        strict=True, ge=1, le=2**63 - 1, json_schema_extra={"format": "int64"}
    )


class PackageActivationPhase(WireEnum):
    """Where a package activation transaction stands."""

    ARMED = "armed"
    ACTIVATION_FAILED = "activation_failed"
    ACKNOWLEDGED = "acknowledged"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"


class PackageActivationReceipt(WireModel):
    schema_version: Literal[2]
    node_id: Annotated[str, Field(pattern=r"^spk_[0-9a-f]{32}$")]
    source_package_sha256: Digest
    source_version: Annotated[
        str, Field(pattern=r"^[0-9A-Za-z][0-9A-Za-z.+~:-]{0,127}$")
    ]
    source_binary_sha256: Digest
    candidate_package_sha256: Digest
    candidate_version: Annotated[
        str, Field(pattern=r"^[0-9A-Za-z][0-9A-Za-z.+~:-]{0,127}$")
    ]
    candidate_binary_sha256: Digest
    attempt_nonce: Digest
    phase: PackageActivationPhase
    created_at: int = Field(
        strict=True, ge=1, le=2**63 - 1, json_schema_extra={"format": "int64"}
    )
    updated_at: int = Field(
        strict=True, ge=1, le=2**63 - 1, json_schema_extra={"format": "int64"}
    )
    outcome: Annotated[str, Field(pattern=r"^[a-z_]{1,128}$")]

    @model_validator(mode="after")
    def ordered_timestamps(self) -> "PackageActivationReceipt":
        if self.updated_at < self.created_at:
            raise ValueError("activation receipt precedes its transaction")
        return self
