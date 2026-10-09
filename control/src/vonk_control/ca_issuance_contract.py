"""Exact, authenticated CA journal request and observation contracts."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator
from vonk_agent_protocol import CertificateCode
from vonk_agent_protocol.enrollment import CertificateGeneration
from vonk_agent_protocol.state_machines import (
    CertificateIssuancePurpose,
    CertificateJournalState,
    CertificateRequestMode,
)

from .strict_json import StrictJSONModel

Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Serial = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,47}$")]
Instant = Annotated[
    str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
]


class CertificateIssuanceBinding(StrictJSONModel):
    request_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{43}$")]
    node_id: Annotated[str, StringConstraints(pattern=r"^spk_[0-9a-f]{32}$")]
    csr_sha256: Digest
    serial: Serial
    not_before: Instant
    not_after: Instant
    issuer_fingerprint: Digest
    provisioner_name: Annotated[
        str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$")
    ]
    provisioner_kid: Annotated[
        str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    ]
    policy_sha256: Digest
    purpose: CertificateIssuancePurpose
    source_serial: Serial | None
    generation: CertificateGeneration

    @model_validator(mode="after")
    def exact_effect(self) -> "CertificateIssuanceBinding":
        if int(self.serial).bit_length() > 159:
            raise ValueError("certificate serial exceeds 159 bits")
        if (
            self.source_serial is not None
            and int(self.source_serial).bit_length() > 159
        ):
            raise ValueError("source serial exceeds 159 bits")
        if (self.purpose == CertificateIssuancePurpose.ROTATION) != (
            self.source_serial is not None
        ):
            raise ValueError("source serial must identify only rotation requests")
        before = datetime.fromisoformat(self.not_before)
        after = datetime.fromisoformat(self.not_after)
        if after <= before:
            raise ValueError("certificate lifetime must be positive")
        if self.serial == self.source_serial:
            raise ValueError("rotation must use a new certificate serial")
        return self


class CertificateSignRequest(StrictJSONModel):
    csr: str
    ott: str
    request: CertificateIssuanceBinding
    mode: CertificateRequestMode


class CertificateIssuedReply(StrictJSONModel):
    state: Literal[CertificateJournalState.ISSUED]
    request: CertificateIssuanceBinding
    crt: str
    ca: str
    certChain: list[str]


class CertificatePendingReply(StrictJSONModel):
    state: Literal[CertificateJournalState.PENDING]
    request: CertificateIssuanceBinding
    reason_code: Literal[CertificateCode.ISSUANCE_IN_PROGRESS]


class CertificateAbsentReply(StrictJSONModel):
    state: Literal[CertificateJournalState.ABSENT]
    request: CertificateIssuanceBinding


CertificateRefusalReason = CertificateCode


class CertificateRefusalReply(StrictJSONModel):
    reason_code: CertificateRefusalReason
    detail: str = Field(max_length=512)


class CertificateIssuanceContract(StrictJSONModel):
    """Schema export roots for the same-process CA and hosted proof consumers."""

    sign: CertificateSignRequest
    refusal: CertificateRefusalReply
    reply: Annotated[
        CertificateIssuedReply | CertificatePendingReply | CertificateAbsentReply,
        Field(discriminator="state"),
    ]
