from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
import types
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest

ROOT = Path(__file__).resolve().parents[3]
SUPERVISOR = ROOT / "deploy/compose/litellm/config_supervisor.py"


def _module():
    spec = importlib.util.spec_from_file_location(
        "litellm_config_supervisor", SUPERVISOR
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_supervisor_allows_first_run_database_migrations() -> None:
    module = _module()

    assert module.STARTUP_SECONDS == 120


def test_supervisor_recovers_from_a_transient_pre_health_child_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    bootstrap = tmp_path / "bootstrap.json"
    bootstrap.write_text('{"model_list":[]}\n')
    module.ACK_ROOT = tmp_path / "supervisor"
    module.ACK = module.ACK_ROOT / "ack.json"
    module.ACK_ROOT.mkdir()
    module.ACK.write_text("stale\n")
    authority = module._RouteLeaseAuthority()
    authority.allow_bootstrap()

    class Child:
        def __init__(self, *, pid: int, returncode: int) -> None:
            self.pid = pid
            self.returncode = returncode

        def poll(self) -> int:
            return self.returncode

    children = iter(
        (
            Child(pid=101, returncode=70),
            Child(pid=102, returncode=23),
        )
    )
    health = iter((False, True))
    spawns: list[Child] = []
    selections = 0
    retry_delays: list[float] = []
    authorization_at_cleanup: list[bool] = []
    original_clear_ack = module._clear_ack

    def spawn(*_args, **_kwargs) -> Child:
        child = next(children)
        spawns.append(child)
        return child

    def clear_ack() -> None:
        authorization_at_cleanup.append(authority.authorized(datetime.now(UTC)))
        original_clear_ack()

    def active_request(**_kwargs):
        nonlocal selections
        selections += 1

    def retry_sleep(seconds: float) -> None:
        assert authority.authorized(datetime.now(UTC)) is False
        assert not module.ACK.exists()
        retry_delays.append(seconds)

    monkeypatch.setattr(module, "_active_request", active_request)
    monkeypatch.setattr(module, "_selected", lambda **_kwargs: bootstrap)
    monkeypatch.setattr(module, "_await_healthy", lambda _child: next(health))
    monkeypatch.setattr(module, "_clear_ack", clear_ack)
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    monkeypatch.setattr(module.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(module.time, "sleep", retry_sleep)

    assert module._supervise(authority) == 23
    assert [child.pid for child in spawns] == [101, 102]
    assert selections == 2
    assert retry_delays == [1]
    assert authorization_at_cleanup[0] is False
    assert authority.authorized(datetime.now(UTC)) is False
    assert not module.ACK.exists()


def test_supervisor_bounds_pre_health_child_exit_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    bootstrap = tmp_path / "bootstrap.json"
    bootstrap.write_text('{"model_list":[]}\n')
    module.ACK_ROOT = tmp_path / "supervisor"
    module.ACK = module.ACK_ROOT / "ack.json"
    authority = module._RouteLeaseAuthority()
    spawns = 0
    retry_delays: list[float] = []

    class Child:
        pid = 101
        returncode = 70

        @staticmethod
        def poll() -> int:
            return 70

    def spawn(*_args, **_kwargs) -> Child:
        nonlocal spawns
        spawns += 1
        return Child()

    monkeypatch.setattr(module, "_active_request", lambda **_kwargs: None)
    monkeypatch.setattr(module, "_selected", lambda **_kwargs: bootstrap)
    monkeypatch.setattr(module, "_await_healthy", lambda _child: False)
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    monkeypatch.setattr(module.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(module.time, "sleep", retry_delays.append)

    assert module._supervise(authority) == 1
    assert spawns == 10
    assert retry_delays == [1] * 9
    assert authority.authorized(datetime.now(UTC)) is False


def test_supervisor_does_not_retry_a_live_child_after_the_health_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    bootstrap = tmp_path / "bootstrap.json"
    bootstrap.write_text('{"model_list":[]}\n')
    module.ACK_ROOT = tmp_path / "supervisor"
    module.ACK = module.ACK_ROOT / "ack.json"
    authority = module._RouteLeaseAuthority()
    spawns = 0

    class Child:
        pid = 101
        returncode = None
        terminated = False

        def poll(self):
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = 0

        def wait(self, timeout=None):
            del timeout
            return self.returncode

    child = Child()

    def spawn(*_args, **_kwargs) -> Child:
        nonlocal spawns
        spawns += 1
        return child

    monkeypatch.setattr(module, "_active_request", lambda **_kwargs: None)
    monkeypatch.setattr(module, "_selected", lambda **_kwargs: bootstrap)
    monkeypatch.setattr(module, "_await_healthy", lambda _child: False)
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    monkeypatch.setattr(module.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(
        module.time,
        "sleep",
        lambda _seconds: pytest.fail("live unhealthy child was retried"),
    )

    assert module._supervise(authority) == 1
    assert spawns == 1
    assert child.terminated is True
    assert authority.authorized(datetime.now(UTC)) is False


def test_route_lease_authority_enforces_immutable_exact_expiry(
    tmp_path: Path,
) -> None:
    module = _module()
    now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    expires_at = now + timedelta(seconds=1)
    _bundle(module, tmp_path, now=now, expires_at=expires_at)
    request = module._active_request(now=now)
    assert request is not None
    authority = module._RouteLeaseAuthority()

    authority.deny()
    assert authority.authorized(now) is False
    authority.allow_bootstrap()
    assert authority.authorized(now) is True
    authority.activate(request)
    request.marker["expires_at"] = now.isoformat()
    assert authority.authorized(expires_at - timedelta(microseconds=1)) is True
    assert authority.authorized(expires_at) is False
    assert authority.authorized(now.replace(tzinfo=None)) is False
    authority.activate(types.SimpleNamespace(marker={}))
    assert authority.authorized(now) is False


def _route_lease_status(
    server: object,
    *,
    path: str = "/vonk/route-lease",
    method: str = "GET",
) -> tuple[int, bytes, object]:
    host, port = server.server_address
    request = urllib.request.Request(f"http://{host}:{port}{path}", method=method)
    try:
        response = urllib.request.urlopen(request, timeout=1)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers
    with response:
        return response.status, response.read(), response.headers


def _raw_route_lease_response(server: object, request: bytes) -> bytes:
    host, port = server.server_address
    with socket.create_connection((host, port), timeout=2) as connection:
        connection.sendall(request)
        connection.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while chunk := connection.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)


@pytest.mark.parametrize(
    "raw_request",
    (
        pytest.param(
            b"GET /" + b"a" * 65536 + b" HTTP/1.1\r\nHost: x\r\n\r\n",
            id="overlong-request-line",
        ),
        pytest.param(
            b"GET / extra HTTP/1.1\r\nHost: x\r\n\r\n",
            id="malformed-request-line",
        ),
        pytest.param(
            b"GET / HTTP/nope\r\nHost: x\r\n\r\n",
            id="malformed-http-version",
        ),
        pytest.param(
            b"GET / HTTP/1.1\r\nX: " + b"a" * 65536 + b"\r\n\r\n",
            id="overlong-header-line",
        ),
    ),
)
def test_route_lease_http_maps_parser_errors_to_empty_404(
    raw_request: bytes,
) -> None:
    module = _module()
    server = module._start_route_lease_server(
        module._RouteLeaseAuthority(),
        host="127.0.0.1",
        port=0,
    )
    try:
        response = _raw_route_lease_response(server, raw_request)
    finally:
        server.shutdown()
        server.server_close()

    status, separator, remainder = response.partition(b"\r\n")
    headers, header_separator, body = remainder.partition(b"\r\n\r\n")
    assert status == b"HTTP/1.1 404 Not Found"
    assert separator == b"\r\n"
    assert header_separator == b"\r\n\r\n"
    assert b"Cache-Control: no-store\r\n" in headers + b"\r\n"
    assert b"Content-Length: 0\r\n" in headers + b"\r\n"
    assert b"Server:" not in headers
    assert body == b""


def test_route_lease_http_suppresses_interim_continue() -> None:
    module = _module()
    authority = module._RouteLeaseAuthority()
    authority.allow_bootstrap()
    server = module._start_route_lease_server(
        authority,
        host="127.0.0.1",
        port=0,
    )
    try:
        response = _raw_route_lease_response(
            server,
            b"GET /vonk/route-lease HTTP/1.1\r\n"
            b"Host: x\r\n"
            b"Connection: close\r\n"
            b"Expect: 100-continue\r\n"
            b"Content-Length: 1\r\n\r\n",
        )
    finally:
        server.shutdown()
        server.server_close()

    status, separator, remainder = response.partition(b"\r\n")
    headers, header_separator, body = remainder.partition(b"\r\n\r\n")
    assert response.count(b"HTTP/1.1 ") == 1
    assert status == b"HTTP/1.1 404 Not Found"
    assert separator == b"\r\n"
    assert header_separator == b"\r\n\r\n"
    assert b"Cache-Control: no-store\r\n" in headers + b"\r\n"
    assert b"Content-Length: 0\r\n" in headers + b"\r\n"
    assert b"Server:" not in headers
    assert body == b""


def test_route_lease_http_fails_closed_without_metadata_or_server_banner(
    tmp_path: Path,
) -> None:
    module = _module()
    now = datetime.now(UTC)
    _bundle(
        module,
        tmp_path,
        now=now - timedelta(seconds=1),
        expires_at=now + timedelta(seconds=30),
    )
    request = module._active_request(now=now)
    assert request is not None
    authority = module._RouteLeaseAuthority()
    server = module._start_route_lease_server(
        authority,
        host="127.0.0.1",
        port=0,
    )
    try:
        status, body, headers = _route_lease_status(server)
        assert (status, body) == (503, b"")
        assert headers["Cache-Control"] == "no-store"
        assert "Server" not in headers

        authority.activate(request)
        status, body, headers = _route_lease_status(server)
        assert (status, body) == (204, b"")
        assert headers["Cache-Control"] == "no-store"
        assert request.activation_sha256.encode() not in body

        assert _route_lease_status(server, path="/health")[0] == 404
        assert _route_lease_status(server, method="POST")[0] == 404
        status, body, headers = _route_lease_status(server, method="BREW")
        assert (status, body) == (404, b"")
        assert "Server" not in headers

        authority.deny()
        assert _route_lease_status(server)[0] == 503
    finally:
        server.shutdown()
        server.server_close()


def _bundle(
    module,
    tmp_path: Path,
    *,
    now: datetime,
    expires_at: datetime,
    generation: int = 1,
):
    root = tmp_path / "routes"
    config = b'{"model_list":[{"model_name":"chat"}]}\n'
    routes = b'{"routes":{"chat":{}},"state":"published"}\n'
    manifest = {
        "schema_version": 1,
        "generation": generation,
        "state": "published",
        "reconciliation_id": "bb7aac18-edbf-4cc1-bafd-15e282557c53",
        "plan_digest": "a" * 64,
        "evidence_set_digest": "b" * 64,
        "routes_sha256": hashlib.sha256(routes).hexdigest(),
        "litellm_sha256": hashlib.sha256(config).hexdigest(),
        "issued_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    manifest_bytes = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    directory_name = f"{generation:08d}-{manifest_digest}"
    directory = root / "generations" / directory_name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "litellm.json").write_bytes(config)
    (directory / "routes.json").write_bytes(routes)
    (directory / "manifest.json").write_bytes(manifest_bytes)
    activation = {
        **manifest,
        "directory": directory_name,
        "manifest_sha256": manifest_digest,
    }
    (root / "activation.json").write_text(
        json.dumps(activation, sort_keys=True, separators=(",", ":")) + "\n"
    )
    bootstrap = tmp_path / "bootstrap.json"
    bootstrap.write_bytes(b'{"model_list":[]}\n')
    module.ROOT = root
    module.ACTIVATION = root / "activation.json"
    module.GENERATIONS = root / "generations"
    module.BOOTSTRAP = bootstrap
    return directory / "litellm.json", bootstrap, directory


def test_supervisor_selects_only_an_exact_fresh_activation_bundle(
    tmp_path: Path,
) -> None:
    module = _module()
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    generated, _bootstrap, _directory = _bundle(
        module,
        tmp_path,
        now=now - timedelta(seconds=30),
        expires_at=now + timedelta(seconds=120),
    )

    assert module._selected(now=now) == generated


def test_supervisor_falls_back_for_expired_or_hash_mismatched_bundle(
    tmp_path: Path,
) -> None:
    module = _module()
    now = datetime(2026, 8, 5, 12, 3, tzinfo=UTC)
    generated, bootstrap, _directory = _bundle(
        module,
        tmp_path,
        now=now - timedelta(seconds=180),
        expires_at=now - timedelta(seconds=30),
    )
    assert module._selected(now=now) == bootstrap

    generated, bootstrap, _directory = _bundle(
        module,
        tmp_path,
        now=now,
        expires_at=now + timedelta(seconds=150),
    )
    generated.write_bytes(b'{"model_list":[{"unsafe":true}]}\n')
    assert module._selected(now=now) == bootstrap


def test_supervisor_rejects_a_lease_beyond_the_production_bound(
    tmp_path: Path,
) -> None:
    module = _module()
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    _generated, bootstrap, _directory = _bundle(
        module,
        tmp_path,
        now=now,
        expires_at=now + timedelta(seconds=301),
    )

    assert module._selected(now=now) == bootstrap


def test_supervisor_falls_back_when_manifest_or_marker_is_not_exact(
    tmp_path: Path,
) -> None:
    module = _module()
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    _generated, bootstrap, directory = _bundle(
        module,
        tmp_path,
        now=now,
        expires_at=now + timedelta(seconds=150),
    )
    manifest = json.loads((directory / "manifest.json").read_bytes())
    manifest["plan_digest"] = "f" * 64
    (directory / "manifest.json").write_text(json.dumps(manifest))
    assert module._selected(now=now) == bootstrap

    _generated, bootstrap, _directory = _bundle(
        module,
        tmp_path,
        now=now,
        expires_at=now + timedelta(seconds=150),
    )
    activation = json.loads(module.ACTIVATION.read_bytes())
    module.ACTIVATION.write_text(json.dumps(activation, indent=2))
    assert module._selected(now=now) == bootstrap

    _generated, bootstrap, _directory = _bundle(
        module,
        tmp_path,
        now=now,
        expires_at=now + timedelta(seconds=150),
    )
    activation = json.loads(module.ACTIVATION.read_bytes())
    activation["unknown"] = True
    module.ACTIVATION.write_text(json.dumps(activation))
    assert module._selected(now=now) == bootstrap


def test_supervisor_ack_binds_a_live_child_to_the_exact_activation_request(
    tmp_path: Path,
) -> None:
    module = _module()
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    _generated, _bootstrap, _directory = _bundle(
        module,
        tmp_path,
        now=now - timedelta(seconds=1),
        expires_at=now + timedelta(seconds=120),
    )
    module.ACK_ROOT = tmp_path / "supervisor"
    module.ACK = module.ACK_ROOT / "ack.json"

    class Child:
        pid = 123

        @staticmethod
        def poll():
            return None

    request = module._active_request(now=now)
    assert request is not None
    module._write_ack(request, Child(), now=now)

    ack = json.loads(module.ACK.read_bytes())
    assert ack == {
        "acknowledged_at": now.isoformat(),
        "activation_sha256": hashlib.sha256(module.ACTIVATION.read_bytes()).hexdigest(),
        "child_pid": 123,
        "expires_at": (now + timedelta(seconds=120)).isoformat(),
        "generation": 1,
        "litellm_sha256": hashlib.sha256(request.config.read_bytes()).hexdigest(),
        "schema_version": 1,
        "state": "published",
    }
    assert (
        module.ACK.read_bytes()
        == (json.dumps(ack, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )


def test_live_supervisor_removes_ack_when_the_acknowledged_child_crashes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _module()
    now = datetime.now(UTC)
    _bundle(
        module,
        tmp_path,
        now=now - timedelta(seconds=1),
        expires_at=now + timedelta(seconds=120),
    )
    module.ACK_ROOT = tmp_path / "supervisor"
    module.ACK = module.ACK_ROOT / "ack.json"
    request = module._active_request(now=now)
    assert request is not None

    class CrashedChild:
        pid = 321
        returncode = 17

        def __init__(self) -> None:
            self.polls = 0

        def poll(self):
            self.polls += 1
            return None if self.polls == 1 else self.returncode

    child = CrashedChild()
    monkeypatch.setattr(module, "_active_request", lambda **_kwargs: request)
    monkeypatch.setattr(module, "_await_healthy", lambda _child: True)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(module.signal, "signal", lambda *_args: None)

    assert module.main() == 17
    assert not module.ACK.exists()


def test_live_supervisor_stops_published_child_at_exact_lease_expiry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _module()
    issued_at = datetime.now(UTC)
    expires_at = issued_at + timedelta(seconds=120)
    generated, bootstrap, _directory = _bundle(
        module,
        tmp_path,
        now=issued_at,
        expires_at=expires_at,
    )
    module.ACK_ROOT = tmp_path / "supervisor"
    module.ACK = module.ACK_ROOT / "ack.json"
    original_active_request = module._active_request
    original_selected = module._selected
    requests = iter(
        (
            original_active_request(now=issued_at),
            original_active_request(now=expires_at),
        )
    )
    assert original_active_request(now=issued_at) is not None
    assert original_active_request(now=expires_at) is None
    assert original_selected(now=expires_at) == bootstrap

    class LiveChild:
        pid = 654
        returncode = None

        def __init__(self) -> None:
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = 0

        def wait(self, timeout=None):
            del timeout
            return self.returncode

    class BootstrapCrash:
        pid = 987
        returncode = 23

        @staticmethod
        def poll():
            return 23

    published_child = LiveChild()
    bootstrap_child = BootstrapCrash()
    children = iter((published_child, bootstrap_child))
    commands: list[list[str]] = []

    def spawn(command, **_kwargs):
        commands.append(command)
        return next(children)

    monkeypatch.setattr(module, "_active_request", lambda **_kwargs: next(requests))
    monkeypatch.setattr(
        module,
        "_selected",
        lambda **_kwargs: bootstrap,
    )
    monkeypatch.setattr(module, "_await_healthy", lambda _child: True)
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    monkeypatch.setattr(module.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    assert module.main() == 23
    assert published_child.terminated is True
    assert commands[0][2] == str(generated)
    assert commands[1][2] == str(bootstrap)
    assert not module.ACK.exists()


def test_live_supervisor_renews_same_config_route_lease_without_restart(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _module()
    issued_at = datetime.now(UTC)
    old_expires_at = issued_at + timedelta(seconds=120)
    new_expires_at = issued_at + timedelta(seconds=240)
    _bundle(
        module,
        tmp_path,
        now=issued_at,
        expires_at=old_expires_at,
    )
    first_request = module._active_request(now=issued_at)
    _bundle(
        module,
        tmp_path,
        now=issued_at,
        expires_at=new_expires_at,
        generation=2,
    )
    second_request = module._active_request(now=issued_at)
    assert first_request is not None and second_request is not None
    assert first_request.config.read_bytes() == second_request.config.read_bytes()
    module.ACK_ROOT = tmp_path / "supervisor"
    module.ACK = module.ACK_ROOT / "ack.json"

    class Child:
        pid = 456
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -9

        def wait(self, timeout=None):
            del timeout
            return self.returncode

    class Server:
        def shutdown(self) -> None:
            return

        def server_close(self) -> None:
            return

    class Timer:
        instances: ClassVar[list[Timer]] = []

        def __init__(self, interval, function) -> None:
            self.interval = interval
            self.function = function
            self.cancelled = False
            self.daemon = False
            self.instances.append(self)

        def start(self) -> None:
            return

        def cancel(self) -> None:
            self.cancelled = True

    child = Child()
    requests = iter((first_request, second_request))
    authorities = []
    spawn_count = 0
    renewal_observations: dict[str, object] = {}
    renewal_order: list[str] = []
    original_write_ack = module._write_ack
    original_guard = module._ServingLeaseGuard

    def start_server(authority, **_kwargs):
        authorities.append(authority)
        original_activate = authority.activate

        def activate(request) -> None:
            original_activate(request)
            if request.marker["generation"] == 2:
                renewal_order.append("activate")

        authority.activate = activate
        return Server()

    def spawn(*_args, **_kwargs):
        nonlocal spawn_count
        spawn_count += 1
        assert spawn_count == 1, "same-config renewal restarted child"
        return child

    def write_ack(request, loaded_child, *, now):
        original_write_ack(request, loaded_child, now=now)
        if request.marker["generation"] == 2:
            renewal_order.append("ack")
            renewal_observations["authorized_after_old_expiry"] = authorities[
                0
            ].authorized(old_expires_at + timedelta(microseconds=1))
            renewal_observations["ack_generation"] = json.loads(
                module.ACK.read_bytes()
            )["generation"]
            renewal_observations["timers"] = tuple(Timer.instances)
            child.returncode = 19

    class Guard(original_guard):
        def renew(self, request) -> None:
            if request is not None and request.marker["generation"] == 2:
                renewal_order.append("renew")
                assert not authorities[0].authorized(
                    old_expires_at + timedelta(microseconds=1)
                )
            super().renew(request)
            if request is not None and request.marker["generation"] == 2:
                assert authorities[0].authorized(
                    old_expires_at + timedelta(microseconds=1)
                )

        def publish_ack(self, request, *, now) -> None:
            if request.marker["generation"] == 2:
                renewal_order.append("publish")
            super().publish_ack(request, now=now)

    monkeypatch.setattr(module, "_active_request", lambda **_kwargs: next(requests))
    monkeypatch.setattr(module, "_await_healthy", lambda _child: True)
    monkeypatch.setattr(module, "_healthy", lambda _child: True)
    monkeypatch.setattr(module, "_start_route_lease_server", start_server)
    monkeypatch.setattr(module, "_write_ack", write_ack)
    monkeypatch.setattr(module, "_ServingLeaseGuard", Guard)
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    monkeypatch.setattr(module.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(module.threading, "Timer", Timer)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    assert module.main() == 19
    assert spawn_count == 1
    assert renewal_observations["authorized_after_old_expiry"] is True
    assert renewal_observations["ack_generation"] == 2
    assert renewal_order == ["renew", "activate", "publish", "ack"]
    timers = renewal_observations["timers"]
    assert isinstance(timers, tuple) and len(timers) == 2
    assert timers[0].cancelled is True
    assert timers[1].interval > timers[0].interval


def test_same_config_renewal_denies_and_aborts_when_rearm_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _module()
    issued_at = datetime.now(UTC)
    old_expires_at = issued_at + timedelta(seconds=120)
    _bundle(
        module,
        tmp_path,
        now=issued_at,
        expires_at=old_expires_at,
    )
    first_request = module._active_request(now=issued_at)
    _bundle(
        module,
        tmp_path,
        now=issued_at,
        expires_at=issued_at + timedelta(seconds=240),
        generation=2,
    )
    second_request = module._active_request(now=issued_at)
    assert first_request is not None and second_request is not None
    module.ACK_ROOT = tmp_path / "supervisor"
    module.ACK = module.ACK_ROOT / "ack.json"
    authority = module._RouteLeaseAuthority()
    requests = iter((first_request, second_request))
    observed: dict[str, bool] = {}

    class Child:
        pid = 456

        @staticmethod
        def poll():
            return None

    class Guard:
        expired = False

        def __init__(self, _request, child, *, authority) -> None:
            self.authority = authority
            self.child = child

        def start(self) -> None:
            return

        def cancel(self) -> None:
            return

        def publish_ack(self, request, *, now) -> None:
            module._write_ack(request, self.child, now=now)

        def renew(self, request) -> None:
            assert request.marker["generation"] == 2
            self.authority.activate(request)
            observed["new_snapshot_installed"] = self.authority.authorized(
                old_expires_at + timedelta(microseconds=1)
            )
            raise RuntimeError("simulated rearm failure")

    monkeypatch.setattr(module, "_active_request", lambda **_kwargs: next(requests))
    monkeypatch.setattr(module, "_await_healthy", lambda _child: True)
    monkeypatch.setattr(module, "_healthy", lambda _child: True)
    monkeypatch.setattr(module, "_ServingLeaseGuard", Guard)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: Child())
    monkeypatch.setattr(module.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="simulated rearm failure"):
        module._supervise(authority)

    assert observed["new_snapshot_installed"] is True
    assert authority.authorized(issued_at) is False
    assert not module.ACK.exists()


def test_same_config_renewal_invalidates_old_expiry_before_authority_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    issued_at = datetime.now(UTC)
    old_expires_at = issued_at + timedelta(seconds=120)
    _bundle(module, tmp_path, now=issued_at, expires_at=old_expires_at)
    first_request = module._active_request(now=issued_at)
    _bundle(
        module,
        tmp_path,
        now=issued_at,
        expires_at=issued_at + timedelta(seconds=240),
        generation=2,
    )
    second_request = module._active_request(now=issued_at)
    assert first_request is not None and second_request is not None

    class Child:
        killed = False

        @staticmethod
        def poll() -> None:
            return None

        def kill(self) -> None:
            self.killed = True

    class Timer:
        instances: ClassVar[list[Timer]] = []

        def __init__(self, interval, function) -> None:
            self.interval = interval
            self.function = function
            self.cancelled = False
            self.daemon = False
            self.instances.append(self)

        def start(self) -> None:
            return

        def cancel(self) -> None:
            self.cancelled = True

    monkeypatch.setattr(module.threading, "Timer", Timer)
    authority = module._RouteLeaseAuthority()
    authority.activate(first_request)
    child = Child()
    guard = module._ServingLeaseGuard(
        first_request,
        child,
        authority=authority,
        clock=lambda: issued_at,
    )
    guard.start()
    old_timer = Timer.instances[0]
    callback_started = threading.Event()
    callback_threads: list[threading.Thread] = []
    original_activate = authority.activate

    def racing_activate(request) -> None:
        if request.marker["generation"] == 2:

            def run_old_expiry() -> None:
                callback_started.set()
                old_timer.function()

            thread = threading.Thread(target=run_old_expiry)
            callback_threads.append(thread)
            thread.start()
            assert callback_started.wait(timeout=1)
        original_activate(request)

    authority.activate = racing_activate

    guard.renew(second_request)
    for thread in callback_threads:
        thread.join(timeout=1)
        assert not thread.is_alive()

    assert callback_threads, "renewal did not replace the authority snapshot"
    assert old_timer.cancelled is True
    assert guard.expired is False
    assert child.killed is False
    assert authority.authorized(old_expires_at + timedelta(microseconds=1)) is True


def test_live_supervisor_denies_malformed_activation_before_cleanup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _module()
    now = datetime.now(UTC)
    _generated, bootstrap, _directory = _bundle(
        module,
        tmp_path,
        now=now,
        expires_at=now + timedelta(seconds=120),
    )
    module.ACTIVATION.write_bytes(b"not-json\n")

    class Child:
        pid = 987
        returncode = None

        def poll(self):
            return self.returncode

    class Server:
        def shutdown(self) -> None:
            return

        def server_close(self) -> None:
            return

    child = Child()
    authorities = []
    authorization_at_cleanup: list[bool] = []

    def start_server(authority, **_kwargs):
        authorities.append(authority)
        return Server()

    def clear_ack() -> None:
        authorization_at_cleanup.append(authorities[0].authorized(now))
        child.returncode = 7

    monkeypatch.setattr(module, "_start_route_lease_server", start_server)
    monkeypatch.setattr(module, "_selected", lambda **_kwargs: bootstrap)
    monkeypatch.setattr(module, "_await_healthy", lambda _child: True)
    monkeypatch.setattr(module, "_clear_ack", clear_ack)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(module.signal, "signal", lambda *_args: None)

    assert module.main() == 7
    assert authorization_at_cleanup[0] is False


def test_expiry_denies_authority_before_ack_or_process_cleanup(
    monkeypatch,
) -> None:
    module = _module()
    expires_at = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    request = types.SimpleNamespace(marker={"expires_at": expires_at.isoformat()})
    authority = module._RouteLeaseAuthority()
    authority.allow_bootstrap()
    events: list[tuple[str, bool]] = []

    class Child:
        @staticmethod
        def poll():
            return None

        @staticmethod
        def kill() -> None:
            events.append(("kill", authority.authorized(expires_at)))

    monkeypatch.setattr(
        module,
        "_clear_ack",
        lambda: events.append(("clear", authority.authorized(expires_at))),
    )
    guard = module._ServingLeaseGuard(
        request,
        Child(),
        authority=authority,
        clock=lambda: expires_at,
    )

    guard.start()

    assert events == [("clear", False), ("kill", False)]


def test_expiry_claim_serializes_ack_publication(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _module()
    issued_at = datetime.now(UTC)
    _bundle(
        module,
        tmp_path,
        now=issued_at,
        expires_at=issued_at + timedelta(seconds=120),
    )
    request = module._active_request(now=issued_at)
    assert request is not None
    module.ACK_ROOT = tmp_path / "supervisor"
    module.ACK = module.ACK_ROOT / "ack.json"
    authority = module._RouteLeaseAuthority()
    authority.activate(request)
    write_entered = threading.Event()
    release_write = threading.Event()
    write_published = threading.Event()
    release_return = threading.Event()
    clear_entered = threading.Event()
    errors: list[BaseException] = []
    original_atomic_write = module._atomic_write
    original_clear_ack = module._clear_ack

    class Child:
        pid = 123
        returncode = None

        def poll(self):
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    class Timer:
        instances: ClassVar[list[Timer]] = []

        def __init__(self, interval, function) -> None:
            self.interval = interval
            self.function = function
            self.daemon = False
            self.instances.append(self)

        def start(self) -> None:
            return

        def cancel(self) -> None:
            return

    child = Child()

    def blocked_atomic_write(target, content) -> None:
        write_entered.set()
        assert release_write.wait(2), "test did not release acknowledgement write"
        original_atomic_write(target, content)
        write_published.set()
        assert release_return.wait(2), "test did not release acknowledgement return"

    def observed_clear_ack() -> None:
        clear_entered.set()
        original_clear_ack()

    def publish() -> None:
        try:
            publisher = getattr(guard, "publish_ack", None)
            if publisher is None:
                module._write_ack(request, child, now=issued_at)
            else:
                publisher(request, now=issued_at)
        except (AssertionError, OSError, RuntimeError) as error:
            errors.append(error)

    def expire() -> None:
        try:
            Timer.instances[0].function()
        except (AssertionError, OSError, RuntimeError) as error:
            errors.append(error)

    monkeypatch.setattr(module, "_atomic_write", blocked_atomic_write)
    monkeypatch.setattr(module, "_clear_ack", observed_clear_ack)
    monkeypatch.setattr(module.threading, "Timer", Timer)
    guard = module._ServingLeaseGuard(
        request,
        child,
        authority=authority,
        clock=lambda: issued_at,
    )
    guard.start()
    writer = threading.Thread(target=publish)
    expiry = threading.Thread(target=expire)
    writer.start()
    assert write_entered.wait(2), "acknowledgement write did not reach interleaving"
    expiry.start()
    expiry_cleared_while_write_pending = clear_entered.wait(0.1)
    release_write.set()
    assert write_published.wait(2), "acknowledgement was not published"
    release_return.set()
    writer.join(timeout=2)
    expiry.join(timeout=2)

    assert not writer.is_alive() and not expiry.is_alive()
    assert errors == []
    assert expiry_cleared_while_write_pending is False
    assert clear_entered.is_set()
    assert not module.ACK.exists()


def test_expired_serving_guard_cannot_be_renewed(
    monkeypatch,
) -> None:
    module = _module()
    expires_at = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    expired = types.SimpleNamespace(marker={"expires_at": expires_at.isoformat()})
    renewed = types.SimpleNamespace(
        marker={"expires_at": (expires_at + timedelta(seconds=1)).isoformat()}
    )

    class Child:
        @staticmethod
        def poll():
            return None

        @staticmethod
        def kill() -> None:
            return

    monkeypatch.setattr(module, "_clear_ack", lambda: None)
    guard = module._ServingLeaseGuard(
        expired,
        Child(),
        clock=lambda: expires_at,
    )
    guard.start()
    assert guard.expired is True

    try:
        with pytest.raises(RuntimeError, match="already expired"):
            guard.renew(renewed)
    finally:
        guard.cancel()


def test_expiry_guard_stops_a_real_serving_process_at_the_exact_deadline(
    tmp_path: Path,
) -> None:
    module = _module()
    issued_at = datetime.now(UTC)
    expires_at = issued_at + timedelta(seconds=1)
    _bundle(module, tmp_path, now=issued_at, expires_at=expires_at)
    request = module._active_request(now=issued_at)
    assert request is not None

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "http.server",
            str(port),
            "--bind",
            "127.0.0.1",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    guard = module._ServingLeaseGuard(
        request,
        child,
        clock=lambda: datetime.now(UTC),
    )
    try:
        guard.start()
        startup_deadline = time.monotonic() + 0.75
        while True:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/", timeout=0.1
                ) as response:
                    assert response.status == 200
                break
            except (OSError, urllib.error.URLError):
                if time.monotonic() >= startup_deadline:
                    raise
                time.sleep(0.01)

        timeout = time.monotonic() + 1.5
        while child.poll() is None and time.monotonic() < timeout:
            time.sleep(0.01)
        stopped_at = datetime.now(UTC)
        assert stopped_at >= expires_at
        assert stopped_at < expires_at + timedelta(milliseconds=500)
        assert child.poll() is not None
        assert guard.expired is True
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.1)
        except (OSError, urllib.error.URLError):
            pass
        else:
            raise AssertionError("expired serving process still accepted a request")
    finally:
        guard.cancel()
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def test_compose_mounts_one_read_only_route_volume_and_starts_bounded_supervisor() -> (
    None
):
    compose = (ROOT / "deploy/compose/compose.yaml").read_text()
    entrypoint = (ROOT / "deploy/compose/litellm/entrypoint.sh").read_text()
    source = SUPERVISOR.read_text()

    assert "route-publications:/routes" in compose
    assert "config_supervisor.py:/app/config-supervisor.py:ro" in compose
    assert "bootstrap-config.json:/app/bootstrap-config.json:ro" in compose
    assert "exec python /app/config-supervisor.py" in entrypoint
    assert "POLL_SECONDS = 2" in source
    assert "TERMINATE_SECONDS = 30" in source
    assert "shell=True" not in source


def test_compose_initializes_route_volume_for_unprivileged_control_worker() -> None:
    environment = os.environ.copy()
    for line in (ROOT / "deploy/compose/tests/test.env").read_text().splitlines():
        name, value = line.split("=", 1)
        environment[name] = value
    rendered = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(ROOT / "deploy/compose/compose.yaml"),
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    services = json.loads(rendered.stdout)["services"]
    bootstrap = services["control-bootstrap"]
    assert bootstrap["network_mode"] == "none"
    assert bootstrap["user"] == "0:0"
    assert bootstrap["cap_drop"] == ["ALL"]
    assert set(bootstrap["cap_add"]) == {"CHOWN", "FOWNER"}
    assert bootstrap["healthcheck"]["test"] == ["CMD", "test", "-f", "/tmp/bootstrap-ready"]
    assert services["control-worker"]["depends_on"]["control-bootstrap"] == {
        "condition": "service_healthy",
        "required": True,
    }
    assert services["litellm"]["depends_on"]["control-bootstrap"] == {
        "condition": "service_healthy",
        "required": True,
    }
    litellm = services["litellm"]
    assert litellm["user"] == "10002:10001"
    assert litellm["cap_drop"] == ["ALL"]
    assert litellm["security_opt"] == ["no-new-privileges:true"]
    assert litellm["read_only"] is True
    assert (
        "litellm-supervisor-state:/supervisor:rw"
        in (ROOT / "deploy/compose/compose.yaml").read_text()
    )
    assert (
        "litellm-supervisor-state:/supervisor:ro"
        in (ROOT / "deploy/compose/compose.yaml").read_text()
    )


def test_development_image_compose_mounts_staged_acknowledging_supervisor() -> None:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(ROOT / "deploy/compose/compose.dev.images.yaml"),
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    services = json.loads(result.stdout)["services"]
    litellm = services["litellm"]
    worker = services["control-worker"]
    volumes = {volume["target"]: volume for volume in litellm["volumes"]}

    assert litellm["entrypoint"] == ["/run/vonk-runtime/litellm-entrypoint.sh"]
    assert volumes["/run/vonk-runtime"] == {
        "type": "volume",
        "source": "dev-runtime-config",
        "target": "/run/vonk-runtime",
        "read_only": True,
        "volume": {},
    }
    assert volumes["/routes"]["read_only"] is True
    assert volumes["/supervisor"].get("read_only", False) is False

    worker_volumes = {volume["target"]: volume for volume in worker["volumes"]}
    assert worker_volumes["/routes"].get("read_only", False) is False
    assert worker_volumes["/supervisor"]["read_only"] is True
    assert worker["depends_on"]["litellm"] == {
        "condition": "service_healthy",
        "required": True,
    }
