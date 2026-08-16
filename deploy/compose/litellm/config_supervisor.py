#!/usr/bin/env python3
"""Run LiteLLM with only an exact, unexpired atomic route bundle."""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path("/routes")
ACTIVATION = ROOT / "activation.json"
GENERATIONS = ROOT / "generations"
BOOTSTRAP = Path("/app/bootstrap-config.json")
ACK_ROOT = Path("/supervisor")
ACK = ACK_ROOT / "ack.json"
POLL_SECONDS = 2
TERMINATE_SECONDS = 30
STARTUP_SECONDS = 120
STARTUP_ATTEMPTS = 10
STARTUP_RETRY_SECONDS = 1
HEALTH_TIMEOUT_SECONDS = 3
MAXIMUM_LEASE = timedelta(seconds=300)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_DIRECTORY = re.compile(r"[0-9]{8}-[0-9a-f]{64}\Z")
_MARKER_FIELDS = {
    "schema_version",
    "generation",
    "state",
    "reconciliation_id",
    "plan_digest",
    "evidence_set_digest",
    "routes_sha256",
    "litellm_sha256",
    "issued_at",
    "expires_at",
    "directory",
    "manifest_sha256",
}
_MANIFEST_FIELDS = _MARKER_FIELDS - {"directory", "manifest_sha256"}


class ActiveRequest:
    def __init__(
        self,
        config: Path,
        marker: dict[str, object],
        activation_sha256: str,
    ) -> None:
        self.config = config
        self.marker = marker
        self.activation_sha256 = activation_sha256


@dataclass(frozen=True)
class _RouteLeaseSnapshot:
    state: str
    generation: int | None = None
    activation_sha256: str | None = None
    litellm_sha256: str | None = None
    expires_at: datetime | None = None


class _RouteLeaseAuthority:
    """Own the request-time decision for the loaded LiteLLM route."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = _RouteLeaseSnapshot(state="denied")

    def deny(self) -> None:
        with self._lock:
            self._snapshot = _RouteLeaseSnapshot(state="denied")

    def allow_bootstrap(self) -> None:
        with self._lock:
            self._snapshot = _RouteLeaseSnapshot(state="bootstrap")

    def activate(self, request: ActiveRequest) -> None:
        marker = getattr(request, "marker", None)
        activation_sha256 = getattr(request, "activation_sha256", None)
        if not isinstance(marker, dict):
            self.deny()
            return
        generation = marker.get("generation")
        litellm_sha256 = marker.get("litellm_sha256")
        expires_at = _parse_timestamp(marker.get("expires_at"))
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation <= 0
            or not isinstance(activation_sha256, str)
            or _DIGEST.fullmatch(activation_sha256) is None
            or not isinstance(litellm_sha256, str)
            or _DIGEST.fullmatch(litellm_sha256) is None
            or expires_at is None
        ):
            self.deny()
            return
        snapshot = _RouteLeaseSnapshot(
            state="active",
            generation=generation,
            activation_sha256=activation_sha256,
            litellm_sha256=litellm_sha256,
            expires_at=expires_at,
        )
        with self._lock:
            self._snapshot = snapshot

    def authorized(self, now: datetime) -> bool:
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            return False
        with self._lock:
            snapshot = self._snapshot
        if snapshot.state == "bootstrap":
            return True
        return (
            snapshot.state == "active"
            and isinstance(snapshot.generation, int)
            and not isinstance(snapshot.generation, bool)
            and snapshot.generation > 0
            and isinstance(snapshot.activation_sha256, str)
            and _DIGEST.fullmatch(snapshot.activation_sha256) is not None
            and isinstance(snapshot.litellm_sha256, str)
            and _DIGEST.fullmatch(snapshot.litellm_sha256) is not None
            and isinstance(snapshot.expires_at, datetime)
            and snapshot.expires_at.tzinfo is not None
            and snapshot.expires_at.utcoffset() is not None
            and now.astimezone(UTC) < snapshot.expires_at.astimezone(UTC)
        )


def _start_route_lease_server(
    authority: _RouteLeaseAuthority,
    *,
    host: str = "0.0.0.0",
    port: int = 4001,
) -> ThreadingHTTPServer:
    class RouteLeaseHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _respond(self, status: int) -> None:
            self.request_version = self.protocol_version
            self.send_response_only(status)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _not_found(self) -> None:
            self._respond(404)

        def handle_expect_100(self) -> bool:
            self.close_connection = True
            self._not_found()
            return False

        def send_error(
            self,
            code: int,
            message: str | None = None,
            explain: str | None = None,
        ) -> None:
            del code, message, explain
            self.close_connection = True
            self._not_found()

        def do_GET(self) -> None:
            if self.path != "/vonk/route-lease":
                self._not_found()
                return
            self._respond(204 if authority.authorized(datetime.now(UTC)) else 503)

        do_CONNECT = _not_found
        do_DELETE = _not_found
        do_HEAD = _not_found
        do_OPTIONS = _not_found
        do_PATCH = _not_found
        do_POST = _not_found
        do_PUT = _not_found
        do_TRACE = _not_found

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer((host, port), RouteLeaseHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class _ServingLeaseGuard:
    """Kill the exact loaded serving process when its route lease expires."""

    def __init__(
        self,
        request: ActiveRequest | None,
        child: subprocess.Popen[bytes],
        *,
        authority: _RouteLeaseAuthority | None = None,
        clock=lambda: datetime.now(UTC),
    ) -> None:
        self._child = child
        self._authority = authority
        self._clock = clock
        self._expires = (
            _parse_timestamp(request.marker.get("expires_at"))
            if request is not None
            else None
        )
        if request is not None and self._expires is None:
            raise RuntimeError("LiteLLM serving lease is invalid")
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._timer_generation = 0
        self._expired = threading.Event()

    @property
    def expired(self) -> bool:
        return self._expired.is_set()

    def start(self) -> None:
        with self._lock:
            self._timer_generation += 1
            generation = self._timer_generation
            expires = self._expires
        if expires is None:
            return
        remaining = (expires - self._clock().astimezone(UTC)).total_seconds()
        if remaining <= 0:
            self._expire(generation)
            return
        timer = threading.Timer(remaining, lambda: self._expire(generation))
        timer.daemon = True
        with self._lock:
            if generation != self._timer_generation:
                return
            self._timer = timer
        timer.start()

    def cancel(self) -> None:
        with self._lock:
            self._timer_generation += 1
            timer = self._timer
            self._timer = None
        if timer is not None:
            timer.cancel()

    def renew(self, request: ActiveRequest | None) -> None:
        expires = (
            _parse_timestamp(request.marker.get("expires_at"))
            if request is not None
            else None
        )
        if request is not None and expires is None:
            raise RuntimeError("LiteLLM serving lease is invalid")
        self.cancel()
        with self._lock:
            if self._expired.is_set():
                raise RuntimeError("LiteLLM serving lease already expired")
            self._expires = expires
        self.start()

    def publish_ack(self, request: ActiveRequest, *, now: datetime) -> None:
        with self._lock:
            if self._expired.is_set():
                raise RuntimeError("LiteLLM serving lease already expired")
            _write_ack(request, self._child, now=now)

    def _expire(self, generation: int) -> None:
        with self._lock:
            if generation != self._timer_generation:
                return
            self._timer = None
            self._expired.set()
            if self._authority is not None:
                self._authority.deny()
            try:
                _clear_ack()
            except RuntimeError:
                pass
        try:
            if self._child.poll() is None:
                self._child.kill()
        except OSError:
            pass


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _encoded(value: dict[str, object]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _active_request(*, now: datetime) -> ActiveRequest | None:
    if (
        ACTIVATION.is_symlink()
        or not ACTIVATION.is_file()
        or GENERATIONS.is_symlink()
        or not GENERATIONS.is_dir()
    ):
        return None
    try:
        activation_content = ACTIVATION.read_bytes()
        marker = json.loads(activation_content)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(marker, dict) or set(marker) != _MARKER_FIELDS:
        return None
    if activation_content != _encoded(marker):
        return None
    generation = marker.get("generation")
    directory_name = marker.get("directory")
    manifest_digest = marker.get("manifest_sha256")
    if (
        marker.get("schema_version") != 1
        or marker.get("state") not in {"maintenance", "published"}
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation <= 0
        or not isinstance(directory_name, str)
        or _DIRECTORY.fullmatch(directory_name) is None
        or not isinstance(manifest_digest, str)
        or _DIGEST.fullmatch(manifest_digest) is None
        or directory_name != f"{generation:08d}-{manifest_digest}"
    ):
        return None
    try:
        reconciliation_id = marker.get("reconciliation_id")
        if (
            not isinstance(reconciliation_id, str)
            or str(uuid.UUID(reconciliation_id)) != reconciliation_id
        ):
            return None
    except (ValueError, AttributeError):
        return None
    if any(
        not isinstance(marker.get(field), str)
        or _DIGEST.fullmatch(marker[field]) is None
        for field in (
            "plan_digest",
            "evidence_set_digest",
            "routes_sha256",
            "litellm_sha256",
        )
    ):
        return None
    issued = _parse_timestamp(marker.get("issued_at"))
    expires = _parse_timestamp(marker.get("expires_at"))
    current = now.astimezone(UTC)
    if (
        issued is None
        or expires is None
        or issued > current
        or current >= expires
        or expires <= issued
        or expires - issued > MAXIMUM_LEASE
    ):
        return None
    directory = GENERATIONS / directory_name
    if directory.is_symlink() or not directory.is_dir():
        return None
    manifest = directory / "manifest.json"
    routes = directory / "routes.json"
    config = directory / "litellm.json"
    if any(
        path.is_symlink() or not path.is_file() for path in (manifest, routes, config)
    ):
        return None
    try:
        exact_manifest = {field: marker[field] for field in _MANIFEST_FIELDS}
        config_document = json.loads(config.read_bytes())
    except (OSError, KeyError, json.JSONDecodeError):
        return None
    if (
        manifest.read_bytes() != _encoded(exact_manifest)
        or _digest(manifest) != manifest_digest
        or _digest(routes) != marker["routes_sha256"]
        or _digest(config) != marker["litellm_sha256"]
        or not isinstance(config_document, dict)
        or not isinstance(config_document.get("model_list"), list)
        or (marker["state"] == "maintenance" and config_document["model_list"] != [])
    ):
        return None
    return ActiveRequest(
        config=config,
        marker=marker,
        activation_sha256=hashlib.sha256(activation_content).hexdigest(),
    )


def _active_config(*, now: datetime) -> Path | None:
    request = _active_request(now=now)
    return None if request is None else request.config


def _activation_present() -> bool:
    return ACTIVATION.exists() or ACTIVATION.is_symlink()


def _selected(*, now: datetime | None = None) -> Path:
    active = _active_config(now=now or datetime.now(UTC))
    if active is not None:
        return active
    if BOOTSTRAP.is_symlink() or not BOOTSTRAP.is_file():
        raise RuntimeError("LiteLLM bootstrap config is unavailable")
    try:
        document = json.loads(BOOTSTRAP.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("LiteLLM bootstrap config is invalid") from error
    if not isinstance(document, dict) or document.get("model_list") != []:
        raise RuntimeError("LiteLLM bootstrap config must be empty")
    return BOOTSTRAP


def _atomic_write(target: Path, content: bytes) -> None:
    target.parent.mkdir(mode=0o750, exist_ok=True)
    if target.parent.is_symlink() or not target.parent.is_dir() or target.is_symlink():
        raise RuntimeError("LiteLLM acknowledgement path is unsafe")
    descriptor, temporary_raw = tempfile.mkstemp(
        prefix=f".{target.name}-", dir=target.parent
    )
    temporary = Path(temporary_raw)
    try:
        os.fchmod(descriptor, 0o640)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _write_ack(
    request: ActiveRequest,
    child: subprocess.Popen[bytes],
    *,
    now: datetime,
) -> None:
    if child.poll() is not None or not isinstance(child.pid, int) or child.pid <= 0:
        raise RuntimeError("LiteLLM process is not live")
    marker = request.marker
    expires = _parse_timestamp(marker.get("expires_at"))
    if expires is None or now.astimezone(UTC) >= expires:
        try:
            if child.poll() is None:
                child.kill()
        except OSError:
            pass
        raise RuntimeError("LiteLLM serving lease expired before acknowledgement")
    acknowledgement = {
        "acknowledged_at": now.astimezone(UTC).isoformat(),
        "activation_sha256": request.activation_sha256,
        "child_pid": child.pid,
        "expires_at": marker["expires_at"],
        "generation": marker["generation"],
        "litellm_sha256": marker["litellm_sha256"],
        "schema_version": 1,
        "state": marker["state"],
    }
    _atomic_write(ACK, _encoded(acknowledgement))


def _clear_ack() -> None:
    if ACK.is_symlink():
        raise RuntimeError("LiteLLM acknowledgement path is unsafe")
    try:
        ACK.unlink()
    except FileNotFoundError:
        return
    directory = os.open(ACK.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _stop(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=TERMINATE_SECONDS)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def _healthy(child: subprocess.Popen[bytes]) -> bool:
    if child.poll() is not None:
        return False
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:4000/health/liveliness",
            timeout=HEALTH_TIMEOUT_SECONDS,
        ) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _await_healthy(child: subprocess.Popen[bytes]) -> bool:
    deadline = time.monotonic() + STARTUP_SECONDS
    while child.poll() is None and time.monotonic() < deadline:
        if _healthy(child):
            return True
        time.sleep(POLL_SECONDS)
    return False


def _supervise(authority: _RouteLeaseAuthority) -> int:
    stopping = False
    child: subprocess.Popen[bytes] | None = None
    startup_attempts = 0

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        authority.deny()
        if child is not None:
            child.terminate()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    request = _active_request(now=datetime.now(UTC))
    selected = request.config if request is not None else _selected()
    while not stopping:
        authority.deny()
        startup_attempts += 1
        active_digest = _digest(selected)
        child = subprocess.Popen(
            [
                "litellm",
                "--config",
                str(selected),
                "--host",
                "0.0.0.0",
                "--port",
                "4000",
            ],
            stdin=subprocess.DEVNULL,
        )
        serving_lease = _ServingLeaseGuard(request, child, authority=authority)
        serving_lease.start()
        if not _await_healthy(child):
            exited_before_health = child.poll() is not None
            authority.deny()
            serving_lease.cancel()
            _clear_ack()
            _stop(child)
            if serving_lease.expired:
                startup_attempts = 0
                request = _active_request(now=datetime.now(UTC))
                selected = request.config if request is not None else _selected()
                continue
            if exited_before_health and startup_attempts < STARTUP_ATTEMPTS:
                time.sleep(STARTUP_RETRY_SECONDS)
                request = _active_request(now=datetime.now(UTC))
                selected = request.config if request is not None else _selected()
                continue
            return 1
        startup_attempts = 0
        if request is None:
            if _activation_present():
                authority.deny()
            else:
                authority.allow_bootstrap()
            _clear_ack()
        else:
            authority.activate(request)
            serving_lease.publish_ack(request, now=datetime.now(UTC))
        reload_requested = False
        while child.poll() is None and not stopping:
            time.sleep(POLL_SECONDS)
            candidate_request = _active_request(now=datetime.now(UTC))
            candidate = (
                candidate_request.config
                if candidate_request is not None
                else _selected()
            )
            if _digest(candidate) != active_digest:
                reload_requested = True
                authority.deny()
                serving_lease.cancel()
                _clear_ack()
                _stop(child)
                selected = candidate
                request = candidate_request
                break
            if not _healthy(child):
                authority.deny()
                _clear_ack()
            elif candidate_request is None:
                serving_lease.renew(None)
                if _activation_present():
                    authority.deny()
                else:
                    authority.allow_bootstrap()
                _clear_ack()
            else:
                authority.activate(candidate_request)
                try:
                    serving_lease.renew(candidate_request)
                except Exception:
                    authority.deny()
                    _clear_ack()
                    raise
                serving_lease.publish_ack(
                    candidate_request,
                    now=datetime.now(UTC),
                )
            request = candidate_request
        if stopping:
            authority.deny()
            serving_lease.cancel()
            _clear_ack()
            _stop(child)
            return 0
        if reload_requested:
            continue
        authority.deny()
        serving_lease.cancel()
        _clear_ack()
        return int(child.returncode or 1)
    return 0


def main() -> int:
    authority = _RouteLeaseAuthority()
    server = _start_route_lease_server(authority)
    try:
        return _supervise(authority)
    finally:
        authority.deny()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
