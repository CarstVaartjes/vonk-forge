"""Real TLS boundaries: progress on a socket must not extend a request's life."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from test_control_client_requests import _artifact_job_response
from vonk_control.observation_transfer import (
    OBSERVATION_MEDIA_TYPE,
    ObservationTransferChunk,
    ObservationTransferComplete,
    ObservationTransferStart,
    _record,
    observation_response,
)
from vonk_control.strict_json import serialize_json_value

from cluster_profiles import cli
from cluster_profiles.control_client import (
    ControlClient,
    ControlClientError,
    ControlTransportError,
)
from control.tests.test_observation_transfer import _large_snapshot

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
        "accepted": threading.Event(),
        "started": threading.Event(),
    }
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        raw_requestline: bytes

        # The client's close can surface anywhere in the exchange, not only
        # while respond() is writing; record it wherever it lands.
        def handle(self):
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                state["closed"].set()
            else:
                # BaseHTTPRequestHandler treats a clean peer EOF before a
                # request line as a normal return, not a write exception.
                if self.raw_requestline == b"":
                    state["closed"].set()

        def finish(self):
            try:
                super().finish()
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                state["closed"].set()

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
                if state["stage"] == "records":
                    self.wfile.write(headers)
                    self.wfile.flush()
                    state["started"].set()
                    for record in body.splitlines(keepends=True):
                        self.wfile.write(record)
                        self.wfile.flush()
                        state["records_sent"] += 1
                        if stop.wait(0.005):
                            break
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

    class Server(ThreadingHTTPServer):
        def get_request(self):
            connection, address = super().get_request()
            # Observe actual TCP admission before TLS can fail or the client
            # deadline can expire without an HTTP request reaching Handler.
            state["accepted"].set()
            return (
                context.wrap_socket(
                    connection, server_side=True, do_handshake_on_connect=False
                ),
                address,
            )

    with Server(("127.0.0.1", 0), Handler) as server:
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
            {"request_key": KEY},
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
    closure_deadline = time.monotonic() + 1
    state["accepted"].wait(1)
    if state["accepted"].is_set():
        assert state["closed"].wait(max(0, closure_deadline - time.monotonic())), (
            "deadline left its admitted HTTPS connection open"
        )
    else:
        # The total attempt includes native setup before a connection exists.
        # Such an expiry cannot invent admission or a peer closure observation.
        assert not state["calls"]
    assert len(state["calls"]) <= 1
    if stage == "body":
        assert len(state["calls"]) == 1
    assert "private-test-token" not in str(failure.value)


def test_tls_peer_eof_before_request_is_observed_without_write_failure(https_peer):
    _, state = https_peer
    origin = urllib.parse.urlsplit(state["url"])
    assert origin.hostname is not None and origin.port is not None
    context = ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])
    with (
        socket.create_connection((origin.hostname, origin.port), timeout=0.25) as raw,
        context.wrap_socket(raw, server_hostname=origin.hostname),
    ):
        assert state["accepted"].wait(1)
    assert state["closed"].wait(1), "graceful TLS EOF was not actually observed"
    assert not state["calls"], "EOF fixture unexpectedly delivered an HTTP request"


@pytest.mark.parametrize(
    "status,acceptance,calls", [(403, "refused", 1), (202, "unknown", 4)]
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
    # The authoritative read shares the remaining deadline. Under load it may
    # expire before reaching the peer; it must never become a second write.
    assert 1 <= len(state["calls"]) <= calls
    assert sum(method == "POST" for method, _path, _body in state["calls"]) == 1
    assert json.loads(state["calls"][0][2])["request_key"] == KEY


@pytest.mark.parametrize("stage", ["headers", "body"])
def test_generated_client_uses_same_body_deadline_and_received_evidence(
    https_peer, stage
):
    client, state = https_peer
    state.update(status=503, stage=stage)
    with pytest.raises(ControlTransportError) as failure:
        client.fleet()
    context = failure.value.context
    assert context is not None and context.transport == "timeout"
    assert context.http_status == (503 if stage == "body" else None)
    assert context.request_id == ("deadline-fixture" if stage == "body" else None)
    assert failure.value.retry_after_seconds == (120 if stage == "body" else None)
    assert len(state["calls"]) == 1
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
                {"request_key": KEY},
            )
        elapsed = time.monotonic() - started
        assert elapsed < 0.75, f"resolver cleanup delayed exit to {elapsed:.3f}s"
        assert (
            failure.value.context is not None
            and failure.value.context.transport == "timeout"
        )
        with pytest.raises(ControlTransportError):
            client.request("GET", "/api/model/library")
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
    assert result["preparation"] == "draft"
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
            {"request_key": KEY},
        )
    assert len(state["calls"]) == 1 and state["calls"][0][1] != "/redirected"


def test_untrusted_tls_is_classified_without_exposing_credentials(
    https_peer, monkeypatch
):
    client, state = https_peer
    monkeypatch.delenv("SSL_CERT_FILE")
    with pytest.raises(ControlTransportError) as failure:
        client.request("GET", "/api/model/library")
    assert (
        failure.value.context is not None and failure.value.context.transport == "tls"
    )
    assert not state["calls"]
    assert "private-test-token" not in str(failure.value)


def test_sigint_stops_promptly_closes_https_and_does_not_cancel_remote_work(
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
        # Starting the child is a bound on a hang; promptness is asserted below.
        assert state["started"].wait(30), "child never reached the response boundary"
        process.send_signal(signal.SIGINT)
        # An interrupted command owes no output; it must only stop promptly.
        output, error = process.communicate(timeout=3)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)
    assert process.returncode != 0
    assert "private-test-token" not in output + error
    assert state["closed"].wait(1)
    # The request was sent once and nothing asked the Controller to cancel it.
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
        env={
            "HOME": str(Path(state["token"]).parent),
            "PATH": os.defpath,
            "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        },
    )
    try:
        # Keep well below the 30-second blocked DNS shim while allowing a busy
        # xdist worker to start the isolated CLI process.
        output, error = process.communicate(timeout=8)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3)
    result = json.loads(output)
    assert process.returncode == 2 and result["submission"]["acceptance"] == "unknown"
    assert result["request_key"] == KEY and not state["calls"]
    assert "Traceback" not in error


def _wedged_loop_response(monkeypatch, *, interrupt: bool):
    """An ``HTTPSResponse`` whose event loop refuses to run its own cleanup.

    This is what a Ctrl-C raised inside the loop's bookkeeping leaves behind:
    the next ``run`` fails with "Event loop stopped before Future completed".
    """

    from types import SimpleNamespace

    from cluster_profiles import control_transport

    async def refuses(_self) -> None:
        raise RuntimeError("Event loop stopped before Future completed.")

    monkeypatch.setattr(control_transport.HTTPSResponse, "_close", refuses)
    request = control_transport.urllib.request.Request("https://control.invalid/")
    if interrupt:

        def interrupted(_self, work):
            work.close()
            raise KeyboardInterrupt

        monkeypatch.setattr(control_transport.HTTPSResponse, "_run", interrupted)
        return control_transport, request
    monkeypatch.setattr(
        control_transport.HTTPSResponse,
        "_run",
        lambda _self, work: (
            work.close(),
            SimpleNamespace(status_code=200, headers=SimpleNamespace(multi_items=list)),
        )[1],
    )
    return control_transport, request


def test_ctrl_c_while_opening_is_not_replaced_by_a_cleanup_failure(monkeypatch):
    # The CLI maps KeyboardInterrupt to exit 130; a RuntimeError from cleanup
    # chained over it turned an interrupted follow into a failed one.
    transport, request = _wedged_loop_response(monkeypatch, interrupt=True)
    with pytest.raises(KeyboardInterrupt):
        transport.HTTPSResponse(request, 1)


def test_ctrl_c_inside_a_response_block_is_not_replaced_by_a_cleanup_failure(
    monkeypatch,
):
    transport, request = _wedged_loop_response(monkeypatch, interrupt=False)
    response = transport.HTTPSResponse(request, 1)
    with pytest.raises(KeyboardInterrupt), response:
        raise KeyboardInterrupt


def test_a_cleanup_failure_with_nothing_in_flight_is_still_reported(monkeypatch):
    # The swallow is only for an exception already being handled: a clean exit
    # whose close fails must not look like success.
    transport, request = _wedged_loop_response(monkeypatch, interrupt=False)
    response = transport.HTTPSResponse(request, 1)
    with pytest.raises(RuntimeError, match="Event loop stopped"):
        response.close()


@pytest.mark.slow(20)
def test_continuous_valid_observation_progress_cannot_extend_attempt(
    https_peer, tmp_path: Path
) -> None:
    client, state = https_peer
    snapshot = _large_snapshot(tmp_path)
    frozen = serialize_json_value(snapshot)
    response = observation_response(snapshot, resource="fleet")

    async def collect() -> bytes:
        parts: list[bytes] = []
        async for part in response.body_iterator:
            parts.append(part.encode() if isinstance(part, str) else bytes(part))
        return b"".join(parts)

    original = asyncio.run(collect()).splitlines()
    start = ObservationTransferStart.model_validate_json(original[0], strict=True)
    raw = b"".join(
        base64.b64decode(
            ObservationTransferChunk.model_validate_json(line, strict=True).data,
            validate=True,
        )
        for line in original[1:-1]
    )
    # Smaller legal fragments expose repeated record progress during one
    # attempt; this fixture packet size is not a domain or transfer-size cap.
    fragments = [raw[offset : offset + 4096] for offset in range(0, len(raw), 4096)]
    records = [
        start,
        *(
            ObservationTransferChunk(
                type="chunk",
                transfer_id=start.transfer_id,
                ordinal=ordinal,
                data=base64.b64encode(fragment).decode("ascii"),
            )
            for ordinal, fragment in enumerate(fragments)
        ),
        ObservationTransferComplete(
            type="complete",
            transfer_id=start.transfer_id,
            chunks=len(fragments),
            bytes=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
        ),
    ]
    body = b"".join(_record(record) for record in records)
    state.update(
        status=200,
        stage="records",
        body=body,
        media_type=OBSERVATION_MEDIA_TYPE,
        records_sent=0,
    )
    started = time.monotonic()
    with pytest.raises(ControlTransportError) as failure:
        client.fleet()
    assert time.monotonic() - started < 0.75
    context = failure.value.context
    assert context is not None and context.transport == "timeout"
    assert context.http_status == 200 and context.request_id == "deadline-fixture"
    assert failure.value.retry_after_seconds == 120
    assert state["records_sent"] > 2, "no repeated valid-record progress was exercised"
    assert len(state["calls"]) == 1
    assert state["closed"].wait(1), "expired observation left the connection open"
    # A fresh read-only attempt gets its own normal caller budget, then adopts
    # the same complete frozen document, without partial membership or reapply.
    state.update(stage="fast")
    repaired = ControlClient(state["url"], Path(state["token"]))
    assert repaired.request("GET", "/api/fleet") == frozen
    assert len(state["calls"]) == 2


def test_sigint_during_executor_shutdown_finishes_cleanup(monkeypatch):
    """Ctrl-C at coroutine creation must not abandon Runner shutdown."""

    import inspect

    transport, request = _wedged_loop_response(monkeypatch, interrupt=False)

    async def closes(_self):
        pass

    monkeypatch.setattr(transport.HTTPSResponse, "_close", closes)
    response = transport.HTTPSResponse(request, 1)
    loop = response._runner.get_loop()
    shutdown = loop.shutdown_default_executor
    coroutines = []

    def interrupt_shutdown(timeout=None):
        work = shutdown(timeout=timeout)
        coroutines.append(work)
        signal.raise_signal(signal.SIGINT)
        return work

    monkeypatch.setattr(loop, "shutdown_default_executor", interrupt_shutdown)
    with pytest.raises(KeyboardInterrupt):
        response.close()
    assert response.closed and loop.is_closed()
    assert all(
        inspect.getcoroutinestate(work) == inspect.CORO_CLOSED for work in coroutines
    )
    assert signal.getsignal(signal.SIGINT) == signal.default_int_handler
    with asyncio.Runner() as runner:
        assert runner.run(asyncio.sleep(0, result=42)) == 42
