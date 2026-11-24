"""Enrollment grants, public status and exact certificate issuance claims."""

from datetime import datetime
from typing import Annotated

from pydantic import ConfigDict, Field, StringConstraints

from vonk_agent_protocol.identifiers import UUID4_PATTERN
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState
from vonk_agent_protocol.outcome import UnknownError
from vonk_agent_protocol.state_machines import (
    CertificateRotationState,
    EnrollmentPurpose,
)
from vonk_control.machine_states import EnrollmentGrantStateField
from vonk_control.pki.issuance_contract import CertificateIssuanceBinding
from vonk_control.strict_json import StrictJSONModel

EnrollmentId = Annotated[str, StringConstraints(pattern=UUID4_PATTERN)]


class EnrollmentGrantStatus(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: EnrollmentId
    state: EnrollmentGrantStateField
    purpose: EnrollmentPurpose | None
    node_id: str | None = Field(pattern=r"^spk_[0-9a-f]{32}$")
    display_name: str | None
    expires_at: datetime
    consumed_at: datetime | None
    revoked_at: datetime | None


class EnrollmentGrant(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: str
    node_id: str | None
    expires_at: datetime
    purpose: EnrollmentPurpose
    token: str = Field(repr=False)


class EnrollmentIssuanceClaim(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    enrollment_id: str
    node_id: str
    csr_pem: bytes
    purpose: EnrollmentPurpose
    provider_request: CertificateIssuanceBinding | None


class EnrollmentRotationClaim(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    node_id: str
    source_serial: str
    generation: int
    csr_pem: bytes
    csr_public_key_fingerprint: str
    provider_request_id: str
    provider_request: CertificateIssuanceBinding | None
    state: CertificateRotationState
    owner: bool


class EnrollmentRotationRecoveryClaim(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    claim: EnrollmentRotationClaim
    retiring_serial: str


class EnrollmentObservationOutcome(UnknownError):
    state: LifecycleState


class EnrollmentRevocationStatus(StrictJSONModel):
    local_denial_complete: bool
    ca_confirmation_complete: bool
