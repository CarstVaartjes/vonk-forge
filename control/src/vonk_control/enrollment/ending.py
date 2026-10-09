"""Release admission owners while retaining exact late-effect revocation intent."""

from datetime import datetime

from sqlalchemy.orm import Session
from vonk_agent_protocol.state_machines import (
    CertificateRecordState,
    CertificateRotationState,
)

from ..ca_issuance_contract import CertificateIssuanceBinding
from ..models import AgentCertificate, AgentIssuedCertificateRevocation


def retain_ended_effect(
    session: Session,
    binding: CertificateIssuanceBinding | None,
    csr_pem: str,
    now: datetime,
) -> None:
    """An ended binding can only be observed and revoked, never rebound/adopted.

    Revocation intent has no node FK or node-unique gate. A fresh authorized
    request therefore need not wait for the unavailable authority. The accepted
    certificate validity bounds late observation, including across restart.
    """
    if binding is None:
        return
    certificate = session.get(AgentCertificate, binding.serial)
    if (
        certificate is not None
        and certificate.node_id == binding.node_id
        and certificate.state == CertificateRecordState.ACTIVE
        and certificate.revoked_at is None
    ):
        # Ending repair of a damaged replay is not permission to revoke the
        # already accepted working credential. Explicit newer intent owns that.
        return
    existing = session.get(AgentIssuedCertificateRevocation, binding.serial)
    if existing is None:
        session.add(
            AgentIssuedCertificateRevocation(
                serial=binding.serial,
                node_id=binding.node_id,
                provider_request_id=binding.request_id,
                provider_request=binding.model_dump(mode="json"),
                csr_pem=csr_pem,
                fingerprint=None,
                generation=binding.generation,
                state=CertificateRotationState.REVOCATION_PENDING,
                created_at=now,
                updated_at=now,
            )
        )
