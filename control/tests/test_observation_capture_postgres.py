"""Real SQL capture/release/repair, without claiming a whole-snapshot RAM bound."""

from __future__ import annotations

import asyncio
import io
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker
from starlette.types import Message, Scope
from vonk_agent_protocol import canonical_message
from vonk_control import db, observation_transfer
from vonk_control.api import create_app
from vonk_control.auth import Actor, TokenCodec
from vonk_control.fleet_projection import FleetNode, FleetProjection, FleetSnapshot
from vonk_control.models import AgentNode, AgentNodeProfile, Base
from vonk_control.observation_transfer import ObservationTransferRecord
from vonk_control.platform_observation import PlatformObservation, PlatformObserver
from vonk_control.strict_json import serialize_json_value

from cluster_profiles.control_limits import MAX_CONTROL_DOCUMENT_BYTES
from cluster_profiles.observation_transfer_reader import receive_observation
from tests.observation_transfer_peer import observation_document
from tests.test_platform_observation import Jobs

NODE = "spk_" + "1" * 32


def _seed(engine):
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            AgentNode(node_id=NODE, state="active", last_seen_at=datetime.now(UTC))
        )
        session.flush()
        session.add(AgentNodeProfile(node_id=NODE, display_name="before"))
    return sessions


def _worker_process(engine) -> str:
    """A fresh interpreter runs the actual readiness owner, not a seeded DTO row.

    This proves completed-loop observation provenance; it does not claim a full
    production scheduler pass or physical Spark execution.
    """
    instance = uuid4().hex + uuid4().hex
    environment = os.environ.copy()
    environment["VONK_TEST_OBSERVATION_DATABASE_URL"] = engine.url.render_as_string(
        hide_password=False
    )
    environment["VONK_TEST_OBSERVATION_INSTANCE"] = instance
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os
from datetime import UTC, datetime
from sqlalchemy.orm import sessionmaker
from vonk_control.db import build_engine
from vonk_control.worker import WorkerHeartbeatRecorder
engine = build_engine(os.environ['VONK_TEST_OBSERVATION_DATABASE_URL'], component='observation-worker-proof')
try:
    recorder = WorkerHeartbeatRecorder(sessionmaker(engine, expire_on_commit=False),
        process_instance_id=os.environ['VONK_TEST_OBSERVATION_INSTANCE'], clock=lambda: datetime.now(UTC))
    recorder.completed_loop()
finally:
    engine.dispose()
""",
        ],
        env=environment,
        check=True,
        timeout=30,
    )
    return instance


def _app(sessions):
    codec = TokenCodec(b"k" * 32)
    token = codec.issue(Actor("viewer", "viewer"), ttl_seconds=100, now=0)
    return create_app(
        jobs=Jobs(),
        tokens=codec,
        now=lambda: 10,
        fleet_projection=FleetProjection(sessions, clock=lambda: datetime.now(UTC)),
        platform_observer=PlatformObserver(sessions, clock=lambda: datetime.now(UTC)),
    ), {"Authorization": f"Bearer {token}"}


def _received(body: bytes, resource: str):
    def validate_payload(value):
        model = FleetSnapshot if resource == "fleet" else PlatformObservation
        document = serialize_json_value(
            model.model_validate_json(canonical_message(value), strict=True)
        )
        assert isinstance(document, dict)
        return document

    def validate_record(value: object) -> None:
        ObservationTransferRecord.model_validate_json(
            canonical_message(value), strict=True
        )

    return receive_observation(
        io.BytesIO(body),
        resource=resource,
        record_max_bytes=MAX_CONTROL_DOCUMENT_BYTES,
        deadline=time.monotonic() + 10,
        validate_record=validate_record,
        validate_payload=validate_payload,
    )


def test_fleet_cursor_and_membership_are_one_real_sql_snapshot(postgres_engine):
    sessions = _seed(postgres_engine)
    projection = FleetProjection(sessions, clock=lambda: datetime.now(UTC))
    changed = False

    def commit_between_selects(
        _connection, _cursor, statement, _parameters, _context, _many
    ):
        nonlocal changed
        if not changed and "select fleet_event_cursor.last_id" in statement.lower():
            changed = True
            # A distinct checked-out connection commits a real mutation after
            # the reader established its snapshot but before membership reads.
            projection.update_display_name(NODE, "after")

    event.listen(postgres_engine, "after_cursor_execute", commit_between_selects)
    try:
        captured = projection.read()
    finally:
        event.remove(postgres_engine, "after_cursor_execute", commit_between_selects)
    assert changed, "the event-cursor capture boundary must actually execute"
    assert captured.event_cursor == 0
    assert captured.nodes[0].display_name == "before", "capture mixed SQL generations"
    current = projection.read()
    assert current.event_cursor > captured.event_cursor
    assert current.nodes[0].display_name == "after"


@pytest.mark.parametrize(
    "resource,table",
    [("fleet", "agent_nodes"), ("platform", "control_process_heartbeats")],
)
def test_slow_stream_releases_sql_and_keeps_original_observation(
    postgres_engine, monkeypatch, resource, table
):
    _seed(postgres_engine)
    first_worker = _worker_process(postgres_engine)
    engine = db.build_engine(
        postgres_engine.url.render_as_string(hide_password=False),
        component="observation-proof",
    )
    sessions = sessionmaker(engine, expire_on_commit=False)
    app, headers = _app(sessions)
    first = observation_document(
        TestClient(app).get(f"/api/{resource}", headers=headers)
    )

    record_threads: list[int] = []
    real_record = observation_transfer._record

    def observe_record(record):
        record_threads.append(threading.get_ident())
        return real_record(record)

    monkeypatch.setattr(observation_transfer, "_record", observe_record)

    async def exercise():
        loop_thread = threading.get_ident()
        paused, resume = asyncio.Event(), asyncio.Event()
        messages: list[Message] = []

        async def receive() -> Message:
            await asyncio.Event().wait()
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            messages.append(message)
            if message["type"] == "http.response.body" and not paused.is_set():
                paused.set()
                await resume.wait()

        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": f"/api/{resource}",
            "raw_path": f"/api/{resource}".encode(),
            "query_string": b"",
            "root_path": "",
            "server": ("test", 80),
            "client": ("127.0.0.1", 1),
            "headers": [
                (key.lower().encode(), value.encode()) for key, value in headers.items()
            ],
        }
        task = asyncio.create_task(app(scope, receive, send))
        try:
            await asyncio.wait_for(paused.wait(), 10)
            with postgres_engine.connect() as connection:
                active = connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'vonk:observation-proof' AND xact_start IS NOT NULL"
                    )
                )
                assert active == 0, "SQL transaction survived first network suspension"
                # A read transaction would retain ACCESS SHARE until closed.
                # NOWAIT detects that actual table lock, rather than a mock depth.
                connection.execute(
                    text(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE NOWAIT")
                )
                connection.rollback()
            FleetProjection(sessions).update_display_name(NODE, "after")
            second_worker = await asyncio.to_thread(_worker_process, postgres_engine)
            unrelated = await asyncio.to_thread(
                TestClient(app).get,
                "/api/platform" if resource == "fleet" else "/api/fleet",
                headers=headers,
            )
            assert unrelated.status_code == 200
            resume.set()
            await asyncio.wait_for(task, 10)
            assert messages[0]["status"] == 200
            body = b"".join(
                message.get("body", b"")
                for message in messages
                if message["type"] == "http.response.body"
            )
            assert record_threads and loop_thread not in record_threads, (
                "record encoding blocked the async event loop"
            )
            frozen = _received(body, resource)
            if resource == "fleet":
                assert (
                    FleetSnapshot.model_validate_json(
                        canonical_message(frozen), strict=True
                    )
                    .nodes[0]
                    .display_name
                    == FleetSnapshot.model_validate_json(
                        canonical_message(first), strict=True
                    )
                    .nodes[0]
                    .display_name
                    == "before"
                )
                assert frozen["event_cursor"] == first["event_cursor"]
            else:
                workers = PlatformObservation.model_validate_json(
                    canonical_message(frozen), strict=True
                ).workers
                assert workers is not None
                assert [worker.process_instance_id for worker in workers] == [
                    first_worker
                ]
            repaired = await asyncio.to_thread(
                TestClient(app).get, f"/api/{resource}", headers=headers
            )
            current = observation_document(repaired)
            if resource == "fleet":
                assert (
                    FleetSnapshot.model_validate_json(
                        canonical_message(current), strict=True
                    )
                    .nodes[0]
                    .display_name
                    == "after"
                )
                assert (
                    FleetSnapshot.model_validate_json(
                        canonical_message(current), strict=True
                    ).event_cursor
                    > FleetSnapshot.model_validate_json(
                        canonical_message(frozen), strict=True
                    ).event_cursor
                )
            else:
                workers = PlatformObservation.model_validate_json(
                    canonical_message(current), strict=True
                ).workers
                assert workers is not None
                assert {worker.process_instance_id for worker in workers} == {
                    first_worker,
                    second_worker,
                }
        finally:
            resume.set()
            # wait_for owns cancellation/reaping of this exact ASGI task if
            # the failed assertion left it suspended; preserve the first error.
            await asyncio.gather(asyncio.wait_for(task, 10), return_exceptions=True)

    try:
        asyncio.run(exercise())
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "resource,table",
    [
        ("fleet", "agent_nodes"),
        (f"fleet/{NODE}", "agent_nodes"),
        ("platform", "control_process_heartbeats"),
        ("cli/contract", "control_process_heartbeats"),
    ],
)
def test_real_sql_capture_timeout_is_retryable_and_same_read_repairs(
    postgres_engine, monkeypatch, resource, table
):
    _seed(postgres_engine)
    worker = _worker_process(postgres_engine)
    # Narrow only the test connection's existing owning wait budgets. There is
    # no new production bound or sleep, and the fault is a real server lock.
    monkeypatch.setattr(
        db,
        "DATABASE_WAIT_BUDGETS",
        replace(
            db.DATABASE_WAIT_BUDGETS, lock_timeout_ms=250, statement_timeout_ms=250
        ),
    )
    engine = db.build_engine(
        postgres_engine.url.render_as_string(hide_password=False),
        component="observation-timeout-proof",
    )
    app, headers = _app(sessionmaker(engine, expire_on_commit=False))
    peer = TestClient(app)
    try:
        with postgres_engine.connect() as locker:
            locker.execute(text(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE"))
            response = peer.get(f"/api/{resource}", headers=headers)
            assert response.status_code == 503
            assert response.headers["retry-after"] == "5"
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["content-type"] == "application/json"
            assert "nodes" not in response.json() and "workers" not in response.json()
            # The lock applies only to this observation's dependency; unrelated
            # work does not wait behind the failed capture transaction.
            other = peer.get(
                "/api/platform" if table == "agent_nodes" else "/api/fleet",
                headers=headers,
            )
            assert other.status_code == 200
            locker.rollback()
        repaired = peer.get(f"/api/{resource}", headers=headers)
        assert repaired.status_code == 200
        if resource == f"fleet/{NODE}":
            assert (
                FleetNode.model_validate_json(repaired.content, strict=True).id == NODE
            )
            return
        if resource == "cli/contract":
            from vonk_control.cli_update_contract import CliUpdateContract

            contract = CliUpdateContract.model_validate_json(
                repaired.content, strict=True
            )
            assert contract.worker_count == 1
            return
        document = observation_document(repaired)
        if resource == "fleet":
            assert (
                FleetSnapshot.model_validate_json(
                    canonical_message(document), strict=True
                )
                .nodes[0]
                .id
                == NODE
            )
        else:
            workers = PlatformObservation.model_validate_json(
                canonical_message(document), strict=True
            ).workers
            assert workers is not None
            assert [row.process_instance_id for row in workers] == [worker]
    finally:
        engine.dispose()
