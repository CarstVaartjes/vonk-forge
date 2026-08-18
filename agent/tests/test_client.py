from __future__ import annotations

import hashlib
import json
import socket
import ssl
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import vonk_agent.client as client_module
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from vonk_agent.client import (
    AgentAuthenticationError,
    AgentClient,
    AgentPermanentError,
    AgentProtocolResponseError,
    AgentRuntimeIdentity,
    AgentTransportError,
    CredentialStore,
    CredentialStoreError,
    EnrollmentPending,
    IssuedCredential,
    StaticCredentialProvider,
)
from vonk_agent_protocol import (
    AgentClaim,
    AgentDirective,
    AgentOperation,
    AgentProgress,
    AgentResult,
    canonical_message,
)

NODE_ID = "spk_0123456789abcdef0123456789abcdef"


@dataclass(frozen=True)
class ResponseSpec:
    status: int
    body: bytes = b""
    content_type: str | None = None
    close_without_response: bool = False


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes
    peer_certificate: bool


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, context: ssl.SSLContext) -> None:
        super().__init__(address, _Handler)
        self.socket = context.wrap_socket(self.socket, server_side=True)
        self.responses: list[ResponseSpec] = []
        self.requests: list[RecordedRequest] = []


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        server = self.server
        assert isinstance(server, _Server)
        server.requests.append(
            RecordedRequest(
                method="POST",
                path=self.path,
                headers={key.lower(): value for key, value in self.headers.items()},
                body=body,
                peer_certificate=bool(self.connection.getpeercert()),
            )
        )
        response = server.responses.pop(0)
        if response.close_without_response:
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        self.send_response(response.status)
        if response.content_type is not None:
            self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if response.body:
            self.wfile.write(response.body)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


@dataclass(frozen=True)
class TLSFiles:
    ca: Path
    server_certificate: Path
    server_key: Path
    client_certificate: Path
    client_key: Path
    ca_certificate_object: x509.Certificate
    ca_private_key: ed25519.Ed25519PrivateKey


def _pem(path: Path, value: bytes, mode: int) -> Path:
    path.write_bytes(value)
    path.chmod(mode)
    return path


def tls_files(tmp_path: Path, *, server_name: str = "localhost") -> TLSFiles:
    now = datetime.now(UTC)
    ca_key = ed25519.Ed25519PrivateKey.generate()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agent-test-ca")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, algorithm=None)
    )

    def issue(common_name: str, eku: ExtendedKeyUsageOID, *, dns: str | None = None):
        key = ed25519.Ed25519PrivateKey.generate()
        builder = (
            x509.CertificateBuilder()
            .subject_name(
                x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
            )
            .issuer_name(ca.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
        )
        if dns is not None:
            builder = builder.add_extension(
                x509.SubjectAlternativeName([x509.DNSName(dns)]), critical=False
            )
        return key, builder.sign(ca_key, algorithm=None)

    server_key, server = issue(
        server_name, ExtendedKeyUsageOID.SERVER_AUTH, dns=server_name
    )
    client_key, client = issue(NODE_ID, ExtendedKeyUsageOID.CLIENT_AUTH)
    return TLSFiles(
        ca=_pem(
            tmp_path / "ca.pem", ca.public_bytes(serialization.Encoding.PEM), 0o644
        ),
        server_certificate=_pem(
            tmp_path / "server.pem",
            server.public_bytes(serialization.Encoding.PEM),
            0o644,
        ),
        server_key=_pem(
            tmp_path / "server.key",
            server_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
            0o600,
        ),
        client_certificate=_pem(
            tmp_path / "client.pem",
            client.public_bytes(serialization.Encoding.PEM),
            0o644,
        ),
        client_key=_pem(
            tmp_path / "client.key",
            client_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
            0o600,
        ),
        ca_certificate_object=ca,
        ca_private_key=ca_key,
    )


@contextmanager
def https_server(
    files: TLSFiles, *, require_client: bool = True, port: int = 0
) -> Iterator[_Server]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(files.server_certificate, files.server_key)
    if require_client:
        context.verify_mode = ssl.CERT_REQUIRED
        context.load_verify_locations(files.ca)
    server = _Server(("127.0.0.1", port), context)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def probe_claim() -> AgentClaim:
    payload: dict[str, object] = {}
    return AgentClaim(
        schema_version=1,
        job_id=str(uuid.uuid4()),
        operation_id=str(uuid.uuid4()),
        attempt=1,
        fence=str(uuid.uuid4()),
        node_id=NODE_ID,
        operation=AgentOperation.NODE_PROBE,
        base_commit="a" * 40,
        payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
        payload=payload,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )


def progress(claim: AgentClaim) -> AgentProgress:
    return AgentProgress(
        schema_version=claim.schema_version,
        job_id=claim.job_id,
        operation_id=claim.operation_id,
        attempt=claim.attempt,
        fence=claim.fence,
        node_id=claim.node_id,
        deadline=claim.deadline,
        progress={"phase": "collecting"},
    )


def result(claim: AgentClaim) -> AgentResult:
    return AgentResult(
        schema_version=claim.schema_version,
        job_id=claim.job_id,
        operation_id=claim.operation_id,
        attempt=claim.attempt,
        fence=claim.fence,
        node_id=claim.node_id,
        deadline=claim.deadline,
        state="succeeded",
        result={"status": "ok"},
    )


def issued_document(*, generation: int = 2) -> dict[str, object]:
    return {
        "certificate_pem": "-----BEGIN CERTIFICATE-----\nYQ==\n-----END CERTIFICATE-----\n",
        "chain_pem": "-----BEGIN CERTIFICATE-----\nYg==\n-----END CERTIFICATE-----\n",
        "fingerprint": "f" * 64,
        "generation": generation,
        "node_id": NODE_ID,
        "not_after": "2026-08-05T12:00:00+00:00",
        "not_before": "2026-08-04T12:00:00+00:00",
        "serial": "1234",
    }


def issue_rotation(
    files: TLSFiles,
    csr_pem: bytes,
    *,
    generation: int = 2,
) -> IssuedCredential:
    request = x509.load_pem_x509_csr(csr_pem)
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(request.subject)
        .issuer_name(files.ca_certificate_object.subject)
        .public_key(request.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            request.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value,
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
        )
        .sign(files.ca_private_key, algorithm=None)
    )
    return IssuedCredential(
        node_id=NODE_ID,
        certificate_pem=certificate.public_bytes(serialization.Encoding.PEM),
        chain_pem=files.ca_certificate_object.public_bytes(serialization.Encoding.PEM),
        serial=str(certificate.serial_number),
        fingerprint=hashlib.sha256(
            certificate.public_bytes(serialization.Encoding.DER)
        ).hexdigest(),
        not_before=certificate.not_valid_before_utc,
        not_after=certificate.not_valid_after_utc,
        generation=generation,
    )


def client_for(server: _Server, files: TLSFiles) -> AgentClient:
    credentials = StaticCredentialProvider(
        files.ca, files.client_certificate, files.client_key
    )
    return AgentClient(
        f"https://localhost:{server.server_port}",
        NODE_ID,
        credentials,
        connect_timeout=1,
        read_timeout=1,
        long_poll_seconds=1,
        lease_seconds=30,
    )


@pytest.mark.parametrize(
    "origin",
    [
        "https://127.1",
        "https://2130706433",
        "https://LOCALHOST",
        "https://[0:0:0:0:0:0:0:1]",
        "https://localhost/",
    ],
)
def test_client_rejects_ambiguous_or_noncanonical_runtime_origins(
    tmp_path: Path, origin: str
) -> None:
    files = tls_files(tmp_path)

    with pytest.raises(ValueError, match="origin"):
        AgentClient(
            origin,
            NODE_ID,
            StaticCredentialProvider(
                files.ca, files.client_certificate, files.client_key
            ),
        )


def test_claim_uses_fixed_mtls_post_and_parses_canonical_protocol_claim(
    tmp_path: Path,
) -> None:
    files = tls_files(tmp_path)
    expected = probe_claim()
    with https_server(files) as server:
        server.responses.append(
            ResponseSpec(200, canonical_message(expected), "application/json")
        )

        claimed = client_for(server, files).claim()

    assert claimed == expected
    request = server.requests[0]
    assert request.method == "POST"
    assert request.path == "/agent/v1/claim"
    assert request.headers["content-type"] == "application/json"
    assert request.body == canonical_message(
        {
            "agent_implementation": "python",
            "capabilities": [
                "agent.rollback",
                "agent.update",
                "node.probe",
                "release.install",
                "workload.health",
                "workload.prepare",
                "workload.start",
                "workload.stop",
                "workload.verify",
            ],
            "lease_seconds": 30,
            "node_id": NODE_ID,
            "protocol_version": 2,
            "wait_seconds": 1,
        }
    )


def test_claim_advertises_verified_running_release_identity(tmp_path: Path) -> None:
    files = tls_files(tmp_path)
    identity = AgentRuntimeIdentity(
        architecture="linux-arm64",
        platform_version="1.2.3",
        build_digest="sha256:" + "b" * 64,
        active_slot="B",
        agent_sha256="c" * 64,
        supervisor_generation=7,
    )
    with https_server(files) as server:
        server.responses.append(
            ResponseSpec(204, b"", "application/json")
        )
        client = AgentClient(
            f"https://localhost:{server.server_port}",
            NODE_ID,
            StaticCredentialProvider(
                files.ca, files.client_certificate, files.client_key
            ),
            connect_timeout=1,
            read_timeout=1,
            runtime_identity=identity,
        )

        assert client.claim() is None

    document = json.loads(server.requests[0].body)
    assert document["runtime_identity"] == identity.wire()


@pytest.mark.parametrize(
    ("machine", "expected"),
    (("aarch64", "linux-arm64"), ("x86_64", "linux-x86_64")),
)
def test_runtime_identity_normalizes_local_machine_architecture(
    monkeypatch, machine: str, expected: str
) -> None:
    monkeypatch.setenv("VONK_AGENT_PLATFORM_VERSION", "1.2.3")
    monkeypatch.setenv("VONK_AGENT_BUILD_DIGEST", "sha256:" + "b" * 64)
    monkeypatch.setenv("VONK_AGENT_SUPERVISOR_SLOT", "B")
    monkeypatch.setenv("VONK_AGENT_SUPERVISOR_SHA256", "c" * 64)
    monkeypatch.setenv("VONK_AGENT_SUPERVISOR_GENERATION", "7")

    identity = AgentRuntimeIdentity.from_environment(machine=lambda: machine)

    assert identity.architecture == expected
    assert identity.wire()["architecture"] == expected
    assert identity.wire()["self_test_passed"] is True
    assert identity.wire()["supervisor_ready_generation"] == 7


def test_runtime_identity_rejects_unknown_local_machine(monkeypatch) -> None:
    monkeypatch.setenv("VONK_AGENT_PLATFORM_VERSION", "1.2.3")
    monkeypatch.setenv("VONK_AGENT_BUILD_DIGEST", "sha256:" + "b" * 64)
    monkeypatch.setenv("VONK_AGENT_SUPERVISOR_SLOT", "B")
    monkeypatch.setenv("VONK_AGENT_SUPERVISOR_SHA256", "c" * 64)
    monkeypatch.setenv("VONK_AGENT_SUPERVISOR_GENERATION", "7")

    with pytest.raises(ValueError, match="runtime identity is unavailable"):
        AgentRuntimeIdentity.from_environment(machine=lambda: "riscv64")


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (ResponseSpec(200, b"{}", "text/json"), "content type"),
        (ResponseSpec(200, b'{"x":1,"x":2}', "application/json"), "duplicate"),
        (ResponseSpec(200, b'{ "x":1}', "application/json"), "canonical"),
        (ResponseSpec(200, b"x" * (64 * 1024 + 1), "application/json"), "large"),
    ],
)
def test_claim_rejects_wrong_content_type_duplicate_noncanonical_and_oversized_responses(
    tmp_path: Path, response: ResponseSpec, message: str
) -> None:
    files = tls_files(tmp_path)
    with https_server(files) as server:
        server.responses.append(response)
        with pytest.raises(AgentProtocolResponseError, match=message):
            client_for(server, files).claim()


def test_claim_reports_disconnect_as_retryable_transport_failure(
    tmp_path: Path,
) -> None:
    files = tls_files(tmp_path)
    with https_server(files) as server:
        server.responses.append(ResponseSpec(0, close_without_response=True))
        with pytest.raises(AgentTransportError):
            client_for(server, files).claim()


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_retryable_http_statuses_are_transport_failures(
    tmp_path: Path, status: int
) -> None:
    files = tls_files(tmp_path)
    with https_server(files) as server:
        server.responses.append(
            ResponseSpec(
                status,
                canonical_message({"detail": "temporarily unavailable"}),
                "application/json",
            )
        )
        with pytest.raises(AgentTransportError):
            client_for(server, files).claim()


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, AgentAuthenticationError),
        (422, AgentPermanentError),
        (503, AgentTransportError),
    ],
)
def test_explicit_error_status_wins_over_non_json_intermediary_body(
    tmp_path: Path, status: int, error: type[Exception]
) -> None:
    files = tls_files(tmp_path)
    with https_server(files) as server:
        server.responses.append(ResponseSpec(status, b"not JSON", "text/plain"))

        with pytest.raises(error):
            client_for(server, files).claim()


def test_client_recovers_after_server_restart_without_reusing_connection(
    tmp_path: Path,
) -> None:
    files = tls_files(tmp_path)
    with https_server(files) as first:
        port = first.server_port
        first.responses.append(ResponseSpec(0, close_without_response=True))
        client = client_for(first, files)
        with pytest.raises(AgentTransportError):
            client.claim()

    with https_server(files, port=port) as restarted:
        restarted.responses.append(ResponseSpec(204))
        assert client.claim() is None


def test_repeated_requests_close_connections_descriptors_and_server_threads(
    tmp_path: Path,
) -> None:
    files = tls_files(tmp_path)
    baseline_descriptors = len(tuple(Path("/proc/self/fd").iterdir()))
    baseline_threads = {thread.ident for thread in threading.enumerate()}

    with https_server(files) as server:
        server.responses.extend(ResponseSpec(204) for _ in range(32))
        client = client_for(server, files)
        for _ in range(32):
            assert client.claim() is None

    assert len(tuple(Path("/proc/self/fd").iterdir())) == baseline_descriptors
    assert {thread.ident for thread in threading.enumerate()} == baseline_threads


def test_runtime_tls_rejects_wrong_ca_hostname_and_missing_client_certificate(
    tmp_path: Path,
) -> None:
    files = tls_files(tmp_path)
    with https_server(files) as server:
        server.responses.append(ResponseSpec(204))
        without_identity = StaticCredentialProvider(files.ca, None, None)
        client = AgentClient(
            f"https://localhost:{server.server_port}",
            NODE_ID,
            without_identity,
            connect_timeout=1,
            read_timeout=1,
            long_poll_seconds=1,
        )
        with pytest.raises(AgentTransportError):
            client.claim()

    untrusted_root = tmp_path / "untrusted-root"
    untrusted_root.mkdir()
    untrusted = tls_files(untrusted_root)
    with https_server(files) as server:
        server.responses.append(ResponseSpec(204))
        wrong_ca = AgentClient(
            f"https://localhost:{server.server_port}",
            NODE_ID,
            StaticCredentialProvider(
                untrusted.ca,
                untrusted.client_certificate,
                untrusted.client_key,
            ),
            connect_timeout=1,
            read_timeout=1,
            long_poll_seconds=1,
        )
        with pytest.raises(AgentTransportError):
            wrong_ca.claim()

    alternate = tmp_path / "alternate"
    alternate.mkdir()
    mismatched = tls_files(alternate, server_name="not-localhost")
    with https_server(mismatched) as server:
        server.responses.append(ResponseSpec(204))
        with pytest.raises(AgentTransportError):
            client_for(server, mismatched).claim()


def test_enrollment_uses_explicit_server_authenticated_origin_without_client_identity(
    tmp_path: Path,
) -> None:
    files = tls_files(tmp_path)
    evidence = {
        "agent_digest": "a" * 64,
        "boot_id": "boot",
        "csr_public_key_fingerprint": "b" * 64,
        "hardware_fingerprint": "hardware",
        "host_key_fingerprint": "host",
        "node_id": NODE_ID,
    }
    with https_server(files, require_client=False) as enrollment_server:
        pending_body = {
            "id": str(uuid.uuid4()),
            "node_id": NODE_ID,
            "state": "pending-approval",
        }
        enrollment_server.responses.append(
            ResponseSpec(202, canonical_message(pending_body), "application/json")
        )
        runtime_origin = "https://runtime.invalid"
        client = AgentClient(
            runtime_origin,
            NODE_ID,
            StaticCredentialProvider(
                files.ca, files.client_certificate, files.client_key
            ),
            connect_timeout=1,
            read_timeout=1,
        )

        response = client.enroll(
            f"https://localhost:{enrollment_server.server_port}",
            "t" * 43,
            b"-----BEGIN CERTIFICATE REQUEST-----\nYQ==\n-----END CERTIFICATE REQUEST-----\n",
            evidence,
        )

    assert isinstance(response, EnrollmentPending)
    request = enrollment_server.requests[0]
    assert request.path == "/agent/v1/enroll"
    assert request.peer_certificate is False
    assert json.loads(request.body) == {
        "csr": "-----BEGIN CERTIFICATE REQUEST-----\nYQ==\n-----END CERTIFICATE REQUEST-----\n",
        "evidence": evidence,
        "grant_token": "t" * 43,
    }


def test_enrollment_returns_issued_certificate_on_approved_pickup(
    tmp_path: Path,
) -> None:
    files = tls_files(tmp_path)
    with https_server(files, require_client=False) as server:
        server.responses.append(
            ResponseSpec(
                200,
                canonical_message(issued_document(generation=1)),
                "application/json",
            )
        )
        client = AgentClient(
            "https://runtime.invalid",
            NODE_ID,
            StaticCredentialProvider(
                files.ca, files.client_certificate, files.client_key
            ),
            connect_timeout=1,
            read_timeout=1,
        )
        response = client.enroll(
            f"https://localhost:{server.server_port}",
            "t" * 43,
            b"-----BEGIN CERTIFICATE REQUEST-----\nYQ==\n-----END CERTIFICATE REQUEST-----\n",
            {
                "agent_digest": "a" * 64,
                "boot_id": "boot",
                "csr_public_key_fingerprint": "b" * 64,
                "hardware_fingerprint": "hardware",
                "host_key_fingerprint": "host",
                "node_id": NODE_ID,
            },
        )

    assert isinstance(response, IssuedCredential)
    assert response.generation == 1
    assert response.node_id == NODE_ID


def test_heartbeat_parses_protocol_progress_and_result_accepts_204_or_stale_409(
    tmp_path: Path,
) -> None:
    files = tls_files(tmp_path)
    claim = probe_claim()
    message = progress(claim)
    directive = AgentDirective(
        schema_version=message.schema_version,
        job_id=message.job_id,
        operation_id=message.operation_id,
        attempt=message.attempt,
        fence=message.fence,
        node_id=message.node_id,
        deadline=message.deadline + timedelta(seconds=30),
        cancel_requested=True,
    )
    terminal = result(claim)
    with https_server(files) as server:
        server.responses.extend(
            [
                ResponseSpec(200, canonical_message(directive), "application/json"),
                ResponseSpec(204),
                ResponseSpec(409, b"stale fence", "text/plain"),
            ]
        )
        client = client_for(server, files)

        assert client.heartbeat(message) == directive
        client.result(terminal)
        client.result(terminal)

    assert [request.path for request in server.requests] == [
        "/agent/v1/heartbeat",
        "/agent/v1/result",
        "/agent/v1/result",
    ]
    assert server.requests[1].body == canonical_message(terminal)
    assert server.requests[2].body == canonical_message(terminal)


def test_result_transport_failure_replays_the_exact_payload_and_fence(
    tmp_path: Path,
) -> None:
    files = tls_files(tmp_path)
    terminal = result(probe_claim())
    with https_server(files) as server:
        server.responses.extend(
            [
                ResponseSpec(0, close_without_response=True),
                ResponseSpec(204),
            ]
        )
        client = client_for(server, files)
        with pytest.raises(AgentTransportError):
            client.result(terminal)
        client.result(terminal)

    assert len(server.requests) == 2
    assert (
        server.requests[0].body
        == server.requests[1].body
        == canonical_message(terminal)
    )


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, AgentAuthenticationError),
        (403, AgentAuthenticationError),
        (422, AgentPermanentError),
    ],
)
def test_terminal_client_errors_are_typed_and_not_silent(
    tmp_path: Path, status: int, error: type[Exception]
) -> None:
    files = tls_files(tmp_path)
    with https_server(files) as server:
        server.responses.append(
            ResponseSpec(
                status, canonical_message({"detail": "denied"}), "application/json"
            )
        )
        with pytest.raises(error):
            client_for(server, files).result(result(probe_claim()))


def test_renew_and_activation_use_fixed_paths_and_staged_identity(
    tmp_path: Path,
) -> None:
    files = tls_files(tmp_path)
    with https_server(files) as server:
        server.responses.extend(
            [
                ResponseSpec(
                    200, canonical_message(issued_document()), "application/json"
                ),
                ResponseSpec(204),
            ]
        )
        staged = StaticCredentialProvider(
            files.ca, files.client_certificate, files.client_key
        )
        client = AgentClient(
            f"https://localhost:{server.server_port}",
            NODE_ID,
            staged,
            connect_timeout=1,
            read_timeout=1,
            long_poll_seconds=1,
            lease_seconds=30,
        )
        issued = client.renew(
            b"-----BEGIN CERTIFICATE REQUEST-----\nYQ==\n-----END CERTIFICATE REQUEST-----\n"
        )
        client.activate(issued.generation, staged)

    assert issued.generation == 2
    assert [request.path for request in server.requests] == [
        "/agent/v1/renew",
        "/agent/v1/renew/activate",
    ]
    assert json.loads(server.requests[1].body) == {
        "generation": 2,
        "node_id": NODE_ID,
    }


def test_credential_store_stages_service_owned_generation_and_publishes_only_after_activation(
    tmp_path: Path,
) -> None:
    files = tls_files(tmp_path)
    state_root = tmp_path / "state"
    store = CredentialStore(
        state_root,
        files.ca,
        files.client_certificate,
        files.client_key,
    )
    pending = store.prepare_rotation(NODE_ID)
    assert store.prepare_rotation(NODE_ID).csr_pem == pending.csr_pem
    issued = issue_rotation(files, pending.csr_pem)

    store.stage(issued)

    generation_root = state_root / "credentials" / "generation-00000002"
    assert oct(generation_root.stat().st_mode & 0o777) == "0o700"
    assert {
        child.name: oct(child.stat().st_mode & 0o777)
        for child in generation_root.iterdir()
    } == {
        "certificate.pem": "0o600",
        "credential.json": "0o600",
        "private-key.pem": "0o600",
    }
    assert store.active_generation == 1
    assert store.staged_generation == 2

    restarted = CredentialStore(
        state_root,
        files.ca,
        files.client_certificate,
        files.client_key,
    )
    staged = restarted.staged_provider()
    assert staged is not None
    with staged.snapshot() as snapshot:
        assert snapshot.generation == 2
        assert snapshot.certificate_path is not None
        assert snapshot.private_key_path is not None
        assert (
            snapshot.certificate_path.read_bytes()
            == issued.certificate_pem + issued.chain_pem
        )
        stored_key = serialization.load_pem_private_key(
            snapshot.private_key_path.read_bytes(), password=None
        )
        assert stored_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        ) == x509.load_pem_x509_csr(pending.csr_pem).public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )

    restarted.publish_active(2)

    assert restarted.active_generation == 2
    assert restarted.staged_generation is None
    assert restarted.pending_rotation() is None


def test_credential_store_installs_initial_generation_without_seed_and_reuses_pending_key(
    tmp_path: Path,
) -> None:
    files = tls_files(tmp_path)
    state_root = tmp_path / "state"
    missing_certificate = tmp_path / "not-yet-enrolled.pem"
    missing_key = tmp_path / "not-yet-enrolled.key"
    store = CredentialStore(state_root, files.ca, missing_certificate, missing_key)
    pending = store.prepare_enrollment(NODE_ID)
    assert store.prepare_enrollment(NODE_ID).csr_pem == pending.csr_pem
    issued = issue_rotation(files, pending.csr_pem, generation=1)

    store.install_initial(issued)

    assert store.active_generation == 1
    assert store.pending_rotation() is None
    assert (state_root / "credentials/active.json").read_bytes() == b'{"generation":1}'
    with store.snapshot() as snapshot:
        assert snapshot.generation == 1
        assert snapshot.certificate_path is not None
        assert snapshot.private_key_path is not None
        assert (
            snapshot.certificate_path.read_bytes()
            == issued.certificate_pem + issued.chain_pem
        )

    # An identical approved pickup is idempotent after a response/restart boundary.
    CredentialStore(
        state_root, files.ca, missing_certificate, missing_key
    ).install_initial(issued)


@pytest.mark.parametrize("crash_call", [1, 2, 3, 4])
def test_initial_install_recovers_each_pending_cleanup_boundary_without_renewal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_call: int
) -> None:
    files = tls_files(tmp_path)
    state_root = tmp_path / "state"
    store = CredentialStore(
        state_root, files.ca, tmp_path / "missing.pem", tmp_path / "missing.key"
    )
    pending = store.prepare_enrollment(NODE_ID)
    issued = issue_rotation(files, pending.csr_pem, generation=1)
    original = client_module._unlink_optional
    calls = 0

    def crash_after_active(descriptor: int, name: str) -> None:
        nonlocal calls
        calls += 1
        if calls == crash_call:
            raise OSError("simulated cleanup crash")
        original(descriptor, name)

    monkeypatch.setattr(client_module, "_unlink_optional", crash_after_active)
    with pytest.raises(CredentialStoreError, match="could not be installed"):
        store.install_initial(issued)
    monkeypatch.setattr(client_module, "_unlink_optional", original)

    restarted = CredentialStore(
        state_root, files.ca, tmp_path / "missing.pem", tmp_path / "missing.key"
    )
    assert restarted.recover_initial_enrollment(NODE_ID) is True
    assert restarted.pending_rotation() is None
    assert restarted.active_generation == 1


def test_initial_credential_rejects_certificate_for_another_key(tmp_path: Path) -> None:
    files = tls_files(tmp_path)
    store = CredentialStore(
        tmp_path / "state", files.ca, tmp_path / "missing.pem", tmp_path / "missing.key"
    )
    store.prepare_enrollment(NODE_ID)
    unrelated = IssuedCredential(
        node_id=NODE_ID,
        certificate_pem=files.client_certificate.read_bytes(),
        chain_pem=files.ca.read_bytes(),
        serial="1",
        fingerprint="a" * 64,
        not_before=datetime.now(UTC) - timedelta(minutes=1),
        not_after=datetime.now(UTC) + timedelta(hours=1),
        generation=1,
    )

    with pytest.raises(CredentialStoreError, match="pending key"):
        store.install_initial(unrelated)


def test_credential_store_renews_at_one_third_remaining_and_closes_snapshot_descriptors(
    tmp_path: Path,
) -> None:
    files = tls_files(tmp_path)
    store = CredentialStore(
        tmp_path / "state",
        files.ca,
        files.client_certificate,
        files.client_key,
    )
    certificate = x509.load_pem_x509_certificate(files.client_certificate.read_bytes())
    lifetime = certificate.not_valid_after_utc - certificate.not_valid_before_utc
    threshold = certificate.not_valid_after_utc - lifetime / 3

    assert store.renewal_due(threshold - timedelta(microseconds=1)) is False
    assert store.renewal_due(threshold) is True

    baseline = len(tuple(Path("/proc/self/fd").iterdir()))
    for _ in range(32):
        with store.snapshot() as snapshot:
            assert snapshot.generation == 1
            assert snapshot.certificate_path is not None
            snapshot.certificate_path.read_bytes()
    assert len(tuple(Path("/proc/self/fd").iterdir())) == baseline


def test_credential_store_rejects_symlinked_state_root(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    files = tls_files(tmp_path)

    with pytest.raises(CredentialStoreError, match="state root"):
        CredentialStore(linked, files.ca, files.client_certificate, files.client_key)


def test_credential_store_restart_removes_only_orphan_staging_directories(
    tmp_path: Path,
) -> None:
    files = tls_files(tmp_path)
    state_root = tmp_path / "state"
    CredentialStore(
        state_root,
        files.ca,
        files.client_certificate,
        files.client_key,
    )
    credentials = state_root / "credentials"
    orphan = credentials / ".generation-00000002-deadbeefdeadbeef"
    orphan.mkdir(mode=0o700)
    (orphan / "private-key.pem").write_bytes(b"partial")
    (orphan / "private-key.pem").chmod(0o600)
    unrelated = credentials / ".operator-note"
    unrelated.write_text("preserve")
    unrelated.chmod(0o600)

    CredentialStore(
        state_root,
        files.ca,
        files.client_certificate,
        files.client_key,
    )

    assert not orphan.exists()
    assert unrelated.read_text() == "preserve"
