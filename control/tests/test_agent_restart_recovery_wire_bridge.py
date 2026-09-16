"""A dead agent process resumes exact bytes through a fresh Controller claim."""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import subprocess
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from threading import Event, Lock, Thread
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import AgentResult, DistributionAssignment
from vonk_agent_protocol.contracts import ArtifactDistributionPayload
from vonk_control.agent_jobs import AgentJobService
from vonk_control.distribution import DistributionService, MemoryVerifiedObjectSource
from vonk_control.models import AgentCertificate, AgentNode, AgentOperation, Base

from .runtime_identity_support import PACKAGED_RUNTIME_IDENTITY, claim_agent
from .test_agent_jobs_postgres import NODE_A, NODE_B, Clock, parent


@pytest.fixture
def controller(request):
    # A disposable externally started PostgreSQL container lets the same
    # Linux probe run in OrbStack without nesting Docker inside its container.
    dsn = os.environ.get("VONK_RESTART_TEST_DSN")
    engine = create_engine(dsn) if dsn else request.getfixturevalue("postgres_engine")
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    clock = Clock()
    clock.now = datetime.now(UTC)
    with sessions.begin() as session:
        for node_id in (NODE_A, NODE_B):
            session.add(
                AgentNode(
                    node_id=node_id,
                    state="active",
                    capabilities=[],
                    workload_intent_ordinal=1,
                )
            )
        session.flush()
        for node_id, serial in ((NODE_A, "serial-a"), (NODE_B, "serial-b")):
            session.add(
                AgentCertificate(
                    serial=serial,
                    node_id=node_id,
                    not_before=clock.now - timedelta(minutes=1),
                    not_after=clock.now + timedelta(hours=2),
                    fingerprint=f"fingerprint-{serial}",
                )
            )
    try:
        yield sessions, clock
    finally:
        if dsn:
            engine.dispose()


def _certificate_files(root: Path) -> dict[str, Path]:
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "restart probe CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    files = {"ca_pem": root / "ca.pem", "chain_pem": root / "chain.pem"}
    files["ca_pem"].write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    files["chain_pem"].write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    for role, usage in (
        ("server", ExtendedKeyUsageOID.SERVER_AUTH),
        ("client", ExtendedKeyUsageOID.CLIENT_AUTH),
    ):
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, role)])
        builder = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(ca_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=2))
            .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
        )
        if role == "server":
            builder = builder.add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.DNSName("localhost"),
                        x509.IPAddress(ip_address("127.0.0.1")),
                    ]
                ),
                critical=False,
            )
        certificate = builder.sign(ca_key, hashes.SHA256())
        cert_path = root / f"{role}.pem"
        key_path = root / f"{role}.key"
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        files[f"{role}_pem"] = cert_path
        files[f"{role}_key"] = key_path
    files["certificate_pem"] = files["client_pem"]
    files["private_key_pem"] = files["client_key"]
    return files


def _assignment(source: MemoryVerifiedObjectSource, now: datetime):
    large = b"model shard\n" * 500_000
    small = b"config!"
    archive = b"oci archive"
    large_digest = source.put(large)
    small_digest = source.put(small)
    archive_digest = source.put(archive)
    plan_digest = "a" * 64
    image_digest = "sha256:" + "d" * 64
    assignment = DistributionAssignment.parse(
        {
            "schema_version": 2,
            "assignment_id": str(uuid4()),
            "plan_digest": plan_digest,
            "generation": 1,
            "node_id": NODE_A,
            "expires_at": (now + timedelta(hours=1)).isoformat(),
            "model_artifact_set_sha256": "b" * 64,
            "objects": [
                {
                    "name": "weights/shard.bin",
                    "sha256": large_digest,
                    "bytes": len(large),
                    "kind": "model",
                },
                {
                    "name": "config/tokenizer.json",
                    "sha256": small_digest,
                    "bytes": len(small),
                    "kind": "model",
                },
                {
                    "name": "image.oci.tar",
                    "sha256": archive_digest,
                    "bytes": len(archive),
                    "kind": "oci-archive",
                },
            ],
            "oci_image_digest": image_digest,
            "oci_archive_sha256": archive_digest,
        }
    )
    source.register_artifact_set(
        assignment.model_artifact_set_sha256, assignment.objects
    )
    source.register_runtime_image(image_digest, archive_digest)
    return assignment, large_digest, small_digest, archive_digest


class DistributionServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service, large_digest: str):
        super().__init__(address, DistributionHandler)
        self.service = service
        self.large_digest = large_digest
        self.partial_sent = Event()
        self.release_partial = Event()
        self.requests: list[tuple[str, int, int]] = []
        self.requests_lock = Lock()


class DistributionHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        server: DistributionServer = self.server  # type: ignore[assignment]
        target = urlsplit(self.path)
        digest = target.path.rsplit("/", 1)[-1]
        plan_digest = parse_qs(target.query).get("plan_digest", [None])[0]
        if target.path.startswith("/agent/distribution/manifests/"):
            assignment = server.service.authorize(node_id=NODE_A, plan_digest=digest)
            body = json.dumps(assignment.to_mapping()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", f'"plan:{digest}"')
            self.end_headers()
            self.wfile.write(body)
            return
        if (
            not target.path.startswith("/agent/distribution/objects/")
            or plan_digest is None
        ):
            self.send_error(404)
            return
        _assignment, spec, opened = server.service.open_object(
            node_id=NODE_A, plan_digest=plan_digest, digest=digest
        )
        try:
            body = opened.stream.read()
        finally:
            opened.stream.close()
        match = re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers.get("Range", ""))
        if match is None:
            self.send_error(416)
            return
        start, end = map(int, match.groups())
        if start > end or end >= spec.bytes:
            self.send_error(416)
            return
        with server.requests_lock:
            server.requests.append((digest, start, end))
        selected = body[start : end + 1]
        self.send_response(206)
        self.send_header("Content-Length", str(len(selected)))
        self.send_header("Content-Range", f"bytes {start}-{end}/{spec.bytes}")
        self.send_header("ETag", f'"sha256:{digest}"')
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        if (
            digest == server.large_digest
            and start == 0
            and not server.partial_sent.is_set()
        ):
            self.wfile.write(selected[:3_000_000])
            self.wfile.flush()
            server.partial_sent.set()
            server.release_partial.wait(timeout=30)
            selected = selected[3_000_000:]
        try:
            self.wfile.write(selected)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLEOFError):
            pass

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture
def distribution_https(tmp_path: Path, controller):
    sessions, clock = controller
    source = MemoryVerifiedObjectSource()
    assignment, large_digest, small_digest, archive_digest = _assignment(
        source, clock.now
    )
    service = DistributionService(source, clock=clock, sessions=sessions)
    service.register(assignment)
    certs = _certificate_files(tmp_path)
    server = DistributionServer(("127.0.0.1", 0), service, large_digest)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certs["server_pem"], certs["server_key"])
    context.load_verify_locations(cafile=str(certs["ca_pem"]))
    context.verify_mode = ssl.CERT_REQUIRED
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, certs, assignment, (large_digest, small_digest, archive_digest)
    finally:
        server.release_partial.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="session")
def restart_probe() -> Path:
    path = os.environ.get("VONK_RESTART_RECOVERY_PROBE")
    if path is None:
        root = Path(__file__).resolve().parents[2]
        subprocess.run(
            [
                "cargo",
                "build",
                "--locked",
                "-p",
                "vonk-agent",
                "--example",
                "restart_recovery_probe",
            ],
            cwd=root,
            check=True,
        )
        path = str(
            Path(os.environ.get("CARGO_TARGET_DIR", root / "target"))
            / "debug/examples/restart_recovery_probe"
        )
    probe = Path(path)
    assert probe.is_file() and os.access(probe, os.X_OK)
    return probe


def _probe_request(
    mode: str, claim, root: Path, server, certs: dict[str, Path]
) -> dict[str, object]:
    return {
        "mode": mode,
        "data_root": str(root),
        "claim": claim.model_dump(mode="json"),
        "controller_url": f"https://localhost:{server.server_port}/",
        "ca_sha256": hashlib.sha256(
            x509.load_pem_x509_certificate(certs["ca_pem"].read_bytes()).public_bytes(
                serialization.Encoding.DER
            )
        ).hexdigest(),
        **{
            key: str(certs[key])
            for key in ("ca_pem", "certificate_pem", "chain_pem", "private_key_pem")
        },
    }


def _run_probe(probe: Path, document: dict[str, object]) -> AgentResult:
    completed = subprocess.run(
        [str(probe)],
        input=json.dumps(document),
        text=True,
        capture_output=True,
        check=False,
        timeout=40,
    )
    assert completed.returncode == 0, completed.stderr
    return AgentResult.model_validate_json(completed.stdout)


@pytest.mark.parametrize("expired_before_recovery", [False, True])
def test_dead_agent_resumes_partial_transfer_from_fresh_controller_claim(
    expired_before_recovery: bool,
    controller,
    distribution_https,
    restart_probe: Path,
    tmp_path: Path,
) -> None:
    sessions, clock = controller
    server, certs, assignment, digests = distribution_https
    large_digest, small_digest, archive_digest = digests
    jobs = AgentJobService(sessions, clock=clock)
    parent_job = parent(sessions, clock)
    operation = jobs.enqueue(
        parent_job.id,
        NODE_A,
        "artifact.distribution.v1",
        assignment.plan_digest,
        ArtifactDistributionPayload(
            schema_version=1,
            authority_revision=assignment.plan_digest,
            plan_digest=assignment.plan_digest,
        ).model_dump(mode="json"),
    )
    first = claim_agent(
        jobs, NODE_A, "serial-a", 30, runtime_identity=PACKAGED_RUNTIME_IDENTITY
    )
    assert first is not None and first.operation_id == operation.id
    data_root = tmp_path / "agent-data"
    first_process = subprocess.Popen(
        [str(restart_probe)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert first_process.stdin is not None
    first_process.stdin.write(
        json.dumps(
            _probe_request("execute-distribution", first, data_root, server, certs)
        )
    )
    first_process.stdin.close()
    partial = data_root / "distribution/models" / f"{large_digest}.partial"
    small = data_root / "distribution/models" / small_digest
    archive = data_root / "oci-archives" / archive_digest
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if (
                server.partial_sent.is_set()
                and partial.exists()
                and partial.stat().st_size > 0
                and small.exists()
                and archive.exists()
            ):
                break
            if first_process.poll() is not None:
                raise AssertionError(
                    first_process.stderr.read()
                    if first_process.stderr
                    else "probe exited"
                )
            time.sleep(0.05)
        else:
            raise AssertionError(
                "agent did not persist partial bytes and completed objects"
            )
    finally:
        first_process.kill()
        first_process.wait(timeout=5)
        server.release_partial.set()
    # Read the checkpoint only after the writer is gone.  The agent buffers
    # range bytes, so the partial keeps growing between the readiness check
    # above and the kill; the resumed process resumes from the durable on-disk
    # length, which is this post-kill size and never an earlier sample.
    saved_bytes = partial.stat().st_size
    assert 0 < saved_bytes < 6_000_000

    interrupted = _run_probe(
        restart_probe, _probe_request("recover", first, data_root, server, certs)
    )
    assert interrupted.operation_id == first.operation_id
    assert interrupted.state == "waiting-for-operator"
    assert interrupted.result["error_code"] == "agent_restart_interrupted"
    assert interrupted.result["failure_kind"] == "uncertain-effect"
    assert interrupted.result["uncertain"] is True
    if expired_before_recovery:
        clock.now = first.deadline + timedelta(seconds=1)
        assert (
            claim_agent(
                jobs, NODE_A, "serial-a", 30, runtime_identity=PACKAGED_RUNTIME_IDENTITY
            )
            is None
        )
        assert jobs.record_late_result(interrupted)
    else:
        jobs.record_result(interrupted)
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None and stored.retry_due_at is not None
        due = stored.retry_due_at
    clock.now = due.replace(tzinfo=UTC) + timedelta(seconds=1)
    second = claim_agent(
        jobs, NODE_A, "serial-a", 30, runtime_identity=PACKAGED_RUNTIME_IDENTITY
    )
    assert second is not None and second.operation_id == first.operation_id
    assert second.attempt == first.attempt + 1 and second.fence != first.fence
    completed = _run_probe(
        restart_probe,
        _probe_request("execute-distribution", second, data_root, server, certs),
    )
    assert completed.state == "succeeded"
    jobs.record_result(completed)
    with sessions() as session:
        stored = session.get(AgentOperation, operation.id)
        assert stored is not None and stored.state == "succeeded"
    with server.requests_lock:
        requests = tuple(server.requests)
    assert any(
        digest == large_digest and start == saved_bytes for digest, start, _ in requests
    )
    assert sum(digest == small_digest for digest, _, _ in requests) == 1
    assert sum(digest == archive_digest for digest, _, _ in requests) == 1
    assert (
        hashlib.sha256(
            (data_root / "distribution/models" / large_digest).read_bytes()
        ).hexdigest()
        == large_digest
    )
    assert hashlib.sha256(small.read_bytes()).hexdigest() == small_digest
    assert completed.result["verified_digests"] == [large_digest, small_digest]
    assert not partial.exists()
