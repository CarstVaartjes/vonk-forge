from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import AgentIdentity, AgentSource
from vonk_control.models import AgentCertificate, AgentNode, AgentPresence, Base
from vonk_control.presence import (
    AgentPresenceService,
    ManagementAddressObservation,
    ManagementAddressPolicy,
    PresenceError,
)

NODE_ID = "spk_" + "a" * 32
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def test_management_address_policy_accepts_only_canonical_bounded_addresses() -> None:
    policy = ManagementAddressPolicy.parse(
        "10.0.0.0/24,2001:db8:42::/64",
        forbidden_cidrs="10.0.0.240/28",
    )

    assert policy.validate("10.0.0.42") == "10.0.0.42"
    assert policy.validate("2001:db8:42::2") == "2001:db8:42::2"

    for address in (
        "127.0.0.1",
        "169.254.1.1",
        "224.0.0.1",
        "10.0.0.241",
        "10.0.1.1",
        "10.0.0.0",
        "10.0.0.255",
    ):
        with pytest.raises(PresenceError):
            policy.validate(address)


def test_management_address_policy_accepts_secret_file_line_format() -> None:
    policy = ManagementAddressPolicy.parse("10.0.0.0/24\n2001:db8:42::/64\n")

    assert policy.validate("10.0.0.42") == "10.0.0.42"
    assert policy.validate("2001:db8:42::2") == "2001:db8:42::2"


def test_management_address_policy_rejects_ambiguous_network_policy() -> None:
    for allowed, forbidden, error in (
        ("10.0.0.1/24", "", "canonical"),
        ("10.0.0.0/24,10.0.0.0/24", "", "duplicate"),
        ("", "", "empty"),
        ("10.0.0.0/24", "10.0.0.0/24", "fully forbidden"),
    ):
        with pytest.raises(PresenceError, match=error):
            ManagementAddressPolicy.parse(allowed, forbidden_cidrs=forbidden)


@pytest.fixture
def presence_system(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'presence.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=NODE_ID, state="active", capabilities=[]))
        session.add(
            AgentCertificate(
                serial="serial-a",
                node_id=NODE_ID,
                fingerprint="fingerprint-a",
                state="active",
                generation=1,
                not_before=NOW - timedelta(minutes=1),
                not_after=NOW + timedelta(hours=1),
            )
        )
    current = [NOW]
    service = AgentPresenceService(
        sessions,
        ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=lambda: current[0],
    )
    source = AgentSource(
        identity=AgentIdentity(NODE_ID, "serial-a", "fingerprint-a", True),
        management_address="10.0.0.42",
    )
    return sessions, service, source, current


def test_observe_upserts_presence_bound_to_exact_active_certificate(
    presence_system,
) -> None:
    sessions, service, source, current = presence_system

    observed = service.observe(source)

    assert observed == ManagementAddressObservation(
        node_id=NODE_ID,
        certificate_serial="serial-a",
        address="10.0.0.42",
        observed_at=NOW,
    )
    with sessions() as session:
        row = session.get(AgentPresence, NODE_ID)
        assert row is not None
        assert row.certificate_serial == "serial-a"
        assert row.certificate_fingerprint == "fingerprint-a"
        assert row.management_address == "10.0.0.42"
        assert row.observed_at.replace(tzinfo=UTC) == NOW

    current[0] += timedelta(seconds=30)
    updated = service.observe(
        AgentSource(identity=source.identity, management_address="10.0.0.43")
    )
    assert updated.address == "10.0.0.43"
    with sessions() as session:
        assert session.query(AgentPresence).count() == 1
        assert session.get(AgentPresence, NODE_ID).management_address == "10.0.0.43"


def test_latest_fails_closed_for_stale_or_no_longer_active_certificate(
    presence_system,
) -> None:
    sessions, service, source, current = presence_system
    service.observe(source)

    assert service.latest(NODE_ID, maximum_age_seconds=60).address == "10.0.0.42"
    current[0] += timedelta(seconds=61)
    with pytest.raises(PresenceError, match="stale"):
        service.latest(NODE_ID, maximum_age_seconds=60)

    current[0] = NOW
    with sessions.begin() as session:
        certificate = session.get(AgentCertificate, "serial-a")
        assert certificate is not None
        certificate.state = "retired"
    with pytest.raises(PresenceError, match="certificate"):
        service.latest(NODE_ID, maximum_age_seconds=60)
    with pytest.raises(PresenceError, match="certificate"):
        service.observe(source)


def test_invalid_management_source_never_creates_presence(presence_system) -> None:
    sessions, service, source, _ = presence_system

    with pytest.raises(PresenceError, match="outside"):
        service.observe(
            AgentSource(identity=source.identity, management_address="10.1.0.42")
        )

    with sessions() as session:
        assert session.get(AgentPresence, NODE_ID) is None


def test_latest_revalidates_durable_address_instead_of_trusting_the_row(
    presence_system,
) -> None:
    sessions, service, source, _ = presence_system
    service.observe(source)
    with sessions.begin() as session:
        row = session.get(AgentPresence, NODE_ID)
        assert row is not None
        row.management_address = "not-an-ip"

    with pytest.raises(PresenceError, match="canonical IP"):
        service.latest(NODE_ID, maximum_age_seconds=60)


def test_latest_rejects_a_malformed_durable_certificate_binding(
    presence_system,
) -> None:
    sessions, service, source, _ = presence_system
    service.observe(source)
    with sessions.begin() as session:
        row = session.get(AgentPresence, NODE_ID)
        assert row is not None
        row.certificate_fingerprint = ""

    with pytest.raises(PresenceError, match="binding is invalid"):
        service.latest(NODE_ID, maximum_age_seconds=60)


def test_latest_in_session_reads_through_the_callers_transaction(
    presence_system,
) -> None:
    sessions, service, source, _ = presence_system
    service.observe(source)

    with sessions.begin() as session:
        row = session.get(AgentPresence, NODE_ID)
        assert row is not None
        row.management_address = "10.0.0.43"
        session.flush()

        observed = service.latest_in_session(
            session,
            NODE_ID,
            maximum_age_seconds=60,
        )

        assert observed.address == "10.0.0.43"


def test_observe_in_session_rolls_back_with_the_callers_transaction(
    presence_system,
) -> None:
    sessions, service, source, _ = presence_system
    session = sessions()
    transaction = session.begin()
    try:
        observed = service.observe_in_session(session, source)
        assert observed.address == "10.0.0.42"
        assert session.get(AgentPresence, NODE_ID) is not None
    finally:
        transaction.rollback()
        session.close()

    with sessions() as check:
        assert check.get(AgentPresence, NODE_ID) is None
