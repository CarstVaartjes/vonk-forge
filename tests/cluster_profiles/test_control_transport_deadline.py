"""Real TLS boundaries: progress on a socket must not extend a request's life."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from test_control_client_requests import _artifact_job_response

from cluster_profiles import cli
from cluster_profiles.control_client import (
    ControlClient,
    ControlClientError,
    ControlTransportError,
)

KEY = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def https_peer(tmp_path, monkeypatch):
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
    cert_path = tmp_path / "certificate.pem"
    key_path = tmp_path / "private.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    token = tmp_path / "token"
    token.write_text("private-test-token")
    token.chmod(0o600)
    monkeypatch.setenv("SSL_CERT_FILE", str(cert_path))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    state = {
        "stage": "body",
        "status": 202,
        "calls": [],
        "closed": threading.Event(),
        "started": threading.Event(),
    }
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state["calls"].append((self.command, self.path, body))
            self.respond()

        def do_GET(self):
            state["calls"].append((self.command, self.path, b""))
            self.respond()

        do_PUT = do_POST

        def respond(self):
            body = state.get("body", b'{"detail":"fixture response"}')
            status = state["status"]
            media_type = state.get("media_type", "application/json")
            headers = (
                f"HTTP/1.1 {status} Fixture\r\nContent-Type: {media_type}\r\n"
                f"Content-Length: {len(body)}\r\nX-Request-ID: deadline-fixture\r\n"
                f"X-Content-SHA256: {hashlib.sha256(body).hexdigest()}\r\n"
                "Location: /redirected\r\n"
                "Retry-After: 120\r\nConnection: close\r\n\r\n"
            ).encode()
            try:
                if state["stage"] == "fast":
                    self.wfile.write(headers + body)
                    self.wfile.flush()
                    return
                if state["stage"] == "headers":
                    data = headers + body
                else:
                    self.wfile.write(headers)
                    self.wfile.flush()
                    state["started"].set()
                    data = body
                for byte in data:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    if stop.wait(0.04):
                        break
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                state["closed"].set()

        def log_message(self, *_args):
            pass

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = ControlClient(
            f"https://127.0.0.1:{server.server_port}", token, timeout_seconds=0.25
        )
        state.update(url=f"https://127.0.0.1:{server.server_port}", token=str(token))
        try:
            yield client, state
        finally:
            stop.set()
            server.shutdown()
            thread.join(timeout=2)


@pytest.mark.parametrize(
    "stage,status", [("headers", 202), ("body", 202), ("body", 403)]
)
def test_elapsed_deadline_closes_slow_https_and_retains_received_evidence(
    https_peer, stage, status
):
    client, state = https_peer
    state.update(stage=stage, status=status)
    started = time.monotonic()
    with pytest.raises(ControlTransportError) as failure:
        client.request(
            "POST",
            "/api/model/chosen/download",
            {"schema_version": 2, "request_key": KEY},
        )
    elapsed = time.monotonic() - started
    assert elapsed < 0.75, (
        f"continuous socket progress extended the 0.25s deadline to {elapsed:.3f}s"
    )
    context = failure.value.context
    assert context is not None and context.transport == "timeout"
    assert context.http_status == (status if stage == "body" else None)
    assert context.request_id == ("deadline-fixture" if stage == "body" else None)
    assert failure.value.retry_after_seconds == (120 if stage == "body" else None)
    assert state["closed"].wait(1), "deadline left its HTTPS connection open"
    assert len(state["calls"]) == 1
    assert "private-test-token" not in str(failure.value)


@pytest.mark.parametrize(
    "status,acceptance,calls", [(403, "refused", 1), (202, "unknown", 2)]
)
def test_cli_slow_response_never_repeats_post_without_authoritative_absence(
    https_peer, status, acceptance, calls
):
    client, state = https_peer
    state["status"] = status
    output = StringIO()
    started = time.monotonic()
    with redirect_stdout(output):
        exit_code = cli.main(
            ["model", "download", "chosen", "--request-key", KEY, "--detach", "--json"],
            control_client=client,
        )
    elapsed = time.monotonic() - started
    result = json.loads(output.getvalue())
    assert exit_code == 2 and result["submission"]["acceptance"] == acceptance
    assert result["request_key"] == KEY
    assert elapsed < 1.0, f"submission escaped its 0.75s total budget: {elapsed:.3f}s"
    assert len(state["calls"]) == calls
    assert sum(method == "POST" for method, _, _ in state["calls"]) == 1
    assert json.loads(state["calls"][0][2])["request_key"] == KEY


def test_generated_client_uses_same_body_deadline_and_received_evidence(https_peer):
    client, state = https_peer
    state["status"] = 503
    with pytest.raises(ControlTransportError) as failure:
        client.fleet()
    context = failure.value.context
    assert context is not None and context.transport == "timeout"
    assert context.http_status == 503 and context.request_id == "deadline-fixture"
    assert failure.value.retry_after_seconds == 120
    assert state["closed"].wait(1)


def test_cancelled_dns_cannot_delay_exit_or_send_a_late_request(
    https_peer, monkeypatch
):
    _, state = https_peer
    from pathlib import Path

    original = socket.getaddrinfo
    release = threading.Event()
    finished = threading.Event()
    lookups = []

    def stalled(*args, **kwargs):
        lookups.append(args)
        try:
            release.wait(1.5)
            return original(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(socket, "getaddrinfo", stalled)
    client = ControlClient(
        state["url"].replace("127.0.0.1", "localhost"),
        Path(state["token"]),
        timeout_seconds=0.25,
    )
    started = time.monotonic()
    try:
        with pytest.raises(ControlTransportError) as failure:
            client.request(
                "POST",
                "/api/model/chosen/download",
                {"schema_version": 2, "request_key": KEY},
            )
        elapsed = time.monotonic() - started
        assert elapsed < 0.75, f"resolver cleanup delayed exit to {elapsed:.3f}s"
        assert (
            failure.value.context is not None
            and failure.value.context.transport == "timeout"
        )
        with pytest.raises(ControlTransportError):
            client.request("GET", "/api/model")
        assert len(lookups) == 1, (
            "repeated requests accumulated stalled resolver workers"
        )
    finally:
        release.set()
    assert finished.wait(1)
    assert not state["calls"], "late resolution submitted a cancelled network request"


def test_transport_preserves_streamed_artifact_upload_and_verified_download(
    https_peer, tmp_path
):
    client, state = https_peer
    state.update(
        stage="fast", status=200, body=json.dumps(_artifact_job_response()).encode()
    )
    content = (
        bytes(range(256)) * 8_193
    )  # Cross both network and artifact read boundaries.
    digest = hashlib.sha256(content).hexdigest()
    source = tmp_path / "source.bin"
    source.write_bytes(content)
    result = client.upload_file(
        "/api/artifact-jobs/job-1/inputs/source.bin",
        source,
        media_type="application/octet-stream",
        expected_sha256=digest,
        expected_size=len(content),
    )
    assert result["state"] == "draft"
    assert state["calls"][0][2] == content
    state.update(body=content, media_type="image/png")
    destination = tmp_path / "result.png"
    client.download_file(
        f"/api/artifact-jobs/job-1/results/result.png/{digest}",
        destination,
        media_type="image/png",
        expected_sha256=digest,
        expected_size=len(content),
        overwrite=False,
    )
    assert destination.read_bytes() == content


def test_redirect_cannot_forward_authenticated_request(https_peer):
    client, state = https_peer
    state.update(stage="fast", status=302)
    with pytest.raises(ControlClientError):
        client.request(
            "POST",
            "/api/model/chosen/download",
            {"schema_version": 2, "request_key": KEY},
        )
    assert len(state["calls"]) == 1 and state["calls"][0][1] != "/redirected"


def test_untrusted_tls_is_classified_without_exposing_credentials(
    https_peer, monkeypatch
):
    client, state = https_peer
    monkeypatch.delenv("SSL_CERT_FILE")
    with pytest.raises(ControlTransportError) as failure:
        client.request("GET", "/api/model")
    assert (
        failure.value.context is not None and failure.value.context.transport == "tls"
    )
    assert not state["calls"]
    assert "private-test-token" not in str(failure.value)


def test_sigint_closes_https_and_keeps_unknown_acceptance_without_cancelling_remote_work(
    https_peer,
):
    _, state = https_peer
    script = """
import sys
from pathlib import Path
from cluster_profiles import cli
from cluster_profiles.control_client import ControlClient
client = ControlClient(sys.argv[1], Path(sys.argv[2]), timeout_seconds=2)
raise SystemExit(cli.main(sys.argv[3:], control_client=client))
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            state["url"],
            state["token"],
            "model",
            "download",
            "chosen",
            "--request-key",
            KEY,
            "--detach",
            "--json",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert state["started"].wait(5), "child never reached the response boundary"
        process.send_signal(signal.SIGINT)
        output, error = process.communicate(timeout=3)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)
    result = json.loads(output)
    assert process.returncode == 130
    assert (
        result["submission"]["acceptance"] == "unknown" and result["request_key"] == KEY
    )
    assert "Traceback" not in error and "private-test-token" not in output + error
    assert state["closed"].wait(1)
    assert [(method, path) for method, path, _ in state["calls"]] == [
        ("POST", "/api/model/chosen/download")
    ]


def test_expired_dns_does_not_keep_the_cli_process_alive(https_peer):
    _, state = https_peer
    script = """
import socket, sys, time
from pathlib import Path
from cluster_profiles import cli
from cluster_profiles.control_client import ControlClient
def stalled(*args, **kwargs):
    time.sleep(30)
    raise socket.gaierror(-2, 'fixture DNS failure')
socket.getaddrinfo = stalled
client = ControlClient('https://deadline.invalid', Path(sys.argv[1]), timeout_seconds=0.25)
raise SystemExit(cli.main(sys.argv[2:], control_client=client))
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            state["token"],
            "model",
            "download",
            "chosen",
            "--request-key",
            KEY,
            "--detach",
            "--json",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        output, error = process.communicate(timeout=3)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)
    result = json.loads(output)
    assert process.returncode == 2 and result["submission"]["acceptance"] == "unknown"
    assert result["request_key"] == KEY and not state["calls"]
    assert "Traceback" not in error
