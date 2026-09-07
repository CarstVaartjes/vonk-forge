"""Canonical enrollment and certificate-rotation JSON messages."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import Field, field_validator

from .wire_model import WireModel

MAX_CSR_BYTES = 16 * 1024
NodeId = Annotated[str, Field(pattern=r"^spk_[0-9a-f]{32}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class EnrollmentEvidence(WireModel):
    node_id: NodeId
    csr_public_key_fingerprint: Digest
    host_key_fingerprint: str = Field(min_length=1, max_length=512)
    hardware_fingerprint: str = Field(min_length=1, max_length=512)
    agent_digest: Digest
    boot_id: str = Field(min_length=1, max_length=128)
    observation_receipt_public_key: Digest

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


class EnrollmentBootstrapResponse(WireModel):
    controller_endpoint: str = Field(min_length=1, max_length=2048)
    enrollment_endpoint: str = Field(min_length=1, max_length=2048)
    ca_fingerprint: Digest
    ca_pem: str = Field(min_length=1, max_length=64 * 1024)
    controller_address: str | None
    service_hostnames: list[str] = Field(max_length=16)
    host_helper_authority_public_key: Digest


class RenewRequest(WireModel):
    csr: str = Field(min_length=1, max_length=MAX_CSR_BYTES)
    node_id: NodeId | None = None


class ActivateRequest(WireModel):
    generation: int = Field(ge=1)
    node_id: NodeId | None = None


class IssuedCertificateResponse(WireModel):
    node_id: NodeId
    certificate_pem: str = Field(min_length=1)
    chain_pem: str = Field(min_length=1)
    serial: str = Field(min_length=1)
    fingerprint: Digest
    not_before: str = Field(min_length=1)
    not_after: str = Field(min_length=1)
    generation: int = Field(ge=1)

    @field_validator("not_before", "not_after")
    @classmethod
    def aware_timestamp(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("certificate timestamps must include a timezone")
        return value
