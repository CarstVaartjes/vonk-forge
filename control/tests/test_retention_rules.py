"""Behaviour of the retention rule table: one case per entry, plus its guards.

Each entry gets one ended row past its window and one inside it; one pass must
remove the first and keep the second. The meta-check keeps every mapped table
either in the rule table or on the explicit retained list, so a new table can
no longer grow without a deletion rule.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import Connection, Engine, ExecutionContext
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.types import Message, Receive, Scope, Send
from vonk_control import terminal_history_collection
from vonk_control.agent_contacts import record_contact
from vonk_control.artifact_lifecycle import (
    ArtifactIdentity,
    clear_removal,
    reserve_removal,
)
from vonk_control.auth import _AGENT_IDENTITY_SCOPE_KEY, AgentIdentity
from vonk_control.models import (
    AgentCertificate,
    AgentEnrollment,
    AgentEnrollmentGrant,
    AgentIssuedCertificateRevocation,
    AgentNode,
    AgentNodeContact,
    AgentNodeProfile,
    AgentOperation,
    ArtifactDistributionAssignment,
    ArtifactLifecycleGate,
    Base,
    ControlProcessHeartbeat,
    FleetProfile,
    FleetProfileApplication,
    ModelCacheOperation,
    ModelCacheSet,
    ModelCacheSetArtifact,
    RecipeLibrarySyncRun,
    RecipeRun,
    ResourceReservation,
    RunNode,
)
from vonk_control.observation_delivery import ObservationDeliveryMiddleware
from vonk_control.terminal_history_collection import (
    PASS_BUDGET_SECONDS,
    RETAINED,
    TerminalHistoryCollector,
    retention_rules,
)

from .test_terminal_history_collection import _job, _stopped_run

if TYPE_CHECKING:
    from vonk_control.agent_api import AgentApiServices

NOW = datetime(2026, 11, 4, 12, tzinfo=UTC)
NODE = "spk_" + "1" * 32
WORKER = "a" * 64
_GENERATIONS = itertools.count(1)


@pytest.fixture
def sessions(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    # A pass with an unlimited budget: these tests check rules, not timing.
    monkeypatch.setattr(
        terminal_history_collection, "time", SimpleNamespace(monotonic=lambda: 0.0)
    )
    # One shared connection: the delivery recorder writes from a worker thread.
    engine = create_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(AgentNode(node_id=NODE, state="active"))
    yield factory
    engine.dispose()


def _hex() -> str:
    return uuid.uuid4().hex * 2


def _operation(session: Session, at: datetime) -> str:
    job = _job(at)
    session.add(job)
    session.flush()
    operation = AgentOperation(
        id=str(uuid.uuid4()),
        parent_job_id=job.id,
        node_id=NODE,
        kind="recipe.start",
        payload_digest="c" * 64,
        payload={},
        authority_revision="revision",
        state="succeeded",
        current_attempt=0,
        created_at=at,
        updated_at=at,
    )
    session.add(operation)
    return operation.id


def _plain_job(session: Session, at: datetime) -> str:
    job = _job(at)
    session.add(job)
    return job.id


def _cache_operation(session: Session, at: datetime) -> str:
    row = ModelCacheOperation(
        id=str(uuid.uuid4()),
        request_key=str(uuid.uuid4()),
        kind="download",
        state="succeeded",
        payload={},
        progress={},
        actor="operator",
        created_at=at,
        updated_at=at,
    )
    session.add(row)
    return row.id


def _application(session: Session, at: datetime) -> str:
    profile = FleetProfile(
        id=str(uuid.uuid4()),
        number=1 + int(at.timestamp()) % 100_000,
        name=f"history-{uuid.uuid4().hex[:8]}",
        created_by="operator",
        created_at=at,
        updated_at=at,
    )
    session.add(profile)
    session.flush()
    row = FleetProfileApplication(
        id=str(uuid.uuid4()),
        request_key=str(uuid.uuid4()),
        profile_id=profile.id,
        profile_digest="a" * 64,
        plan_digest=_hex(),
        state="failed",
        plan={},
        actor="operator",
        created_at=at,
        updated_at=at,
    )
    session.add(row)
    return row.id


def _sync_run(session: Session, at: datetime, *, state: str = "succeeded") -> str:
    row = RecipeLibrarySyncRun(
        id=str(uuid.uuid4()),
        request_key=str(uuid.uuid4()),
        trigger="manual",
        state=state,
        repository="example/recipes",
        result={},
        actor="operator",
        created_at=at,
        started_at=at,
        completed_at=at,
    )
    session.add(row)
    return row.id


def _assignment(session: Session, at: datetime) -> str:
    row = ArtifactDistributionAssignment(
        id=str(uuid.uuid4()),
        plan_digest=_hex(),
        node_id=NODE,
        generation=1,
        expires_at=at,
        model_artifact_set_sha256=_hex(),
        objects=[],
        oci_image_digest="sha256:" + "b" * 64,
        oci_image_config_digest="sha256:" + "c" * 64,
        oci_archive_sha256=_hex(),
        state="revoked",
        created_at=at,
        updated_at=at,
    )
    session.add(row)
    return row.id


def _reservation(session: Session, at: datetime) -> str:
    row = ResourceReservation(
        id=str(uuid.uuid4()),
        node_id=NODE,
        kind="disk",
        resource_key="disk",
        amount_bytes=1,
        owner_kind="job",
        owner_id=str(uuid.uuid4()),
        state="released",
        plan_digest="a" * 64,
        created_at=at,
        released_at=at,
    )
    session.add(row)
    return row.id


def _run(session: Session, at: datetime) -> str:
    run = _stopped_run(at, NODE)
    session.add(run)
    return run.id


def _heartbeat(session: Session, at: datetime) -> str:
    row = ControlProcessHeartbeat(
        id=str(uuid.uuid4()),
        process_kind="worker",
        process_instance_id=_hex(),
        loop_sequence=1,
        completed_at=at,
    )
    session.add(row)
    return row.id


def _cache_set(session: Session, at: datetime) -> str:
    digest = _hex()
    session.add(
        ModelCacheSet(
            artifact_set_sha256=digest,
            manifest={},
            expected_bytes=0,
            verified_bytes=0,
            state="failed",
            protected=False,
            protected_reasons=[],
            created_at=at,
            updated_at=at,
            last_accessed_at=at,
        )
    )
    return digest


def _certificate(session: Session, at: datetime) -> str:
    serial = str(uuid.uuid4().int)
    session.add(
        AgentCertificate(
            serial=serial,
            node_id=NODE,
            not_before=at - timedelta(days=30),
            not_after=at,
            fingerprint=serial,
            state="revoked",
            generation=next(_GENERATIONS),
            revoked_at=at,
            ca_revoked_at=at,
        )
    )
    return serial


def _issued_revocation(session: Session, at: datetime) -> str:
    serial = str(uuid.uuid4().int)
    session.add(
        AgentIssuedCertificateRevocation(
            serial=serial,
            node_id=NODE,
            provider_request_id=_hex(),
            generation=1,
            state="revoked",
            created_at=at,
            updated_at=at,
            ca_revoked_at=at,
        )
    )
    return serial


def _grant(session: Session, at: datetime) -> str:
    row = AgentEnrollmentGrant(
        id=str(uuid.uuid4()),
        token_digest=_hex(),
        created_by="operator",
        created_at=at - timedelta(minutes=10),
        expires_at=at,
        consumed_at=at - timedelta(minutes=5),
    )
    session.add(row)
    return row.id


def _retired_node(session: Session, at: datetime) -> str:
    node_id = "spk_" + uuid.uuid4().hex
    session.add(AgentNode(node_id=node_id, state="retired", revoked_at=at))
    return node_id


#: One seed per rule-table entry, and how far past (and inside) its window.
SEEDS: dict[str, Callable[[Session, datetime], str]] = {
    "agent_operations": _operation,
    "jobs": _plain_job,
    "model_cache_operations": _cache_operation,
    "fleet_profile_applications": _application,
    "recipe_library_sync_runs": _sync_run,
    "artifact_distribution_assignments": _assignment,
    "resource_reservations": _reservation,
    "recipe_runs": _run,
    "control_process_heartbeats": _heartbeat,
    "model_cache_sets": _cache_set,
    "agent_certificates": _certificate,
    "agent_issued_certificate_revocations": _issued_revocation,
    "agent_enrollment_grants": _grant,
    "agent_nodes": _retired_node,
}


def _present(sessions: sessionmaker[Session], table: str, key: str) -> bool:
    rule = next(rule for rule in retention_rules() if rule.table == table)
    with sessions() as session:
        return session.scalar(select(rule.key).where(rule.key == key)) is not None


def test_every_rule_has_a_behavioural_case() -> None:
    assert set(SEEDS) == {rule.table for rule in retention_rules()}


@pytest.mark.parametrize("table", sorted(SEEDS))
def test_each_rule_removes_an_ended_row_past_its_window_and_keeps_a_recent_one(
    sessions: sessionmaker[Session], table: str
) -> None:
    rule = next(rule for rule in retention_rules() if rule.table == table)
    with sessions.begin() as session:
        old = SEEDS[table](session, NOW - rule.window - timedelta(hours=1))
        recent = SEEDS[table](session, NOW - rule.window + timedelta(hours=1))
    removed = TerminalHistoryCollector(sessions, clock=lambda: NOW).collect()
    assert removed[table] >= 1
    assert not _present(sessions, table, old)
    assert _present(sessions, table, recent)


def test_every_mapped_table_has_a_rule_or_a_stated_reason() -> None:
    rules = retention_rules()
    ruled = {rule.table for rule in rules}
    children = {child.class_.__tablename__ for rule in rules for child in rule.children}
    mapped = set(Base.metadata.tables)
    assert mapped <= ruled | children | set(RETAINED), sorted(
        mapped - ruled - children - set(RETAINED)
    )
    assert not ruled & set(RETAINED)
    assert set(RETAINED) <= mapped


def test_the_running_worker_keeps_its_heartbeat_and_another_one_goes(
    sessions: sessionmaker[Session],
) -> None:
    old = NOW - timedelta(days=2)
    with sessions.begin() as session:
        session.add_all(
            (
                ControlProcessHeartbeat(
                    process_kind="worker",
                    process_instance_id=WORKER,
                    loop_sequence=0,
                    completed_at=None,
                ),
                ControlProcessHeartbeat(
                    process_kind="worker",
                    process_instance_id="b" * 64,
                    loop_sequence=0,
                    completed_at=None,
                ),
                ControlProcessHeartbeat(
                    process_kind="worker",
                    process_instance_id="c" * 64,
                    loop_sequence=3,
                    completed_at=old,
                ),
            )
        )
    TerminalHistoryCollector(
        sessions, clock=lambda: NOW, process_instance_id=WORKER
    ).collect()
    with sessions() as session:
        assert set(
            session.scalars(select(ControlProcessHeartbeat.process_instance_id))
        ) == {WORKER}


async def _deliver_observations(sessions: sessionmaker[Session], status: int) -> None:
    """One recipe-run observation report through the real delivery recorder."""

    async def app(_scope: Scope, _receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: Message) -> None:
        return None

    identity = AgentIdentity(
        node_id=NODE,
        certificate_serial="1",
        certificate_fingerprint="f" * 64,
        verified=True,
    )
    services = cast("AgentApiServices", SimpleNamespace(sessions=sessions))
    scope: Scope = {
        "type": "http",
        "path": "/agent/recipe-runs/observations",
        "method": "POST",
        "headers": [],
        _AGENT_IDENTITY_SCOPE_KEY: identity,
    }
    await ObservationDeliveryMiddleware(app, services=services)(scope, receive, send)


@pytest.mark.parametrize(
    ("observation", "kept"),
    [
        # Only a job claim since the end: contact proves nothing about processes.
        (None, True),
        # A report the Controller refused proves nothing either.
        (409, True),
        # An accepted report after the end that left the run failed: no process.
        (204, False),
    ],
)
def test_a_failed_run_is_kept_until_an_accepted_report_after_its_end(
    sessions: sessionmaker[Session], observation: int | None, kept: bool
) -> None:
    old = NOW - timedelta(days=2)
    run = _stopped_run(old, NODE)
    run.state = "failed"
    with sessions.begin() as session:
        session.add(run)
        session.flush()
        session.add(
            RunNode(
                run_id=run.id,
                node_id=NODE,
                rank=0,
                role="entrypoint",
                state="failed",
                port=8000,
                reserved_memory_bytes=1,
                observed_run_generation=2,
                observation_process_running=True,
                observation_observed_at=old,
                updated_at=old,
            )
        )
        # The writer every job claim uses: contact after the run ended.
        record_contact(session, NODE, last_seen_at=old + timedelta(hours=1))
    if observation is not None:
        asyncio.run(_deliver_observations(sessions, observation))
    TerminalHistoryCollector(sessions, clock=lambda: NOW).collect()
    with sessions() as session:
        assert (session.get(RecipeRun, run.id) is not None) is kept


def test_a_normal_pass_takes_only_declared_edges(
    sessions: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    """The last table deleting or keeping a row ends the cycle without a restart."""
    caplog.set_level(logging.INFO, logger=terminal_history_collection.__name__)
    old = NOW - timedelta(days=40)
    with sessions.begin() as session:
        gone = _retired_node(session, old)
        held = _retired_node(session, old)
        session.add(_job(old))
        session.flush()
        session.add(
            AgentCertificate(
                serial="99",
                node_id=held,
                not_before=old - timedelta(days=30),
                not_after=NOW,
                fingerprint="99",
                state="active",
                generation=1,
            )
        )
    collector = TerminalHistoryCollector(sessions, clock=lambda: NOW)
    removed = collector.collect()
    assert removed["agent_nodes"] == 1 and removed["jobs"] == 1
    removed = collector.collect()
    assert not removed
    with sessions() as session:
        assert session.get(AgentNode, gone) is None
        assert session.get(AgentNode, held) is not None
    assert not [
        record
        for record in caplog.records
        if "history.cycle_restarted" in record.getMessage()
    ]


def test_rows_kept_without_a_release_cost_no_statement_per_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kept rows with no release event (yet) are not selected again each pass.

    A failed download that lists objects of its own and a retired node that an
    installation, build, mapping or reservation still names stay. Their keeps
    are part of the indexed selection, so the statements of a pass do not grow
    with how many such rows there are.
    """
    monkeypatch.setattr(
        terminal_history_collection, "time", SimpleNamespace(monotonic=lambda: 0.0)
    )

    def statements_per_pass(kept: int) -> int:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        old = NOW - timedelta(days=40)
        with factory.begin() as session:
            session.add(AgentNode(node_id=NODE, state="active"))
            for index in range(kept):
                digest = _cache_set(session, old)
                node_id = _retired_node(session, old)
                session.flush()
                session.add(
                    ModelCacheSetArtifact(
                        artifact_set_sha256=digest,
                        artifact_key="weights",
                        artifact_sha256=f"{index:064x}",
                        path="model.safetensors",
                    )
                )
                session.add(
                    ResourceReservation(
                        node_id=node_id,
                        kind="disk",
                        resource_key="disk",
                        amount_bytes=1,
                        owner_kind="job",
                        owner_id=str(uuid.uuid4()),
                        state="active",
                        plan_digest="a" * 64,
                        created_at=old,
                    )
                )
        executed: list[str] = []

        def count(
            _connection: Connection,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: ExecutionContext,
            _executemany: bool,
        ) -> None:
            executed.append(statement)

        collector = TerminalHistoryCollector(factory, clock=lambda: NOW)
        event.listen(engine, "after_cursor_execute", count)
        try:
            assert not collector.collect()
        finally:
            event.remove(engine, "after_cursor_execute", count)
            engine.dispose()
        return len(executed)

    assert statements_per_pass(5) == statements_per_pass(100)


def test_the_newest_sync_runs_stay_and_older_ones_go(
    sessions: sessionmaker[Session],
) -> None:
    with sessions.begin() as session:
        _sync_run(session, NOW - timedelta(days=5))
        newest = _sync_run(session, NOW - timedelta(days=4))
        _sync_run(session, NOW - timedelta(days=4, hours=1), state="failed")
        newer_failure = _sync_run(session, NOW - timedelta(days=3), state="failed")
    TerminalHistoryCollector(sessions, clock=lambda: NOW).collect()
    with sessions() as session:
        assert set(session.scalars(select(RecipeLibrarySyncRun.id))) == {
            newest,
            newer_failure,
        }


def test_a_cache_set_that_lists_its_own_object_is_never_removed(
    sessions: sessionmaker[Session],
) -> None:
    old = NOW - timedelta(days=2)
    with sessions.begin() as session:
        own, shared, holder = (_cache_set(session, old) for _ in range(3))
        session.flush()
        session.add_all(
            (
                ModelCacheSetArtifact(
                    artifact_set_sha256=own,
                    artifact_key="weights",
                    artifact_sha256="1" * 64,
                    path="model.safetensors",
                ),
                ModelCacheSetArtifact(
                    artifact_set_sha256=shared,
                    artifact_key="weights",
                    artifact_sha256="2" * 64,
                    path="model.safetensors",
                ),
                ModelCacheSetArtifact(
                    artifact_set_sha256=holder,
                    artifact_key="weights",
                    artifact_sha256="2" * 64,
                    path="model.safetensors",
                ),
                ModelCacheSetArtifact(
                    artifact_set_sha256=holder,
                    artifact_key="config",
                    artifact_sha256="3" * 64,
                    path="config.json",
                ),
            )
        )
    TerminalHistoryCollector(sessions, clock=lambda: NOW).collect()
    with sessions() as session:
        remaining = set(session.scalars(select(ModelCacheSet.artifact_set_sha256)))
    # ``shared`` lists only an object ``holder`` lists too: dropping it frees
    # nothing. ``own`` and ``holder`` each list an object nobody else does.
    assert remaining == {own, holder}


def test_a_referenced_certificate_and_an_issuing_enrollment_are_kept(
    sessions: sessionmaker[Session],
) -> None:
    old = NOW - timedelta(days=2)
    with sessions.begin() as session:
        serial = _certificate(session, old)
        grant = _grant(session, old)
        session.flush()
        session.add(
            AgentEnrollment(
                grant_id=grant,
                node_id=NODE,
                state="issuing",
                csr_pem="csr",
                csr_public_key_pem="key",
                csr_public_key_fingerprint="f" * 64,
                host_key_fingerprint="host",
                hardware_fingerprint="hardware",
                agent_digest="agent",
                boot_id="boot",
                created_at=old,
                certificate_serial=serial,
            )
        )
    collector = TerminalHistoryCollector(sessions, clock=lambda: NOW)
    collector.collect()
    with sessions() as session:
        assert session.get(AgentCertificate, serial) is not None
        assert session.get(AgentEnrollmentGrant, grant) is not None
    # The enrollment ends; grant, enrollment and then the certificate follow.
    with sessions.begin() as session:
        enrollment = session.scalar(select(AgentEnrollment))
        assert enrollment is not None
        enrollment.state = "ended"
    # A kept row is revisited when its table's cursor comes round again, and
    # the certificate's turn in a pass precedes the grant's.
    for _ in range(2):
        TerminalHistoryCollector(sessions, clock=lambda: NOW).collect()
    with sessions() as session:
        assert session.scalar(select(AgentEnrollment.id)) is None
        assert session.get(AgentEnrollmentGrant, grant) is None
        assert session.get(AgentCertificate, serial) is None


def test_a_retired_node_goes_with_its_children_after_its_certificates(
    sessions: sessionmaker[Session],
) -> None:
    """With foreign keys enforced, children must go first and certificates gate."""
    old = NOW - timedelta(days=40)
    with sessions() as session:
        session.execute(text("PRAGMA foreign_keys=ON"))
    with sessions.begin() as session:
        node_id = _retired_node(session, old)
        session.flush()
        session.add(AgentNodeContact(node_id=node_id, last_seen_at=old))
        session.add(
            AgentNodeProfile(node_id=node_id, display_name="spark", lifecycle="retired")
        )
        session.add(
            AgentCertificate(
                serial="77",
                node_id=node_id,
                not_before=old - timedelta(days=30),
                not_after=old,
                fingerprint="77",
                state="revoked",
                generation=1,
                ca_revoked_at=old,
            )
        )
    TerminalHistoryCollector(sessions, clock=lambda: NOW).collect()
    TerminalHistoryCollector(sessions, clock=lambda: NOW).collect()
    with sessions() as session:
        assert session.get(AgentNode, node_id) is None
        assert session.get(AgentNodeContact, node_id) is None
        assert session.get(AgentCertificate, "77") is None


def test_an_open_gate_is_deleted_where_its_removal_ends(
    sessions: sessionmaker[Session],
) -> None:
    identity = ArtifactIdentity("model-set", "d" * 64)
    owner, fence = str(uuid.uuid4()), str(uuid.uuid4())
    with sessions.begin() as session:
        reserve_removal(
            session,
            (identity,),
            owner_kind="model-cache-operation",
            owner_id=owner,
            fence=fence,
            now=NOW,
        )
    with sessions() as session:
        assert session.scalar(select(ArtifactLifecycleGate.removal_owner_id)) == owner
    with sessions.begin() as session:
        clear_removal(
            session,
            (identity,),
            owner_kind="model-cache-operation",
            owner_id=owner,
            fence=fence,
            now=NOW,
        )
    with sessions() as session:
        assert session.scalar(select(ArtifactLifecycleGate.artifact_sha256)) is None


def test_a_pass_stays_inside_its_budget_on_a_large_backlog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live regression (2026-11-04): passes of 10.8-24 s removed 1-6 rows.

    Every statement costs time on a charged clock. Whatever the backlog, one
    pass ends within its budget plus one row's recheck, its reference
    inventory stays one statement per table, and it removes a bounded batch.
    """
    elapsed = SimpleNamespace(seconds=0.0)
    monkeypatch.setattr(
        terminal_history_collection,
        "time",
        SimpleNamespace(monotonic=lambda: elapsed.seconds),
    )
    statements: list[str] = []

    def charge(
        _connection: Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: ExecutionContext,
        _executemany: bool,
    ) -> None:
        statements.append(statement)
        elapsed.seconds += 0.01

    def one_pass(backlog: int) -> tuple[float, int, list[str]]:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        old = NOW - timedelta(days=3)
        with factory.begin() as session:
            session.add(AgentNode(node_id=NODE, state="active"))
            session.add_all(_job(old) for _ in range(backlog))
            # The live and recent rows the inventory must read: fixed.
            session.add_all(_job(NOW, state="running") for _ in range(5))
        assert isinstance(engine, Engine)
        event.listen(engine, "after_cursor_execute", charge)
        statements.clear()
        elapsed.seconds = 0.0
        try:
            removed = TerminalHistoryCollector(factory, clock=lambda: NOW).collect()
        finally:
            event.remove(engine, "after_cursor_execute", charge)
            engine.dispose()
        return elapsed.seconds, removed["jobs"], list(statements)

    small = one_pass(200)
    large = one_pass(2_000)
    # The inventory's statement count does not grow with the backlog.
    inventory = "SELECT jobs.id, jobs.kind, jobs.payload \nFROM jobs"
    inventory_small = [s for s in small[2] if s.startswith(inventory)]
    inventory_large = [s for s in large[2] if s.startswith(inventory)]
    assert len(inventory_small) == len(inventory_large) == 1
    for seconds, removed, _ in (small, large):
        assert removed >= 1
        # The budget starts after the inventory; one row's recheck may overrun.
        assert seconds <= PASS_BUDGET_SECONDS + 1.0
    assert large[1] <= small[1] + 32
