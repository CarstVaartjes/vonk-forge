"""Real managed CA outages and PostgreSQL: bounded endings and fresh admission.

Catches an unavailable issuer becoming a standing refusal, premature retirement
of the still-valid source certificate, and ended claims blocking newer requests.
No provider call, HTTP transport, or SQL persistence is replaced.
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import UnknownError
from vonk_agent_protocol.state_machines import (
    CertificateIssuancePurpose,
    CertificateRecordState,
)
from vonk_control.enrollment.service import EnrollmentService
from vonk_control.enrollment_contract import (
    EnrollmentGrant,
    EnrollmentObservationOutcome,
)
from vonk_control.models import AgentCertificate, Base
from vonk_control.pki import IssuedCertificate

from .test_ca_image_controller_postgres import _managed_ca, _provider
from .test_enrollment import NODE_ID, OTHER_NODE_ID, csr, evidence


def _issued_within(
    action: Callable[[], IssuedCertificate | UnknownError],
) -> IssuedCertificate:
    deadline = time.monotonic() + 15
    while True:
        outcome = action()
        assert time.monotonic() < deadline, "issuance did not heal within its budget"
        if isinstance(outcome, IssuedCertificate):
            return outcome
        time.sleep(0.05)


@pytest.mark.slow(60)  # Real CA stop/restart and four bounded provider attempts.
@pytest.mark.parametrize(
    ("purpose", "supersede"),
    (
        (CertificateIssuancePurpose.ENROLLMENT, False),
        (CertificateIssuancePurpose.ENROLLMENT, True),
        (CertificateIssuancePurpose.ROTATION, False),
    ),
)
def test_ca_outage_preserves_authority_and_recovers_without_poisoning_admission(
    postgres_engine, tmp_path, purpose, supersede
):
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with _managed_ca(tmp_path) as (settings, restart_ca, stop_ca):
        provider = _provider(settings)
        service = EnrollmentService(sessions, provider, clock=lambda: datetime.now(UTC))
        source = None
        if purpose == CertificateIssuancePurpose.ROTATION:
            original = csr()
            grant = service.create(NODE_ID, "admin", 600)
            assert isinstance(grant, EnrollmentGrant)
            source = service.submit(grant.token, original, evidence(original))
            assert isinstance(source, IssuedCertificate)
        else:
            grant = service.create(NODE_ID, "admin", 600)
            assert isinstance(grant, EnrollmentGrant)
        request = csr()
        try:
            stop_ca()
            # Production owns retries and their ending. A security exception here
            # fails the test: an unavailable issuer is never denied authority.
            started = time.monotonic()
            outcome = (
                service.renew(NODE_ID, source.serial, request)
                if source is not None
                else service.submit(grant.token, request, evidence(request))
            )
            assert time.monotonic() - started < 15
            assert isinstance(outcome, EnrollmentObservationOutcome)
            if source is not None:
                with sessions() as session:
                    retained = session.get(AgentCertificate, source.serial)
                    assert retained is not None
                    assert retained.state == CertificateRecordState.ACTIVE
                    assert (
                        retained.revoked_at is None and retained.ca_revoked_at is None
                    )
        finally:
            provider._client.close()
            settings = restart_ca()
        # A new Controller owner observes the same real PostgreSQL state, with
        # no fixture cleanup of leases, claims or queue heads between requests.
        provider = _provider(settings)
        recovered = EnrollmentService(
            sessions, provider, clock=lambda: datetime.now(UTC)
        )
        try:
            if source is not None:
                fresh = csr()
                issued = _issued_within(
                    lambda: recovered.renew(NODE_ID, source.serial, fresh)
                )
                assert isinstance(issued, IssuedCertificate)
                recovered.activate(NODE_ID, issued.serial, issued.generation)
                assert isinstance(
                    recovered.renew(NODE_ID, issued.serial, csr()), IssuedCertificate
                )
            else:
                if supersede:
                    grant = recovered.create(NODE_ID, "admin", 600)
                    assert isinstance(grant, EnrollmentGrant)
                    request = csr()
                # Replay reuses the original accepted binding; a newer grant
                # fences its obsolete publisher instead of issuing it first.
                _issued_within(
                    lambda: recovered.submit(grant.token, request, evidence(request))
                )
            fresh = csr(OTHER_NODE_ID)
            fresh_grant = recovered.create(OTHER_NODE_ID, "admin", 600)
            assert isinstance(fresh_grant, EnrollmentGrant)
            _issued_within(
                lambda: recovered.submit(
                    fresh_grant.token, fresh, evidence(fresh, node_id=OTHER_NODE_ID)
                )
            )
        finally:
            provider._client.close()
