"""Revocation confirmation against the real local CA on real PostgreSQL.

Every row, signed or not and expired or not, is revoked at the CA by serial and
only then confirmed. Passes are bounded, resume from the persisted due time and
back off by blocked time. No test sleeps; the clock is injected.
"""

import hashlib
import logging
import math
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from vonk_agent_protocol import CertificateCode, WaitReason
from vonk_agent_protocol.state_machines import (
    CertificateIssuancePurpose,
    CertificateRecordState,
    CertificateRotationState,
    NodeIdentityState,
)
from vonk_control.enrollment import EnrollmentService
from vonk_control.enrollment.ending import retain_ended_effect
from vonk_control.models import (
    AgentCertificate,
    AgentIssuedCertificateRevocation,
    AgentNode,
)
from vonk_control.models.fleet import LocalCertificateRevocation
from vonk_control.persisted_waits import read_wait, schedule_wait
from vonk_control.step_ca import StepCAError, StepCAUnavailable

from .test_local_ca import local_ca as local_ca  # noqa: PLC0414 -- fixture
from .test_step_ca import NODE_ID, NOW, _csr

BATCH = 64
EVENT = "enrollment.revocation_unconfirmed"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def world(local_ca):
    """The real CA plus a service built BEFORE any evidence is seeded."""
    ca, _material, options = local_ca
    sessions = options["sessions"]
    clock = Clock()
    service = EnrollmentService(sessions, ca, clock=clock)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=NODE_ID, state=NodeIdentityState.ACTIVE))
    return ca, sessions, clock, service


def _bind(ca, csr, *, now=NOW):
    return ca.prepare_request(
        NODE_ID,
        csr,
        now,
        purpose=CertificateIssuancePurpose.ENROLLMENT,
        source_serial=None,
        generation=1,
    )


def _seed_evidence(sessions, ca, csr, *, now=NOW, count=1, wait=None):
    """Rows shaped like ``retain_ended_effect``; returns their bindings."""
    bindings = []
    with sessions.begin() as session:
        for _ in range(count):
            binding = _bind(ca, csr, now=now)
            retain_ended_effect(session, binding, csr.decode("ascii"), NOW)
            row = session.get(AgentIssuedCertificateRevocation, binding.serial)
            if wait is not None:
                wait(row)
            bindings.append(binding)
    return bindings


def _serials(bindings):
    return [binding.serial for binding in bindings]


def _legacy_plus_five(row) -> None:
    # What the old code left behind: a +5 s check with observation evidence.
    schedule_wait(
        row,
        NOW - timedelta(seconds=5),
        now=NOW - timedelta(seconds=10),
        reason=WaitReason.OBSERVATION_UNAVAILABLE,
    )


def _journal(sessions) -> set[str]:
    with sessions() as session:
        return set(session.scalars(select(LocalCertificateRevocation.serial)))


def _evidence(sessions, serial):
    with sessions() as session:
        return session.get(AgentIssuedCertificateRevocation, serial)


def _unconfirmed(sessions) -> int:
    with sessions() as session:
        return session.scalar(
            select(func.count())
            .select_from(AgentIssuedCertificateRevocation)
            .where(AgentIssuedCertificateRevocation.ca_revoked_at.is_(None))
        ) + session.scalar(
            select(func.count())
            .select_from(AgentCertificate)
            .where(
                AgentCertificate.revoked_at.is_not(None),
                AgentCertificate.ca_revoked_at.is_(None),
            )
        )


def _spy(monkeypatch, ca, *, fail=None):
    calls: list[str] = []
    real = ca.revoke_node

    def revoke(serial, now):
        calls.append(serial)
        if fail is not None:
            fail(serial)
        return real(serial, now)

    monkeypatch.setattr(ca, "revoke_node", revoke)
    return calls


def _certificate(serial, generation, *, revoked, expired, now=NOW):
    return AgentCertificate(
        serial=serial,
        node_id=NODE_ID,
        generation=generation,
        not_before=now - timedelta(days=61 if expired else 1),
        not_after=now - timedelta(days=1) if expired else now + timedelta(days=25),
        fingerprint=hashlib.sha256(serial.encode()).hexdigest(),
        state=CertificateRecordState.REVOKED
        if revoked
        else CertificateRecordState.ACTIVE,
        revoked_at=now - timedelta(days=3) if revoked else None,
    )


@pytest.mark.slow(30)  # 1,200 real CA journal rows drained 64 per pass on PostgreSQL
def test_production_scale_drains_in_bounded_passes(world, monkeypatch):
    """Catches the observe gate and expiry skips stranding ~1,100 rows forever."""
    ca, sessions, _clock, service = world
    csr = _csr()
    expired_at = NOW - timedelta(days=31)
    serials = _serials(_seed_evidence(sessions, ca, csr, count=750))  # unsigned
    bindings = _seed_evidence(sessions, ca, csr, count=50)  # signed
    for binding in bindings:
        ca.issue_node(NODE_ID, csr, NOW, request=binding)
    signed = _serials(bindings)
    expired = _serials(_seed_evidence(sessions, ca, csr, now=expired_at, count=400))
    with sessions.begin() as session:
        for index, serial in enumerate(serials + signed + expired):
            if index % 3 == 1:
                _legacy_plus_five(session.get(AgentIssuedCertificateRevocation, serial))
        for number in range(33):
            session.add(
                _certificate(
                    str(900_000 + number), number + 1, revoked=True, expired=number < 31
                )
            )
        for number in range(2):
            session.add(
                _certificate(
                    str(800_000 + number), 100 + number, revoked=False, expired=False
                )
            )
    total = 1200 + 33
    calls = _spy(monkeypatch, ca)

    passes = 0
    while _unconfirmed(sessions):
        passes += 1
        assert passes <= math.ceil(total / BATCH) + 1
        before = len(calls)
        assert service.reconcile_revocations()
        attempted = calls[before:]
        assert 0 < len(attempted) <= BATCH
        assert len(set(attempted)) == len(attempted)

    live = {"800000", "800001"}
    assert _journal(sessions) >= set(serials + signed + expired)
    assert len(set(calls)) == total
    assert not live & _journal(sessions) & set(calls)
    with sessions() as session:
        for serial in live:
            row = session.get(AgentCertificate, serial)
            assert row.revoked_at is None and row.ca_revoked_at is None
            assert row.next_action_at is None and row.wait_evidence is None
    assert not service.reconcile_revocations()


def test_budget_stops_the_pass_and_the_cursor_continues(world, monkeypatch):
    ca, sessions, _clock, service = world
    serials = _serials(_seed_evidence(sessions, ca, _csr(), count=10))
    calls = _spy(monkeypatch, ca)
    ticks = iter(range(1000))

    assert service.reconcile_revocations(
        budget_seconds=4.5, monotonic=lambda: float(next(ticks))
    )
    first = list(calls)
    assert len(first) == 4

    assert service.reconcile_revocations(limit=3)
    second = calls[len(first) :]
    assert len(second) == 3 and not set(first) & set(second)
    assert service.reconcile_revocations()
    assert set(calls) == set(serials) and len(calls) == len(serials)


def test_ca_unavailable_backs_off_by_blocked_time_and_recovers(
    world, monkeypatch, caplog
):
    ca, sessions, clock, service = world
    serials = _serials(_seed_evidence(sessions, ca, _csr(), count=8))
    broken = set(serials[:3])
    unavailable = {"on": True}

    def fail(serial):
        if unavailable["on"] and serial in broken:
            raise StepCAUnavailable("journal unavailable")

    _spy(monkeypatch, ca, fail=fail)
    caplog.set_level(logging.INFO, logger="vonk_control.enrollment")

    delays = []
    first_blocked = set()
    for _ in range(9):
        caplog.clear()
        started = clock.now
        service.reconcile_revocations()
        assert sum(EVENT in record.getMessage() for record in caplog.records) == 1
        rows = [_evidence(sessions, serial) for serial in broken]
        assert all(row.ca_revoked_at is None for row in rows)
        wait = read_wait(rows[0])
        assert wait is not None
        first_blocked.add(wait.first_blocked_at)
        delays.append((wait.next_check - started).total_seconds())
        clock.now = wait.next_check
    assert delays[0] == 5.0
    assert delays == sorted(delays) and delays[-1] == 300.0
    assert len(first_blocked) == 1
    assert _journal(sessions) >= set(serials[3:])
    assert not _journal(sessions) & broken

    unavailable["on"] = False
    caplog.clear()
    assert service.reconcile_revocations()
    assert _journal(sessions) >= broken
    assert not _unconfirmed(sessions)
    assert not any(EVENT in record.getMessage() for record in caplog.records)


def test_crash_between_revoke_and_confirm_confirms_after_restart(world, monkeypatch):
    ca, sessions, clock, service = world
    (serial,) = _serials(_seed_evidence(sessions, ca, _csr()))

    def crash(*_args):
        raise SystemExit

    with monkeypatch.context() as patched:
        patched.setattr(service, "_confirm_remote_revocation", crash)
        with pytest.raises(SystemExit):
            service.reconcile_revocations()
    row = _evidence(sessions, serial)
    assert serial in _journal(sessions) and row.ca_revoked_at is None
    assert row.next_action_at > clock.now
    assert not EnrollmentService(sessions, ca, clock=clock).reconcile_revocations()

    clock.now = row.next_action_at
    restarted = EnrollmentService(sessions, ca, clock=clock)
    assert restarted.reconcile_revocations()
    assert _evidence(sessions, serial).ca_revoked_at is not None


def test_crash_before_revoke_is_not_hot_and_retries_when_due(world, monkeypatch):
    ca, sessions, clock, service = world
    (serial,) = _serials(_seed_evidence(sessions, ca, _csr()))
    calls = _spy(monkeypatch, ca)
    real = ca.revoke_node

    def crash(*_args):
        raise SystemExit

    monkeypatch.setattr(ca, "revoke_node", crash)
    with pytest.raises(SystemExit):
        service.reconcile_revocations()
    monkeypatch.setattr(ca, "revoke_node", real)
    row = _evidence(sessions, serial)
    assert serial not in _journal(sessions) and row.ca_revoked_at is None
    assert row.next_action_at > clock.now
    assert not service.reconcile_revocations() and not calls

    clock.now = row.next_action_at
    assert service.reconcile_revocations()
    assert serial in _journal(sessions)
    assert _evidence(sessions, serial).ca_revoked_at is not None


def test_advisory_lock_busy_backs_off_then_confirms(world):
    ca, sessions, clock, service = world
    (serial,) = _serials(_seed_evidence(sessions, ca, _csr()))
    key = int.from_bytes(
        hashlib.sha256(f"serial:{serial}".encode()).digest()[:8], signed=True
    )
    engine = sessions.kw["bind"]
    with engine.connect() as holder:
        holder.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
        assert not service.reconcile_revocations()
        row = _evidence(sessions, serial)
        assert row.ca_revoked_at is None and serial not in _journal(sessions)
        assert row.next_action_at > clock.now
        holder.rollback()

    clock.now = row.next_action_at
    assert service.reconcile_revocations()
    assert serial in _journal(sessions)
    assert _evidence(sessions, serial).ca_revoked_at is not None


def test_late_signer_is_fenced_by_the_revocation(world):
    """Catches revoking only issued bindings: a never-signed one must be fenced.

    The service is built first (the fixture), then the row is seeded, so
    ``_import_revocations`` cannot have fenced the serial at construction.
    """
    ca, sessions, _clock, service = world
    csr = _csr()
    (binding,) = _seed_evidence(sessions, ca, csr)
    assert binding.serial not in _journal(sessions)

    assert service.reconcile_revocations()

    assert binding.serial in _journal(sessions)
    with pytest.raises(StepCAError) as refused:
        ca.issue_node(NODE_ID, csr, NOW, request=binding)
    assert refused.value.reason_code == CertificateCode.ISSUANCE_REVOKED


def test_live_credential_is_never_revoked(world, monkeypatch):
    ca, sessions, _clock, service = world
    calls = _spy(monkeypatch, ca)
    with sessions.begin() as session:
        session.add(_certificate("777001", 7, revoked=False, expired=False))
        session.add(
            AgentIssuedCertificateRevocation(
                serial="777001",
                node_id=NODE_ID,
                provider_request_id="live-evidence",
                generation=7,
                state=CertificateRotationState.REVOCATION_PENDING,
                created_at=NOW,
                updated_at=NOW,
            )
        )

    assert service.reconcile_revocations()

    assert not calls and "777001" not in _journal(sessions)
    assert _evidence(sessions, "777001") is None
    with sessions() as session:
        live = session.get(AgentCertificate, "777001")
        assert live.revoked_at is None and live.state == CertificateRecordState.ACTIVE


def test_rows_in_current_production_states_are_adopted(world):
    ca, sessions, _clock, service = world
    csr = _csr()
    cleared = _serials(_seed_evidence(sessions, ca, csr, count=2))
    waiting = _serials(
        _seed_evidence(sessions, ca, csr, count=2, wait=_legacy_plus_five)
    )
    expired = _serials(
        _seed_evidence(
            sessions,
            ca,
            csr,
            now=NOW - timedelta(days=31),
            count=2,
            wait=_legacy_plus_five,
        )
    )
    for serial in cleared:
        row = _evidence(sessions, serial)
        assert row.next_action_at is None and row.wait_evidence is None
    for serial in waiting:
        row = _evidence(sessions, serial)
        assert row.wait_evidence.reason == WaitReason.OBSERVATION_UNAVAILABLE

    assert service.reconcile_revocations()

    assert not _unconfirmed(sessions)
    assert _journal(sessions) >= set(cleared + waiting + expired)
