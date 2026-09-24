"""The packaged CLI keeps W10 consent and recovery across its process boundary."""

from __future__ import annotations

import ipaddress
import json
import os
import pty
import select as select_io
import shutil
import signal
import socket
import ssl
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from sqlalchemy import select
from vonk_control.models import FleetProfileApplication

from .test_profile_load_submission import _profile_api

KEY = "11111111-1111-4111-8111-111111111111"


@dataclass
class _PeerState:
    api: TestClient
    headers: dict[str, str]
    calls: list[tuple[str, str, object]]
    drop_load_answer: bool = False
    accepted: dict[str, object] | None = None
    profile_edit_status: int | None = None
    drop_responses: set[tuple[str, str]] = field(default_factory=set)
    dropped_responses: list[tuple[str, str]] = field(default_factory=list)
    corrupt_responses: set[tuple[str, str]] = field(default_factory=set)
    corrupted_responses: list[tuple[str, str]] = field(default_factory=list)
    held_response: tuple[str, str] | None = None
    response_accepted: Event = field(default_factory=Event)
    release_held_response: Event = field(default_factory=Event)
    discard_held_response: bool = False


@contextmanager
def _https_api_peer(
    tmp_path: Path,
    api: TestClient,
    headers: dict[str, str],
    *,
    drop_load_answer: bool = False,
) -> Iterator[tuple[str, Path, _PeerState]]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    x509.DNSName("localhost"),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certificate_path = tmp_path / "controller-test-certificate.pem"
    private_key_path = tmp_path / "controller-test-private-key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    private_key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    private_key_path.chmod(0o600)
    state = _PeerState(
        api=api,
        headers=headers,
        calls=[],
        drop_load_answer=drop_load_answer,
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._forward()

        def do_POST(self) -> None:
            self._forward()

        def do_PUT(self) -> None:
            self._forward()

        def _forward(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            request_content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
            document = (
                json.loads(body)
                if body and request_content_type == "application/json"
                else None
            )
            state.calls.append((self.command, self.path, document))
            request_headers = {
                name: value
                for name in (
                    "Authorization",
                    "Accept",
                    "Content-Type",
                    "X-Request-ID",
                    "X-Content-SHA256",
                )
                if (value := self.headers.get(name)) is not None
            }
            response = state.api.request(
                self.command,
                self.path,
                headers=request_headers,
                content=body or None,
            )
            response_key = (self.command, self.path)
            if response_key == state.held_response:
                state.response_accepted.set()
                if not state.release_held_response.wait(timeout=30):
                    self.close_connection = True
                    return
                if state.discard_held_response:
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.connection.close()
                    return
            if (
                state.drop_load_answer
                and self.command == "POST"
                and self.path == "/api/profile/1/load"
            ):
                assert response.status_code == 202, response.text
                state.accepted = response.json()
                edited = state.api.put(
                    "/api/profile/1",
                    headers=headers,
                    json={"name": "Edited after acceptance", "expected_revision": 1},
                )
                state.profile_edit_status = edited.status_code
                state.drop_load_answer = False
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
                return

            if response_key in state.drop_responses:
                state.drop_responses.remove(response_key)
                state.dropped_responses.append(response_key)
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
                return

            response_body = response.content
            if response_key in state.corrupt_responses:
                state.corrupt_responses.remove(response_key)
                state.corrupted_responses.append(response_key)
                assert response_body
                response_body = bytes([response_body[0] ^ 1]) + response_body[1:]
            self.send_response(response.status_code)
            for name in (
                "Content-Type",
                "X-Request-ID",
                "X-Content-SHA256",
                "X-Vonk-Error-Code",
            ):
                value = response.headers.get(name)
                if value is not None:
                    self.send_header(name, value)
            self.send_header("Content-Length", str(len(response_body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(response_body)
            self.close_connection = True

        def log_message(self, *_args: object) -> None:
            pass

    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate_path, private_key_path)
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        server.socket = server_context.wrap_socket(server.socket, server_side=True)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"https://127.0.0.1:{server.server_port}", certificate_path, state
        finally:
            state.release_held_response.set()
            server.shutdown()
            thread.join(timeout=2)


@pytest.fixture(scope="session")
def installed_vonkctl(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("uv") is None:
        pytest.skip("uv is required to build and install the CLI wheel")
    return _build_installed_vonkctl(tmp_path_factory.mktemp("installed-profile-cli"))


def _build_installed_vonkctl(workspace: Path) -> Path:
    """Build and install the CLI wheel into an isolated, caller-owned directory."""

    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required to build and install the CLI wheel")
    assert uv is not None
    root = Path(__file__).resolve().parents[2]
    workspace.mkdir(parents=True, exist_ok=True)
    wheel_directory = workspace / "wheel"
    wheel_directory.mkdir()
    environment = {
        **os.environ,
        "VONK_BUILD_SOURCE_SHA": "d" * 40,
        "VONK_BUILD_RELEASE_VERSION": "0.1.1",
    }
    built = subprocess.run(
        [uv, "build", "--wheel", "--offline", "--out-dir", str(wheel_directory)],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    wheels = list(wheel_directory.glob("vonk_cluster_profiles-*-py3-none-any.whl"))
    assert len(wheels) == 1
    venv = workspace / "venv"
    created = subprocess.run(
        [uv, "venv", "--python", "3.14", str(venv)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    python = venv / "bin" / "python"
    installed = subprocess.run(
        [
            uv,
            "pip",
            "install",
            "--offline",
            "--python",
            str(python),
            str(wheels[0]),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    executable = venv / "bin" / "vonkctl"
    assert executable.is_file()
    isolated_import = subprocess.run(
        [
            str(python),
            "-c",
            (
                "import importlib.util; "
                "assert importlib.util.find_spec('vonk_control') is None; "
                "assert importlib.util.find_spec('pydantic') is None"
            ),
        ],
        env={**environment, "PYTHONPATH": ""},
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert isolated_import.returncode == 0, isolated_import.stderr
    return executable


def _process_environment(
    tmp_path: Path,
    url: str,
    certificate: Path,
    headers: dict[str, str],
) -> dict[str, str]:
    token = tmp_path / "controller-token"
    token.write_text(headers["Authorization"].removeprefix("Bearer "), encoding="utf-8")
    token.chmod(0o600)
    return {
        **os.environ,
        "PYTHONPATH": "",
        "VONK_CONTROL_URL": url,
        "VONK_CONTROL_TOKEN_FILE": str(token),
        "VONK_CLI_UPDATE_NOTICES": "0",
        "VONK_RECIPE_LIBRARY_ROOT": "/opt/vonk-forge-recipes",
        "SSL_CERT_FILE": str(certificate),
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "HTTPS_PROXY": "",
        "https_proxy": "",
    }


def _run_pty(
    executable: Path,
    arguments: tuple[str, ...],
    environment: dict[str, str],
    cwd: Path,
    *,
    answer: str | None = "yes",
    interrupt: bool = False,
) -> tuple[int, str, str]:
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [str(executable), *arguments],
        stdin=slave,
        stdout=subprocess.PIPE,
        stderr=slave,
        env=environment,
        cwd=cwd,
    )
    os.close(slave)
    transcript = bytearray()
    answer_sent = False
    deadline = time.monotonic() + 45
    try:
        while True:
            if time.monotonic() >= deadline:
                process.kill()
                raise TimeoutError("installed CLI did not finish its PTY interaction")
            ready, _, _ = select_io.select([master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b""
                if not chunk and process.poll() is not None:
                    break
                transcript.extend(chunk)
                if not answer_sent and b"[y/N]" in transcript:
                    if interrupt:
                        process.send_signal(signal.SIGINT)
                    elif answer is None:
                        os.write(master, b"\x04")
                    else:
                        os.write(master, f"{answer}\n".encode())
                    answer_sent = True
            elif process.poll() is not None:
                break
        stdout, _ = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        os.close(master)
    returncode = process.returncode
    assert returncode is not None and stdout is not None
    return returncode, stdout.decode(), transcript.decode(errors="replace")


@pytest.mark.lane
def test_installed_interactive_review_recovers_the_original_load_after_edit(
    installed_vonkctl: Path,
    postgres_engine,
    tmp_path: Path,
) -> None:
    sessions, api, _codec, headers, preview = _profile_api(postgres_engine)
    digest = preview.get("plan_digest")
    assert isinstance(digest, str)
    with _https_api_peer(tmp_path, api, headers, drop_load_answer=True) as (
        url,
        certificate,
        state,
    ):
        environment = _process_environment(tmp_path, url, certificate, headers)
        arguments = (
            "--profile",
            "1",
            "profile",
            "load",
            "--expected-plan",
            digest,
            "--request-key",
            KEY,
            "--detach",
        )
        rejected_status, rejected_stdout, rejected_stderr = _run_pty(
            installed_vonkctl,
            arguments,
            environment,
            tmp_path,
            answer="no",
        )
        assert rejected_status == 2
        assert not rejected_stdout
        assert "Ready for review" in rejected_stderr
        assert rejected_stderr.count("[y/N]") == 1
        assert state.calls == [("POST", "/api/profile/1/preview", None)], (
            rejected_stderr
        )
        assert "Request key:" not in rejected_stderr
        with sessions() as session:
            assert not list(session.scalars(select(FleetProfileApplication)))

        status, stdout, stderr = _run_pty(
            installed_vonkctl,
            arguments,
            environment,
            tmp_path,
        )

    assert status == 0
    assert "Ready for review" in stderr
    assert digest in stderr and stderr.count("[y/N]") == 1
    assert "Ready for review" not in stdout
    assert state.profile_edit_status == 200
    assert state.accepted is not None
    assert state.accepted["request_key"] == KEY
    progress = state.accepted.get("progress")
    assert isinstance(progress, dict)
    intended_profile = progress.get("intended_profile")
    assert isinstance(intended_profile, dict)
    assert intended_profile["reviewed_plan_digest"] == digest
    application_id = state.accepted.get("id")
    assert isinstance(application_id, str)
    assert application_id in stdout
    token_value = headers["Authorization"].removeprefix("Bearer ")
    assert token_value not in stdout + stderr
    assert [(method, path) for method, path, _ in state.calls] == [
        ("POST", "/api/profile/1/preview"),
        ("POST", "/api/profile/1/preview"),
        ("POST", "/api/profile/1/load"),
        ("GET", f"/api/profile/1/requests/{KEY}"),
    ]
    assert state.calls[2][2] == {
        "request_key": KEY,
        "plan_digest": digest,
    }
    with sessions() as session:
        applications = list(session.scalars(select(FleetProfileApplication)))
        assert len(applications) == 1
        assert applications[0].request_key == KEY


@pytest.mark.lane
def test_installed_json_no_input_load_requires_reviewed_digest_and_emits_one_result(
    installed_vonkctl: Path,
    postgres_engine,
    tmp_path: Path,
) -> None:
    sessions, api, _codec, headers, preview = _profile_api(postgres_engine)
    digest = preview.get("plan_digest")
    assert isinstance(digest, str)
    with _https_api_peer(tmp_path, api, headers, drop_load_answer=True) as (
        url,
        certificate,
        state,
    ):
        environment = _process_environment(tmp_path, url, certificate, headers)
        missing_review = subprocess.run(
            [
                str(installed_vonkctl),
                "--no-input",
                "--json",
                "--profile",
                "1",
                "profile",
                "load",
                "--yes",
                "--detach",
            ],
            env=environment,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert missing_review.returncode == 2
        refusal = json.loads(missing_review.stdout)
        assert "--expected-plan DIGEST" in refusal["error"]
        assert not state.calls

        completed = subprocess.run(
            [
                str(installed_vonkctl),
                "--no-input",
                "--json",
                "--profile",
                "1",
                "profile",
                "load",
                "--expected-plan",
                digest,
                "--yes",
                "--request-key",
                KEY,
                "--detach",
            ],
            env=environment,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.endswith("\n")
    result = json.loads(completed.stdout)
    assert completed.stdout.count("\n") == 1
    assert not completed.stderr
    assert state.accepted is not None
    assert result["id"] == state.accepted["id"]
    assert result["request_key"] == KEY
    assert state.profile_edit_status == 200
    token_value = headers["Authorization"].removeprefix("Bearer ")
    assert token_value not in completed.stdout + completed.stderr
    assert [(method, path) for method, path, _ in state.calls] == [
        ("POST", "/api/profile/1/load"),
        ("GET", f"/api/profile/1/requests/{KEY}"),
    ]
    assert state.calls[0][2] == {
        "request_key": KEY,
        "plan_digest": digest,
    }
    with sessions() as session:
        applications = list(session.scalars(select(FleetProfileApplication)))
        assert len(applications) == 1
        assert applications[0].request_key == KEY
