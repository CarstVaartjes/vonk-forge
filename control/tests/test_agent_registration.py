from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from vonk_control.agent_registration import (
    AgentRegistrationError,
    AgentRegistrationService,
    RegistrationRequest,
)
from vonk_control.enrollment_service import VerificationResult

NODE = "spk_" + "a" * 32
IDENTITY = "spiffe://vonk-forge.local/node/" + NODE


@pytest.fixture
def service():
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.exec_driver_sql("CREATE TABLE enrollment_intents (intent_id TEXT PRIMARY KEY,node_id TEXT,state TEXT,consumed_at DATETIME,expires_at DATETIME,metadata JSON)")
        c.exec_driver_sql("CREATE TABLE enrollment_evidence (evidence_id TEXT PRIMARY KEY,intent_id TEXT UNIQUE,node_id TEXT,csr_pem TEXT,host_identity TEXT,hardware_identity TEXT,agent_version TEXT,boot_id TEXT,evidence JSON)")
        c.exec_driver_sql("CREATE TABLE certificate_records (node_id TEXT,certificate_identity TEXT,state TEXT)")
        c.execute(text("INSERT INTO enrollment_intents VALUES ('i',:node,'waiting_for_registration',:consumed,:expires,:metadata)"), {"node": NODE, "consumed": datetime.now(UTC), "expires": datetime.now(UTC) + timedelta(minutes=5), "metadata": "{}"})
    return AgentRegistrationService(sessionmaker(engine, expire_on_commit=False))


def context():
    return VerificationResult("i", NODE, {})


def request():
    return RegistrationRequest(context(), IDENTITY, "-----BEGIN CSR-----", "host", "hardware", "digest", "boot", "csr")


def test_registration_creates_pending_evidence_only(service):
    result = service.register(request())
    assert result.state == "pending_review"
    assert service._sessions().execute(text("SELECT count(*) FROM enrollment_evidence")).scalar() == 1
    assert service._sessions().execute(text("SELECT state FROM enrollment_intents")).scalar() == "pending_review"


def test_duplicate_registration_rejected(service):
    service.register(request())
    with pytest.raises(AgentRegistrationError, match="already submitted"):
        service.register(request())


@pytest.mark.parametrize("bad", ["spiffe://vonk-forge.local/node/spk_" + "b" * 32, "bad"])
def test_invalid_certificate_identity_rejected(service, bad):
    with pytest.raises(AgentRegistrationError, match="certificate identity"):
        service.register(RegistrationRequest(context(), bad, "csr", "host", "hardware", "digest", "boot", "csr"))


def test_missing_or_unconsumed_evidence_rejected(service):
    missing = VerificationResult("missing", NODE, {})
    with pytest.raises(AgentRegistrationError, match="missing"):
        service.register(missing, certificate_identity=IDENTITY, csr_pem="csr", evidence={})

    with service._sessions.begin() as session:
        session.execute(text("UPDATE enrollment_intents SET consumed_at = NULL WHERE intent_id = 'i'"))
    with pytest.raises(AgentRegistrationError, match="not consumed"):
        service.register(request())


def test_pending_node_is_not_active_fleet(service):
    service.register(request())
    assert service._sessions().execute(text("SELECT state FROM enrollment_intents")).scalar() == "pending_review"
    assert service._sessions().execute(text("SELECT count(*) FROM certificate_records WHERE state = 'active'")).scalar() == 0
