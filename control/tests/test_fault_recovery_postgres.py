"""Local CA journal faults retain intent and recover through PostgreSQL."""

import pytest
from sqlalchemy import text
from vonk_agent_protocol import UnknownError
from vonk_control.local_ca import LocalCertificateAuthority
from vonk_control.pki import IssuedCertificate
from vonk_control.step_ca import StepCAUnavailable

from .test_local_ca import _binding, local_ca  # noqa: F401 - pytest fixture
from .test_step_ca import NODE_ID, NOW, _csr


def test_ca_journal_unavailable_then_same_request_recovers(local_ca):  # noqa: F811 - pytest fixture
    """Catches a storage outage poisoning an exact accepted issuance request."""
    ca, _, options = local_ca
    csr = _csr()
    request = _binding(ca, csr)
    sessions = options["sessions"]
    with sessions.begin() as session:
        session.execute(
            text(
                "ALTER TABLE local_certificate_issuance RENAME TO unavailable_ca_journal"
            )
        )
    try:
        assert isinstance(ca.check_health(), UnknownError)
        with pytest.raises(StepCAUnavailable):
            ca.issue_node(NODE_ID, csr, NOW, request=request)
    finally:
        with sessions.begin() as session:
            session.execute(
                text(
                    "ALTER TABLE unavailable_ca_journal RENAME TO local_certificate_issuance"
                )
            )
    recovered = LocalCertificateAuthority(**options)
    issued = recovered.issue_node(NODE_ID, csr, NOW, request=request)
    assert isinstance(issued, IssuedCertificate)
    assert recovered.observe_node(csr, NOW, request=request) == issued
