from __future__ import annotations

import select
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import QueuePool
from vonk_control import db

from .test_database_adoption_recovery_postgres import (
    CONFIG,
    StartupDatabase,
    assert_retained_and_current,
    legacy_engine,
    observe_retry,
    startup_database,
    storage_engine,
    watch_native_attempts,
)
from .test_exact_integer_storage import _schema

__all__ = ["legacy_engine", "startup_database", "storage_engine"]
pytestmark = pytest.mark.postgres


@contextmanager
def silent_then_forwarding_peer(
    target: tuple[str, int],
) -> Iterator[tuple[int, threading.Event, threading.Event]]:
    """An actual TCP peer: accept a PG handshake silently, then forward retries."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.1)
    port = listener.getsockname()[1]
    stopped = threading.Event()
    repaired = threading.Event()
    handshake_seen = threading.Event()
    workers: list[threading.Thread] = []
    failures: list[BaseException] = []

    def serve(client: socket.socket) -> None:
        try:
            with client:
                client.settimeout(0.1)
                if not repaired.is_set():
                    # Receive genuine libpq handshake bytes but never answer.
                    # The driver, rather than the fixture, must end this wait.
                    while not stopped.is_set() and not repaired.is_set():
                        try:
                            data = client.recv(8192)
                        except TimeoutError:
                            continue
                        if not data:
                            return
                        handshake_seen.set()
                    return
                with socket.create_connection(target, timeout=2) as upstream:
                    upstream.settimeout(0.5)
                    client.settimeout(0.5)
                    while not stopped.is_set():
                        readable, _, _ = select.select([client, upstream], [], [], 0.1)
                        for source in readable:
                            data = source.recv(8192)
                            if not data:
                                return
                            destination = upstream if source is client else client
                            destination.sendall(data)
        except OSError as error:
            if not stopped.is_set():
                failures.append(error)

    def accept() -> None:
        while not stopped.is_set():
            try:
                client, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError as error:
                if not stopped.is_set():
                    failures.append(error)
                return
            worker = threading.Thread(target=serve, args=(client,), daemon=True)
            workers.append(worker)
            worker.start()

    accepting = threading.Thread(target=accept, daemon=True)
    accepting.start()
    try:
        yield port, repaired, handshake_seen
    finally:
        stopped.set()
        repaired.set()
        listener.close()
        accepting.join(timeout=2)
        assert not accepting.is_alive()
        for worker in workers:
            worker.join(timeout=3)
            assert not worker.is_alive()
        assert not failures


def test_initialize_database_silent_handshake_timeout_releases_attempt_then_same_call_recovers(
    startup_database: StartupDatabase,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Catches unbounded first connect, leaked failed pools or a required second call."""
    database = startup_database
    host = database.engine.url.host
    port = database.engine.url.port
    assert host is not None and port is not None
    before = _schema(database.engine)
    monkeypatch.setattr(
        db,
        "DATABASE_WAIT_BUDGETS",
        replace(db.DATABASE_WAIT_BUDGETS, connect_timeout_seconds=2),
    )
    with silent_then_forwarding_peer((host, port)) as (
        peer_port,
        repaired,
        handshake_seen,
    ):
        same_url = database.engine.url.set(host="127.0.0.1", port=peer_port)
        disposed: list[Engine] = []
        authenticated: list[Engine] = []
        waits: list[float] = []

        def engine_disposed(engine: Engine) -> None:
            if engine.url == same_url:
                assert isinstance(engine.pool, QueuePool)
                disposed.append(engine)
                assert engine.pool.checkedout() == 0

        def engine_connected(connection: Connection) -> None:
            if connection.engine.url == same_url:
                authenticated.append(connection.engine)

        def repair_before_backoff(wait: float) -> None:
            waits.append(wait)
            assert len(waits) == 1
            assert handshake_seen.is_set() and disposed and not authenticated
            # Bound the failed physical connection, independently of normal
            # migration's own statement/transaction budgets after admission.
            assert 2 <= time.monotonic() - started < 5
            assert all(engine.url == same_url for engine in disposed)
            assert _schema(database.engine) == before
            with database.engine.connect() as probe:
                assert (
                    probe.exec_driver_sql(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=%s AND application_name='vonk:control'",
                        (database.engine.url.database,),
                    ).scalar_one()
                    == 0
                )
            repaired.set()
            time.sleep(wait)

        observe_retry(monkeypatch, timeout=8, sleep=repair_before_backoff)
        event.listen(Engine, "engine_disposed", engine_disposed)
        event.listen(Engine, "engine_connect", engine_connected)
        started = time.monotonic()
        try:
            # Same public call and same URL/credentials; only the actual peer
            # changes from silent handshake to transparent PG transport.
            with watch_native_attempts() as attempts:
                db.initialize_database(
                    same_url.render_as_string(hide_password=False), config_path=CONFIG
                )
                attempts.assert_released(database.engine)
                assert attempts.migration_pids
            assert len(waits) == 1
            assert len(disposed) == 2 and authenticated
            assert set(authenticated) == {disposed[-1]}
            assert all(engine.url == same_url for engine in disposed + authenticated)
        finally:
            event.remove(Engine, "engine_disposed", engine_disposed)
            event.remove(Engine, "engine_connect", engine_connected)
    diagnostics = capsys.readouterr().err
    assert "connection establishment cause unknown" in diagnostics
    assert "deadline expired" not in diagnostics
    assert_retained_and_current(database)
