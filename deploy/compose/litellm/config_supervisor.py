#!/usr/bin/env python3
"""Run LiteLLM with the last exact atomic route bundle the Controller published.

The activation marker still carries the Controller's issued/expiry timestamps,
but the supervisor deliberately ignores expiry: a stalled Controller must not
darken inference on healthy Sparks. Routes change only when the Controller
publishes a new generation (including explicit withdrawals).
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, "/opt/vonk-litellm")
from route_activation import (
    ROUTE_KILL_REAP_SECONDS,
    ROUTE_SHUTDOWN_SECONDS,
    ROUTE_STARTUP_SECONDS,
    ActivationMarker,
    SupervisorAcknowledgement,
)

ROOT = Path("/routes")
ACTIVATION = ROOT / "activation.json"
GENERATIONS = ROOT / "generations"
BOOTSTRAP = Path(
    "/run/vonk-normalized-secrets/runtime-assets/litellm/bootstrap-config.json"
)
ACK_ROOT = Path("/supervisor")
ACK = ACK_ROOT / "ack.json"
POLL_SECONDS = 2
TERMINATE_SECONDS = ROUTE_SHUTDOWN_SECONDS
STARTUP_SECONDS = ROUTE_STARTUP_SECONDS
STARTUP_ATTEMPTS = 10
STARTUP_RETRY_SECONDS = 1
HEALTH_TIMEOUT_SECONDS = 3
_PRISMA_CACHE_ROOT = Path("/opt/vonk-litellm/prisma")
_PRISMA_QUERY_ENGINE_ENV = "PRISMA_QUERY_ENGINE_BINARY"


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


def _prepare_query_engine(*, deadline: float | None = None) -> None:
    """Populate and validate the non-root executable Prisma cache."""
    if deadline is None:
        deadline = time.monotonic() + STARTUP_SECONDS
    configured = os.environ.get(_PRISMA_QUERY_ENGINE_ENV)
    if configured is None:
        return
    path = Path(configured)
    try:
        path.relative_to(_PRISMA_CACHE_ROOT)
    except ValueError as error:
        raise RuntimeError("Prisma query engine path is outside its cache") from error

    if not path.exists():
        environment = os.environ.copy()
        environment.pop(_PRISMA_QUERY_ENGINE_ENV, None)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("LiteLLM startup budget exhausted during preparation")
        subprocess.run(
            ["prisma", "-v"],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            timeout=remaining,
        )

    # prisma -v populates the native engine name. On AMD64 that currently
    # matches the configured Debian fallback, while ARM64 uses a distinct
    # linux-arm64 filename. Accept only one native engine in the same pinned
    # cache directory, then retain the strict file checks below.
    if not path.exists():
        candidates = sorted(path.parent.glob("query-engine-*"))
        if len(candidates) != 1:
            raise RuntimeError("Prisma query engine was not populated")
        path = candidates[0]
        os.environ[_PRISMA_QUERY_ENGINE_ENV] = str(path)

    try:
        metadata = path.stat(follow_symlinks=False)
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise RuntimeError("Prisma query engine was not populated") from error
    # The immutable runtime layer is assembled as root, then executed as the
    # dedicated LiteLLM uid.  A root-owned, non-writable binary is therefore
    # the expected production shape; a first-run Prisma binary may instead be
    # owned by the runtime uid.  Neither shape permits group/other writes or
    # privilege-bearing mode bits.
    trusted_owner = metadata.st_uid in {0, os.geteuid()}
    unsafe_mode = metadata.st_mode & (
        stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID
    )
    if (
        resolved != path
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or not trusted_owner
        or unsafe_mode
        or not 0 < metadata.st_size <= 128 * 1024 * 1024
        or not os.access(path, os.X_OK)
    ):
        raise RuntimeError("Prisma query engine cache is unsafe")


def _encoded(value: dict[str, object]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _active_request() -> ActiveRequest | None:
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
    try:
        activation = ActivationMarker.model_validate(marker)
    except ValidationError:
        return None
    marker = activation.model_dump()
    if activation_content != _encoded(marker):
        return None
    generation = activation.generation
    directory_name = activation.directory
    manifest_digest = activation.manifest_sha256
    if directory_name != f"{generation:08d}-{manifest_digest}":
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
        exact_manifest = activation.manifest_document()
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


def _selected() -> Path:
    request = _active_request()
    if request is not None:
        return request.config
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
    acknowledgement = SupervisorAcknowledgement(
        acknowledged_at=now.astimezone(UTC).isoformat(),
        activation_sha256=request.activation_sha256,
        child_pid=child.pid,
        expires_at=marker["expires_at"],
        generation=marker["generation"],
        litellm_sha256=marker["litellm_sha256"],
        schema_version=1,
        state=marker["state"],
    )
    _atomic_write(ACK, acknowledgement.canonical_bytes())


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


def _stop(child: subprocess.Popen[bytes], *, deadline: float | None = None) -> None:
    if child.poll() is not None:
        return
    if deadline is None:
        deadline = time.monotonic() + TERMINATE_SECONDS
    child.terminate()
    if child.poll() is not None:
        return
    remaining = deadline - time.monotonic()
    terminate_wait = max(0, remaining - ROUTE_KILL_REAP_SECONDS)
    if terminate_wait > 0:
        try:
            child.wait(timeout=terminate_wait)
            return
        except subprocess.TimeoutExpired:
            pass
    child.kill()
    remaining = deadline - time.monotonic()
    if remaining > 0:
        try:
            child.wait(timeout=remaining)
            return
        except subprocess.TimeoutExpired:
            pass
    if child.poll() is None:
        raise RuntimeError(
            "LiteLLM child exit was not confirmed within shutdown budget"
        )


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


def _newer_activation(
    current: ActiveRequest | None, candidate: ActiveRequest | None
) -> bool:
    return candidate is not None and (
        current is None or candidate.marker["generation"] > current.marker["generation"]
    )


def _await_healthy(
    child: subprocess.Popen[bytes],
    *,
    deadline: float,
    request: ActiveRequest | None = None,
) -> bool:
    while child.poll() is None and time.monotonic() < deadline:
        if _newer_activation(request, _active_request()):
            return False
        if _healthy(child) and time.monotonic() < deadline:
            return True
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(POLL_SECONDS, remaining))
    return False


def _supervise(*, startup_deadline: float | None = None) -> int:
    stopping = False
    child: subprocess.Popen[bytes] | None = None
    startup_attempts = 0
    if startup_deadline is None:
        startup_deadline = time.monotonic() + STARTUP_SECONDS

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        if child is not None:
            child.terminate()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    request = _active_request()
    selected = request.config if request is not None else _selected()
    while not stopping:
        if time.monotonic() >= startup_deadline:
            _clear_ack()
            return 1
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
        if not _await_healthy(child, deadline=startup_deadline, request=request):
            exited_before_health = child.poll() is not None
            _clear_ack()
            next_request = _active_request()
            newer_activation = _newer_activation(request, next_request)
            _stop(child, deadline=None if newer_activation else startup_deadline)
            if newer_activation:
                # This generation owns a new transition budget, after confirmed
                # shutdown of the superseded child. Ordinary retries do not.
                startup_attempts = 0
                request = next_request
                selected = request.config
                startup_deadline = time.monotonic() + STARTUP_SECONDS
                continue
            if exited_before_health and startup_attempts < STARTUP_ATTEMPTS:
                remaining = startup_deadline - time.monotonic()
                if remaining <= 0:
                    return 1
                time.sleep(min(STARTUP_RETRY_SECONDS, remaining))
                continue
            return 1
        startup_attempts = 0
        if request is None:
            _clear_ack()
        else:
            _write_ack(request, child, now=datetime.now(UTC))
        reload_requested = False
        while child.poll() is None and not stopping:
            time.sleep(POLL_SECONDS)
            if child.poll() is not None:
                break
            candidate_request = _active_request()
            candidate = (
                candidate_request.config
                if candidate_request is not None
                else _selected()
            )
            if _digest(candidate) != active_digest:
                reload_requested = True
                _clear_ack()
                _stop(child)
                selected = candidate
                request = candidate_request
                startup_deadline = time.monotonic() + STARTUP_SECONDS
                break
            if not _healthy(child) or candidate_request is None:
                _clear_ack()
            else:
                # Same config under a newer marker: acknowledge it without a
                # restart so the Controller sees the generation converge.
                _write_ack(candidate_request, child, now=datetime.now(UTC))
            request = candidate_request
        if stopping:
            _clear_ack()
            _stop(child)
            return 0
        if reload_requested:
            continue
        _clear_ack()
        return int(child.returncode or 1)
    return 0


def main() -> int:
    startup_deadline = time.monotonic() + STARTUP_SECONDS
    _prepare_query_engine(deadline=startup_deadline)
    return _supervise(startup_deadline=startup_deadline)


if __name__ == "__main__":
    raise SystemExit(main())
