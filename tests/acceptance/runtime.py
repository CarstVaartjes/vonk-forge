from __future__ import annotations

import errno
import json
import os
import pty
import re
import select
import signal
import ssl
import stat
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path


class AcceptanceError(RuntimeError):
    pass


def write_all(write: Callable[[bytes], int], payload: bytes) -> None:
    """Write a complete payload or fail instead of silently truncating it."""
    offset = 0
    while offset < len(payload):
        count = write(payload[offset:])
        if not isinstance(count, int) or count <= 0:
            raise AcceptanceError("acceptance transport stopped before a full write")
        offset += count


def run_interactive(
    command: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    responses: Sequence[tuple[str, str]],
    timeout: float,
    require_all_prompts: bool = True,
) -> str:
    if not command or timeout <= 0:
        raise AcceptanceError("interactive command is invalid")
    argv = [os.fspath(value) for value in command]
    pending = list(responses)
    transcript = bytearray()
    matched_through = 0
    deadline = time.monotonic() + timeout
    pid, terminal = pty.fork()
    if pid == 0:
        try:
            os.chdir(cwd)
            os.execvpe(argv[0], argv, dict(environment))
        except OSError:
            os._exit(127)

    status: int | None = None
    try:
        while status is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AcceptanceError("interactive command timed out")
            readable, _, _ = select.select([terminal], [], [], min(remaining, 0.2))
            if readable:
                try:
                    chunk = os.read(terminal, 64 * 1024)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    chunk = b""
                transcript.extend(chunk)
                if pending:
                    prompt, answer = pending[0]
                    observed = transcript.decode("utf-8", errors="replace")
                    position = observed.find(prompt, matched_through)
                    if position >= 0:
                        write_all(
                            lambda payload: os.write(terminal, payload),
                            answer.encode("utf-8") + b"\n",
                        )
                        matched_through = position + len(prompt)
                        pending.pop(0)
            child, observed_status = os.waitpid(pid, os.WNOHANG)
            if child == pid:
                status = observed_status
        exit_code = os.waitstatus_to_exitcode(status)
        rendered = transcript.decode("utf-8", errors="replace")
        for _, answer in responses:
            if answer:
                rendered = rendered.replace(answer, "<redacted>")
        if exit_code != 0:
            raise AcceptanceError(
                f"interactive command exited with {exit_code}:\n{rendered[-8000:]}"
            )
        if pending and require_all_prompts:
            missing = ", ".join(prompt for prompt, _ in pending)
            raise AcceptanceError(f"interactive prompts were not observed: {missing}")
        return rendered
    except BaseException:
        if status is None:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            child, _ = os.waitpid(pid, 0)
            if child != pid:
                raise AcceptanceError("interactive child could not be reaped")
        raise
    finally:
        os.close(terminal)


def _require_mode(path: Path, expected: int) -> None:
    metadata = path.stat(follow_symlinks=False)
    if stat.S_IMODE(metadata.st_mode) != expected:
        raise AcceptanceError(f"{path.name} has unsafe permissions")


def assert_bundle_contract(bundle: Path) -> None:
    if bundle.is_symlink() or not bundle.is_dir():
        raise AcceptanceError("NAS bundle is not a safe directory")
    if {entry.name for entry in bundle.iterdir()} != {
        ".env",
        "docker-compose.yaml",
        "secrets",
    }:
        raise AcceptanceError(
            "NAS bundle must contain exactly docker-compose.yaml, .env, and secrets"
        )
    compose = bundle / "docker-compose.yaml"
    environment = bundle / ".env"
    secrets = bundle / "secrets"
    for path in (compose, environment):
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise AcceptanceError(f"{path.name} is unsafe")
    if secrets.is_symlink() or not secrets.is_dir():
        raise AcceptanceError("secrets is unsafe")
    _require_mode(compose, 0o644)
    _require_mode(environment, 0o600)

    compose_raw = compose.read_bytes()
    environment_raw = environment.read_bytes()
    secret_files = sorted(path for path in secrets.rglob("*") if path.is_file())
    if not secret_files:
        raise AcceptanceError("NAS bundle has no secrets")
    for directory in [secrets, *(path for path in secrets.rglob("*") if path.is_dir())]:
        if directory.is_symlink():
            raise AcceptanceError("secret directory is unsafe")
        _require_mode(directory, 0o700)
    for path in secret_files:
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise AcceptanceError("secret file is unsafe")
        _require_mode(path, 0o600)
        content = path.read_bytes().strip()
        if not content:
            raise AcceptanceError(f"secret file {path.relative_to(secrets)} is empty")
        if content in compose_raw or content in environment_raw:
            raise AcceptanceError(
                f"secret value {path.relative_to(secrets)} leaked into bundle metadata"
            )


def _compose_rows(raw: str) -> list[dict[str, object]]:
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        try:
            document = [json.loads(line) for line in raw.splitlines() if line.strip()]
        except json.JSONDecodeError as error:
            raise AcceptanceError("Compose status is not valid JSON") from error
    if isinstance(document, dict):
        document = [document]
    if not isinstance(document, list) or any(
        not isinstance(row, dict) for row in document
    ):
        raise AcceptanceError("Compose status is not a list of services")
    return document


def assert_compose_services_healthy(raw: str, expected: set[str]) -> None:
    rows = _compose_rows(raw)
    observed: dict[str, dict[str, object]] = {}
    for row in rows:
        service = row.get("Service")
        if not isinstance(service, str) or not service or service in observed:
            raise AcceptanceError("Compose status has an invalid service identity")
        observed[service] = row
    missing = expected - set(observed)
    unexpected = set(observed) - expected
    if missing or unexpected:
        raise AcceptanceError(
            f"Compose services differ: missing={sorted(missing)}, unexpected={sorted(unexpected)}"
        )
    broken = sorted(
        service
        for service, row in observed.items()
        if row.get("State") != "running" or row.get("Health") != "healthy"
    )
    if broken:
        raise AcceptanceError(f"Compose services are not healthy: {', '.join(broken)}")


def assert_compose_compatibility(
    bundle: Path,
    *,
    fixtures: Sequence[tuple[str, Path]],
    environment: Mapping[str, str],
) -> None:
    """Exercise each declared standalone Compose parser without a Docker engine."""
    if not fixtures or len({name for name, _ in fixtures}) != len(fixtures):
        raise AcceptanceError("Compose compatibility fixtures are invalid")
    compose = bundle / "docker-compose.yaml"
    if not compose.is_file() or not (bundle / ".env").is_file():
        raise AcceptanceError("NAS bundle is missing Compose inputs")
    for name, executable in fixtures:
        if not name or not executable.is_file() or not os.access(executable, os.X_OK):
            raise AcceptanceError(f"Compose fixture {name!r} is unavailable")
        result = subprocess.run(
            [os.fspath(executable), "-f", compose.name, "config", "--quiet"],
            cwd=bundle,
            env=dict(environment),
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            raise AcceptanceError(
                f"Compose fixture {name!r} rejected the bundle:\n"
                f"{result.stdout[-4000:]}\n{result.stderr[-4000:]}"
            )
        if result.stdout or result.stderr:
            raise AcceptanceError(
                f"Compose fixture {name!r} emitted output:\n"
                f"{result.stdout[-4000:]}\n{result.stderr[-4000:]}"
            )


def https_over_command(
    command: Sequence[str | os.PathLike[str]],
    *,
    server_hostname: str,
    path: str,
    cwd: Path,
    environment: Mapping[str, str],
    timeout: float,
    ca_file: Path | None = None,
) -> bytes:
    if (
        not command
        or timeout <= 0
        or not server_hostname
        or any(character in server_hostname for character in "\0\r\n /")
        or not path.startswith("/")
        or any(character in path for character in "\0\r\n")
    ):
        raise AcceptanceError("HTTPS tunnel request is invalid")
    process = subprocess.Popen(
        [os.fspath(value) for value in command],
        cwd=cwd,
        env=dict(environment),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    incoming = ssl.MemoryBIO()
    outgoing = ssl.MemoryBIO()
    context = ssl.create_default_context(cafile=os.fspath(ca_file) if ca_file else None)
    tls = context.wrap_bio(incoming, outgoing, server_hostname=server_hostname)
    deadline = time.monotonic() + timeout

    def flush_tls() -> None:
        while data := outgoing.read():
            write_all(process.stdin.write, data)
            process.stdin.flush()

    def receive_tls() -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AcceptanceError("HTTPS tunnel timed out")
        readable, _, _ = select.select([process.stdout], [], [], remaining)
        if not readable:
            if process.poll() is not None:
                raise AcceptanceError("HTTPS tunnel exited before completing TLS")
            raise AcceptanceError("HTTPS tunnel timed out")
        data = os.read(process.stdout.fileno(), 64 * 1024)
        if data:
            incoming.write(data)
        else:
            incoming.write_eof()

    try:
        while True:
            try:
                tls.do_handshake()
                break
            except ssl.SSLWantReadError:
                flush_tls()
                receive_tls()
            except ssl.SSLWantWriteError:
                flush_tls()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {server_hostname}\r\n"
            "Connection: close\r\n"
            "User-Agent: vonk-forge-acceptance\r\n\r\n"
        ).encode("ascii")
        offset = 0
        while offset < len(request):
            try:
                offset += tls.write(request[offset:])
            except ssl.SSLWantWriteError:
                flush_tls()
            except ssl.SSLWantReadError:
                flush_tls()
                receive_tls()
        flush_tls()
        response = bytearray()
        while True:
            try:
                chunk = tls.read(64 * 1024)
                if not chunk:
                    break
                response.extend(chunk)
            except ssl.SSLWantReadError:
                flush_tls()
                receive_tls()
            except ssl.SSLWantWriteError:
                flush_tls()
            except ssl.SSLEOFError:
                if response:
                    break
                raise
            except ssl.SSLZeroReturnError:
                break
        status_line = bytes(response).partition(b"\r\n")[0]
        if not re.fullmatch(rb"HTTP/1\.[01] 2[0-9][0-9](?: .*)?", status_line):
            raise AcceptanceError(
                f"tailnet HTTPS endpoint returned {status_line[:200]!r}"
            )
        return bytes(response)
    except (BrokenPipeError, ConnectionError, OSError, ssl.SSLError) as error:
        raise AcceptanceError("tailnet HTTPS endpoint is unavailable") from error
    finally:
        try:
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        if process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            process.wait()
