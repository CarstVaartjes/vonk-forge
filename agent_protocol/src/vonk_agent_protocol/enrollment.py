"""Canonical enrollment and certificate-rotation JSON messages."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Self

from pydantic import Field, field_validator, model_validator

from .http_failure import RenewalWindow
from .wire_model import WireModel

CertificateGeneration = Annotated[int, Field(ge=1, le=2**64 - 1)]

MAX_CSR_BYTES = 16 * 1024
# Physical allocation budget for complete enrollment/bootstrap JSON bodies.
# This includes UTF-8 encoding, JSON escapes, and every envelope field.
MAX_ENROLLMENT_RESPONSE_BYTES = 64 * 1024


def _check_response_budget(value: WireModel) -> None:
    from .contracts import canonical_message

    observed = len(canonical_message(value))
    if observed > MAX_ENROLLMENT_RESPONSE_BYTES:
        raise ValueError(
            f"enrollment response exceeds {MAX_ENROLLMENT_RESPONSE_BYTES} bytes "
            f"(observed {observed})"
        )


NodeId = Annotated[str, Field(pattern=r"^spk_[0-9a-f]{32}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class EnrollmentEvidence(WireModel):
    node_id: NodeId
    csr_public_key_fingerprint: Digest
    host_key_fingerprint: str = Field(min_length=1, max_length=512)
    hardware_fingerprint: str = Field(min_length=1, max_length=512)
    agent_digest: Digest
    boot_id: str = Field(min_length=1, max_length=128)

    @field_validator("host_key_fingerprint", "hardware_fingerprint", "boot_id")
    @classmethod
    def nonblank_evidence(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("evidence must not be blank")
        return value


class EnrollmentSubmitRequest(WireModel):
    grant_token: str = Field(min_length=43, max_length=64)
    csr: str = Field(min_length=1, max_length=MAX_CSR_BYTES)
    evidence: EnrollmentEvidence

    @field_validator("csr")
    @classmethod
    def ascii_csr(cls, value: str) -> str:
        value.encode("ascii")
        return value


class EnrollmentBootstrapResponse(WireModel):
    controller_endpoint: str = Field(min_length=1, max_length=2048)
    enrollment_endpoint: str = Field(min_length=1, max_length=2048)
    ca_fingerprint: Digest
    ca_pem: str = Field(min_length=1, max_length=64 * 1024)
    controller_address: str | None
    service_hostnames: list[str] = Field(max_length=16)
    host_helper_authority_public_key: Digest

    @model_validator(mode="after")
    def bounded_response(self) -> Self:
        _check_response_budget(self)
        return self


class RenewRequest(WireModel):
    csr: str = Field(min_length=1, max_length=MAX_CSR_BYTES)
    node_id: NodeId

    @field_validator("csr")
    @classmethod
    def ascii_csr(cls, value: str) -> str:
        value.encode("ascii")
        return value


class ExpiredRenewRequest(RenewRequest):
    """Proof of possession, bound to the exact durable replacement CSR."""

    serial: str = Field(min_length=1, max_length=128)
    signed_at: int = Field(ge=0)
    signature: str = Field(pattern=r"^[0-9a-f]{128}$")

    def proof_bytes(self) -> bytes:
        import hashlib

        digest = hashlib.sha256(self.csr.encode("ascii")).hexdigest()
        return (
            f"vonk-expired-renew-v1\n{self.node_id}\n{self.serial}\n"
            f"{self.signed_at}\n{digest}"
        ).encode("ascii")


class ActivateRequest(WireModel):
    generation: CertificateGeneration
    node_id: NodeId


class IssuedCertificateResponse(WireModel):
    renewal_window: RenewalWindow | None = None
    node_id: NodeId
    certificate_pem: str = Field(min_length=1)
    chain_pem: str = Field(min_length=1)
    serial: str = Field(min_length=1)
    fingerprint: Digest
    not_before: str = Field(min_length=1)
    not_after: str = Field(min_length=1)
    generation: CertificateGeneration

    @model_validator(mode="after")
    def bounded_response(self) -> Self:
        _check_response_budget(self)
        return self

    @field_validator("not_before", "not_after")
    @classmethod
    def aware_timestamp(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("certificate timestamps must include a timezone")
        return value
