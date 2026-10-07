"""Real PostgreSQL and HTTPS prove claims do not hold unrelated node progress.

This fixture signs and durably stores fixed-policy leaves. The pinned Go CA
workflow separately proves its real Authority, journal and issuer epoch fence.
"""

from __future__ import annotations

import ipaddress
import json
import os
import ssl
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from jwt.algorithms import ECAlgorithm
from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker
from vonk_control.ca_issuance_contract import CertificateIssuanceBinding
from vonk_control.enrollment import (
    EnrollmentIssuanceUncertain,
    EnrollmentService,
    RenewalInProgress,
    RenewalIssuanceUncertain,
)
from vonk_control.models import AgentCertificate, AgentEnrollment, Base
from vonk_control.step_ca import StepCertificateAuthority

from .test_enrollment import OTHER_NODE_ID, csr, evidence
from .test_step_ca import NODE_ID, NOW, _leaf, _write_material


@contextmanager
def https_journal(tmp_path: Path):
    material = _write_material(tmp_path)
    # Real HTTPS uses strict chain validation; the older transport-only helper
    # lacks issuer key identifiers. Supply a complete test-only CA chain.
    root_key = ed25519.Ed25519PrivateKey.generate()
    intermediate_key = ed25519.Ed25519PrivateKey.generate()
    root_name = material["root"].subject
    intermediate_name = material["intermediate"].subject

    def ca_certificate(subject, issuer, key, issuer_key, path_length):
        return (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(NOW - timedelta(days=1))
            .not_valid_after(NOW + timedelta(days=365))
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=path_length), critical=True
            )
            .add_extension(
                x509.KeyUsage(
                    False, False, False, False, False, True, True, False, False
                ),
                critical=True,
            )
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                critical=False,
            )
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(
                    issuer_key.public_key()
                ),
                critical=False,
            )
            .sign(issuer_key, algorithm=None)
        )

    material["root"] = ca_certificate(root_name, root_name, root_key, root_key, 1)
    material["intermediate"] = ca_certificate(
        intermediate_name, root_name, intermediate_key, root_key, 0
    )
    material["intermediate_key"] = intermediate_key
    material["root_path"].write_bytes(
        material["root"].public_bytes(serialization.Encoding.PEM)
    )
    material["intermediate_path"].write_bytes(
        material["intermediate"].public_bytes(serialization.Encoding.PEM)
    )
    server_key = material["intermediate_key"]
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(material["intermediate"].subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(True, False, False, False, False, False, False, False, False),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(server_key.public_key()),
            critical=False,
        )
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(server_key, algorithm=None)
    )
    cert_path = tmp_path / "server-chain.pem"
    cert_path.write_bytes(
        server_cert.public_bytes(serialization.Encoding.PEM)
        + material["intermediate"].public_bytes(serialization.Encoding.PEM)
    )
    key_path = tmp_path / "server-key.pem"
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    guard = threading.Lock()
    pending: set[str] = set()
    counts: dict[str, int] = {}
    entered, release = threading.Event(), threading.Event()
    committed = threading.Event()
    control: dict[str, str | None] = {"pause_node": None, "lose_node": None}
    jtis: list[str] = []
    jwt_public_key = ECAlgorithm.from_jwk(json.dumps(material["public_jwk"]))
    assert isinstance(jwt_public_key, ec.EllipticCurvePublicKey)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, document):
            data = json.dumps(document).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ssl.SSLEOFError):
                pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            binding = CertificateIssuanceBinding.model_validate(body["request"])
            claims = jwt.decode(
                body["ott"],
                jwt_public_key,
                algorithms=["ES256"],
                issuer="vonk-forge-agent",
                audience=origin + "/1.0/sign",
                options={"verify_exp": False, "verify_nbf": False, "verify_iat": False},
            )
            assert claims["vonk"] == binding.model_dump(mode="json")
            assert claims["sub"] == binding.node_id
            with guard:
                assert claims["jti"] not in jtis
                jtis.append(claims["jti"])
                path = tmp_path / (binding.request_id + ".json")
                if path.exists():
                    stored = json.loads(path.read_bytes())
                    assert stored["request"] == binding.model_dump(mode="json")
                    self.reply(201, stored)
                    return
                if binding.request_id in pending:
                    self.reply(
                        200,
                        {
                            "state": "pending",
                            "request": binding.model_dump(mode="json"),
                        },
                    )
                    return
                if body["mode"] == "observe":
                    self.reply(
                        200,
                        {"state": "absent", "request": binding.model_dump(mode="json")},
                    )
                    return
                pending.add(binding.request_id)
                counts[binding.request_id] = counts.get(binding.request_id, 0) + 1
            if binding.node_id == control["pause_node"]:
                entered.set()
                assert release.wait(5)
            leaf = _leaf(
                body["csr"].encode(), material, now=NOW, serial=int(binding.serial)
            )
            leaf_pem = leaf.public_bytes(serialization.Encoding.PEM).decode()
            intermediate = (
                material["intermediate"]
                .public_bytes(serialization.Encoding.PEM)
                .decode()
            )
            result = {
                "state": "issued",
                "request": binding.model_dump(mode="json"),
                "crt": leaf_pem,
                "ca": intermediate,
                "certChain": [leaf_pem, intermediate],
            }
            with guard:
                path.write_text(json.dumps(result))
                pending.remove(binding.request_id)
                committed.set()
                lose = binding.node_id == control["lose_node"]
                if lose:
                    control["lose_node"] = None
            if lose:
                self.close_connection = True
                return
            self.reply(201, result)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    origin = f"https://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = StepCertificateAuthority(
        ca_url=origin,
        root_certificate_path=material["root_path"],
        intermediate_certificate_path=material["intermediate_path"],
        provisioner_name="vonk-forge-agent",
        provisioner_kid=material["kid"],
        credential_path=material["credential_path"],
        provisioner_public_jwk_path=material["public_jwk_path"],
        timeout_seconds=10,
    )
    try:
        yield provider, control, entered, release, counts, committed
    finally:
        release.set()
        provider._client.close()
        server.shutdown()
        server.server_close()
        thread.join(5)


def prepare(postgres_engine, provider):
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    return sessions, EnrollmentService(sessions, provider, clock=lambda: NOW)


def enroll(service, node_id):
    request = csr(node_id)
    grant = service.create(node_id, "admin", 600)
    return service.submit(grant.token, request, evidence(request, node_id=node_id))


@pytest.mark.parametrize("purpose", ("enrollment", "rotation"))
def test_slow_https_node_does_not_hold_an_unrelated_postgres_claim(
    postgres_engine, tmp_path, purpose
):
    """Catches a process lock held while one provider request waits externally."""
    with https_journal(tmp_path) as (provider, control, entered, release, counts, _):
        sessions, service = prepare(postgres_engine, provider)
        sources = (
            {node: enroll(service, node) for node in (NODE_ID, OTHER_NODE_ID)}
            if purpose == "rotation"
            else {}
        )
        requests = {node: csr(node) for node in (NODE_ID, OTHER_NODE_ID)}
        grants = (
            {node: service.create(node, "admin", 600) for node in requests}
            if purpose == "enrollment"
            else {}
        )

        def issue(node):
            if purpose == "rotation":
                return service.renew(node, sources[node].serial, requests[node])
            return service.submit(
                grants[node].token,
                requests[node],
                evidence(requests[node], node_id=node),
            )

        control["pause_node"] = NODE_ID
        with ThreadPoolExecutor(max_workers=2) as pool:
            blocked = pool.submit(issue, NODE_ID)
            assert entered.wait(5)
            with postgres_engine.connect() as connection:
                assert (
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND state = 'idle in transaction'"
                        )
                    )
                    == 0
                )
            try:
                independent = pool.submit(issue, OTHER_NODE_ID).result(timeout=3)
                assert independent.node_id == OTHER_NODE_ID
                assert not blocked.done()
                with sessions() as session:
                    assert session.get(AgentCertificate, independent.serial) is not None
            finally:
                release.set()
            assert blocked.result(5).node_id == NODE_ID
        assert all(count == 1 for count in counts.values())


@pytest.mark.parametrize("purpose", ("enrollment", "rotation"))
def test_postgres_concurrent_exact_binding_and_lost_https_response_adopt_same_leaf(
    postgres_engine, tmp_path, purpose
):
    """Catches duplicate sign effects or generation changes during same-CSR retry."""
    with https_journal(tmp_path) as (provider, control, entered, release, counts, _):
        sessions, service = prepare(postgres_engine, provider)
        source = enroll(service, NODE_ID) if purpose == "rotation" else None
        request = csr(NODE_ID)
        grant = (
            service.create(NODE_ID, "admin", 600) if purpose == "enrollment" else None
        )

        def issue(owner):
            if purpose == "rotation":
                assert source is not None
                return owner.renew(NODE_ID, source.serial, request)
            assert grant is not None
            return owner.submit(grant.token, request, evidence(request))

        control["pause_node"] = NODE_ID
        control["lose_node"] = NODE_ID
        with ThreadPoolExecutor(max_workers=2) as pool:
            blocked = pool.submit(issue, service)
            assert entered.wait(5)
            with postgres_engine.connect() as connection:
                assert (
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND state = 'idle in transaction'"
                        )
                    )
                    == 0
                )
            try:
                replay = pool.submit(issue, service)
                with pytest.raises(
                    (
                        EnrollmentIssuanceUncertain,
                        RenewalInProgress,
                        RenewalIssuanceUncertain,
                    )
                ):
                    replay.result(3)
            finally:
                release.set()
            with pytest.raises((EnrollmentIssuanceUncertain, RenewalIssuanceUncertain)):
                blocked.result(5)
        restarted = EnrollmentService(sessions, provider, clock=lambda: NOW)
        adopted = issue(restarted)
        assert issue(restarted) == adopted
        assert all(count == 1 for count in counts.values())
        with sessions() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AgentCertificate)
                    .where(AgentCertificate.serial == adopted.serial)
                )
                == 1
            )
            if source is not None:
                stored_source = session.get(AgentCertificate, source.serial)
                assert stored_source is not None
                assert stored_source.state == "active"
                assert adopted.generation == source.generation + 1
            else:
                enrollment = session.scalar(select(AgentEnrollment))
                assert enrollment is not None
                assert enrollment.certificate_serial == adopted.serial


_CHILD_SUBMIT = """
import json, sys
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.enrollment import EnrollmentService
from vonk_control.step_ca import StepCertificateAuthority
args = json.load(sys.stdin)
provider = StepCertificateAuthority(**args["provider"])
engine = create_engine(args["database"])
service = EnrollmentService(sessionmaker(engine, expire_on_commit=False), provider,
                            clock=lambda: datetime.fromisoformat(args["now"]))
service.submit(args["token"], args["csr"].encode(), args["evidence"])
"""


def test_postgres_controller_process_death_adopts_the_committed_https_leaf(
    postgres_engine, tmp_path
):
    """Catches restart generating another request after death during external HTTP."""
    with https_journal(tmp_path) as (
        provider,
        control,
        entered,
        release,
        counts,
        committed,
    ):
        sessions, service = prepare(postgres_engine, provider)
        request = csr(NODE_ID)
        grant = service.create(NODE_ID, "admin", 600)
        control["pause_node"] = NODE_ID
        # The child consumes the same public trust and scoped fixture credential.
        # Values travel on stdin, never in process arguments or diagnostic logs.
        payload = {
            "database": postgres_engine.url.render_as_string(hide_password=False),
            "now": NOW.isoformat(),
            "token": grant.token,
            "csr": request.decode(),
            "evidence": evidence(request),
            "provider": {
                "ca_url": provider._ca_url,
                "root_certificate_path": str(tmp_path / "root.pem"),
                "intermediate_certificate_path": str(tmp_path / "intermediate.pem"),
                "provisioner_name": "vonk-forge-agent",
                "provisioner_kid": provider._provisioner_kid,
                "credential_path": str(tmp_path / "provisioner.pem"),
                "provisioner_public_jwk_path": str(tmp_path / "provisioner-public.jwk"),
                "timeout_seconds": 10,
            },
        }
        process = subprocess.Popen(
            [sys.executable, "-c", _CHILD_SUBMIT],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={
                **os.environ,
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")
                + os.pathsep
                + os.environ.get("PYTHONPATH", ""),
            },
        )
        try:
            assert process.stdin is not None
            process.stdin.write(json.dumps(payload).encode())
            process.stdin.close()
            assert entered.wait(5)
            with sessions() as session:
                enrollment = session.scalar(select(AgentEnrollment))
                assert enrollment is not None
                assert enrollment.state == "issuing"
                accepted = CertificateIssuanceBinding.model_validate(
                    enrollment.provider_request
                )
            process.kill()
            process.wait(5)
            assert process.returncode != 0
            release.set()
            assert committed.wait(5)
            restarted = EnrollmentService(sessions, provider, clock=lambda: NOW)
            adopted = restarted.submit(grant.token, request, evidence(request))
            assert adopted.serial == accepted.serial
            assert restarted.submit(grant.token, request, evidence(request)) == adopted
            assert counts == {accepted.request_id: 1}
        finally:
            release.set()
            if process.poll() is None:
                process.kill()
                process.wait(5)
