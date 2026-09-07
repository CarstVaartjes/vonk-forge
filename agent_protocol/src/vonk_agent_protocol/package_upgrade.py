"""Exact source-bound rollback authority for the current package transaction."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .wire_model import Digest, WireModel


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
    activation_deadline: int = Field(strict=True, ge=1)


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
    phase: Literal[
        "armed",
        "activation_failed",
        "acknowledged",
        "rolling_back",
        "rolled_back",
        "rollback_failed",
    ]
    created_at: int = Field(strict=True, ge=1)
    updated_at: int = Field(strict=True, ge=1)
    outcome: Annotated[str, Field(pattern=r"^[a-z_]{1,128}$")]

    @model_validator(mode="after")
    def ordered_timestamps(self) -> "PackageActivationReceipt":
        if self.updated_at < self.created_at:
            raise ValueError("activation receipt precedes its transaction")
        return self
