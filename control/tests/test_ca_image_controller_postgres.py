"""Actual managed CA + PostgreSQL, with independent Controller processes.

The fault is a PostgreSQL division-by-zero during the certificate transaction,
only after the actual CA has returned its committed receipt. No provider method
or HTTP transport is replaced. The CA and Controller both restart before retry.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import traceback
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker
from vonk_control.enrollment.service import EnrollmentService
from vonk_control.enrollment_contract import EnrollmentGrant
from vonk_control.models import (
    AgentCertificate,
    AgentCertificateRotation,
    AgentEnrollment,
    Base,
)
from vonk_control.pki import IssuedCertificate
from vonk_control.step_ca import StepCAError, StepCertificateAuthority

from .test_enrollment import NODE_ID, csr, evidence
from .test_step_ca import STEP_CA_IMAGE


def _provider(settings):
    directory = Path(settings["directory"])
    return StepCertificateAuthority(
        ca_url=settings["url"],
        root_certificate_path=directory / "root_ca.crt",
        intermediate_certificate_path=directory / "intermediate_ca.crt",
        provisioner_name="vonk-forge-agent",
        provisioner_kid=settings["kid"],
        credential_path=directory / "agent-ca-credential",
        provisioner_public_jwk_path=directory / "agent-ca-public.jwk",
        timeout_seconds=3.0,
    )


def _local_ca_dns():
    original = socket.getaddrinfo
    socket.getaddrinfo = lambda host, *args, **kwargs: original(
        "127.0.0.1" if host == "step-ca" else host, *args, **kwargs
    )


@contextmanager
def _managed_ca(directory):
    image = os.environ.get("VONK_JOURNAL_CA_TEST_IMAGE")
    if not image:
        if os.environ.get("CI"):
            pytest.fail("the exact candidate managed CA image is required")
        pytest.skip("the exact candidate managed CA image is required")
    directory.chmod(0o777)
    (directory / "root-password").write_text("fixture-offline-root-password\n")
    password = directory / "intermediate-password"
    password.write_text("fixture-intermediate-password\n")
    pki = """
set -eu
cd /work
step certificate create "Vonk Forge Test Root" root_ca.crt root_ca.key \\
 --profile root-ca --kty OKP --curve Ed25519 --not-after 87600h \\
 --password-file root-password
step certificate create "Vonk Forge Test Intermediate" \\
 intermediate_ca.crt intermediate_ca_key \\
 --profile intermediate-ca --kty OKP --curve Ed25519 --not-after 8760h \\
 --ca root_ca.crt --ca-key root_ca.key --ca-password-file root-password \\
 --password-file intermediate-password
step crypto jwk create agent-ca-public.jwk agent-ca-credential \\
 --kty EC --crv P-256 --no-password --insecure
step crypto jwk thumbprint < agent-ca-public.jwk
"""
    kid = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-v",
            f"{directory}:/work",
            "--entrypoint",
            "sh",
            STEP_CA_IMAGE,
            "-c",
            pki,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.strip()
    for name in ("agent-ca-public.jwk", "agent-ca-credential"):
        path = directory / name
        jwk = json.loads(path.read_text())
        jwk.update(kid=kid, alg="ES256", use="sig")
        path.write_text(json.dumps(jwk))
    config = json.loads(
        (
            Path(__file__).resolve().parents[2] / "deploy/compose/step-ca/ca.json"
        ).read_text()
    )
    config["authority"]["provisioners"][0]["key"] = json.loads(
        (directory / "agent-ca-public.jwk").read_text()
    )
    (directory / "ca.json").write_text(json.dumps(config))
    (directory / "db").mkdir(mode=0o777)
    (directory / "db").chmod(0o777)
    container = f"vonk-ca-pg-recovery-{uuid.uuid4().hex}"
    mounts = {
        "ca.json": "/home/step/config/ca.json",
        "root_ca.crt": "/run/vonk-normalized-secrets/step-ca/root-certificate",
        "intermediate_ca.crt": "/run/vonk-normalized-secrets/step-ca/intermediate-certificate",
        "intermediate_ca_key": "/run/vonk-normalized-secrets/step-ca/intermediate-key",
        "intermediate-password": "/run/vonk-normalized-secrets/step-ca/password",
    }
    args = ["docker", "run", "-d", "--name", container, "-p", "127.0.0.1::9000"]
    for source, destination in mounts.items():
        (directory / source).chmod(0o444)
        args.extend(["-v", f"{directory / source}:{destination}:ro"])
    args.extend(
        [
            "-v",
            f"{directory / 'db'}:/home/step/db",
            "--entrypoint",
            "vonk-step-ca",
            image,
            "--config",
            mounts["ca.json"],
            "--password-file",
            mounts["intermediate-password"],
        ]
    )
    subprocess.run(args, capture_output=True, check=True, timeout=30)
    original_dns = socket.getaddrinfo
    _local_ca_dns()
    try:

        def healthy_settings():
            port = (
                subprocess.run(
                    ["docker", "port", container, "9000/tcp"],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=10,
                )
                .stdout.strip()
                .rsplit(":", 1)[1]
            )
            settings = {
                "directory": str(directory),
                "kid": kid,
                "url": f"https://step-ca:{port}",
            }
            provider = _provider(settings)
            try:
                deadline = time.monotonic() + 30
                while True:
                    try:
                        if provider.check_health() is None:
                            return settings
                    except StepCAError:
                        pass
                    if time.monotonic() >= deadline:
                        pytest.fail("actual managed CA did not become healthy")
                    time.sleep(0.1)
            finally:
                provider._client.close()

        def restart():
            subprocess.run(
                ["docker", "restart", container],
                capture_output=True,
                check=True,
                timeout=30,
            )
            return healthy_settings()

        def stop():
            subprocess.run(
                ["docker", "stop", "--time", "1", container],
                capture_output=True,
                check=True,
                timeout=10,
            )

        yield healthy_settings(), restart, stop
    finally:
        socket.getaddrinfo = original_dns
        subprocess.run(
            ["docker", "rm", "--force", container],
            capture_output=True,
            check=False,
            timeout=30,
        )


def _controller_process(payload):
    """All credentials enter on stdin, never argv or diagnostic output."""
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.test_ca_image_controller_postgres",
            "--controller-child",
        ],
        cwd=Path(__file__).resolve().parents[1],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )
    assert completed.returncode == 0, (
        "independent Controller child failed (private diagnostics withheld)"
    )
    result = json.loads(completed.stdout)
    assert "child_error" not in result, result
    return result


def _run_child(payload):
    _local_ca_dns()
    engine = create_engine(payload["database_url"], pool_pre_ping=True)
    sessions = sessionmaker(engine, expire_on_commit=False)
    provider = _provider(payload["ca"])
    service = EnrollmentService(sessions, provider, clock=lambda: datetime.now(UTC))
    exchanges = []

    def received(response):
        if response.request.url.path != "/1.0/vonk/sign":
            return
        body = json.loads(response.request.content)
        # Decode for observational correlation only; the real CA verifies JWTs.
        claims = jwt.decode(body["ott"], options={"verify_signature": False})
        exchanges.append(
            {
                "mode": body["mode"],
                "binding": body["request"],
                "jti": claims["jti"],
                "csr_sha256": hashlib.sha256(
                    x509.load_pem_x509_csr(body["csr"].encode()).public_bytes(
                        serialization.Encoding.DER
                    )
                ).hexdigest(),
                "status": response.status_code,
            }
        )

    provider._client.event_hooks["response"].append(received)
    attempted = {}

    def abort_certificate_transaction(session, _flush_context, _instances):
        if not payload["fail_sql"] or attempted:
            return
        certificate = next(
            (row for row in session.new if isinstance(row, AgentCertificate)), None
        )
        if certificate is None:
            return  # The durable accepted claim must commit successfully first.
        committed = [entry for entry in exchanges if entry["status"] == 201]
        assert committed and committed[-1]["mode"] == "issue"
        assert committed[-1]["binding"]["serial"] == certificate.serial
        assert committed[-1]["binding"]["generation"] == certificate.generation
        attempted.update(
            certificate_pem=certificate.certificate_pem,
            serial=certificate.serial,
            binding=committed[-1]["binding"],
        )
        # Kill the Controller after the real SQL failure, before its bounded
        # retry can adopt the CA result. Process disconnect rolls back SQL;
        # the provider journal and accepted claim survive independently.
        try:
            session.execute(text("SELECT 1 / 0"))
        except DBAPIError as error:
            attempted["sqlstate"] = getattr(error.orig, "sqlstate", None)
            print(
                json.dumps(
                    {
                        "uncertain": True,
                        **attempted,
                        "pid": os.getpid(),
                        "exchanges": exchanges,
                    }
                ),
                flush=True,
            )
            os._exit(0)

    event.listen(sessions, "before_flush", abort_certificate_transaction)
    try:
        request = payload["csr"].encode()
        if payload["purpose"] == "rotation":
            issued = service.renew(NODE_ID, payload["source_serial"], request)
        else:
            issued = service.submit(payload["token"], request, evidence(request))
        assert not payload["fail_sql"]
        assert isinstance(issued, IssuedCertificate)
        result = {
            "uncertain": False,
            "certificate_pem": issued.certificate_pem.decode(),
            "serial": issued.serial,
            "generation": issued.generation,
        }
        return {**result, "pid": os.getpid(), "exchanges": exchanges}
    finally:
        event.remove(sessions, "before_flush", abort_certificate_transaction)
        provider._client.close()
        engine.dispose()


@pytest.mark.parametrize("purpose", ("enrollment", "rotation"))
def test_actual_ca_postgres_commit_failure_dual_restart_adopts_exact_der(
    postgres_engine,
    tmp_path,
    purpose,
):
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with _managed_ca(tmp_path) as (settings, restart_ca, _stop_ca):
        provider = _provider(settings)
        service = EnrollmentService(sessions, provider, clock=lambda: datetime.now(UTC))
        source = None
        if purpose == "rotation":
            source_csr = csr()
            source_grant = service.create(NODE_ID, "admin", 600)
            assert isinstance(source_grant, EnrollmentGrant)
            source = service.submit(
                source_grant.token, source_csr, evidence(source_csr)
            )
            assert isinstance(source, IssuedCertificate)
        request = csr()
        grant = (
            service.create(NODE_ID, "admin", 600) if purpose == "enrollment" else None
        )
        assert grant is None or isinstance(grant, EnrollmentGrant)
        payload = {
            "database_url": postgres_engine.url.render_as_string(hide_password=False),
            "ca": settings,
            "purpose": purpose,
            "csr": request.decode(),
            "token": grant.token if grant else None,
            "source_serial": source.serial if source else None,
            "fail_sql": True,
        }
        failed = _controller_process(payload)
        assert failed["uncertain"] and failed["sqlstate"] == "22012"
        binding = failed["binding"]
        with sessions() as session:
            assert session.get(AgentCertificate, failed["serial"]) is None
            if purpose == "enrollment":
                claim = session.scalar(select(AgentEnrollment))
            else:
                claim = session.get(AgentCertificateRotation, NODE_ID)
                assert source is not None
                old = session.get(AgentCertificate, source.serial)
                assert old is not None
                assert old.state == "active" and old.revoked_at is None
                assert old.ca_revoked_at is None
            assert claim is not None
            assert claim.state == "issuing"
            assert claim.provider_request == binding
            assert claim.csr_pem == request.decode()
        if source:
            bundle = provider.revocation_bundle(datetime.now(UTC))
            assert isinstance(bundle, bytes)
            crl = x509.load_pem_x509_crl(bundle)
            assert (
                crl.get_revoked_certificate_by_serial_number(int(source.serial)) is None
            )
        # Both processes restart, retaining the same CA Badger files and PG DB.
        provider._client.close()
        payload.update(ca=restart_ca(), fail_sql=False)
        recovered = _controller_process(payload)
        assert recovered["pid"] != failed["pid"]
        assert recovered["serial"] == failed["serial"]
        assert recovered["certificate_pem"] == failed["certificate_pem"]
        certificate = x509.load_pem_x509_certificate(
            recovered["certificate_pem"].encode()
        )
        leaf_der = certificate.public_bytes(serialization.Encoding.DER)
        assert leaf_der == x509.load_pem_x509_certificate(
            failed["certificate_pem"].encode()
        ).public_bytes(serialization.Encoding.DER)
        issuer = x509.load_pem_x509_certificate(
            (tmp_path / "intermediate_ca.crt").read_bytes()
        )
        issuer_public_key = issuer.public_key()
        assert isinstance(issuer_public_key, ed25519.Ed25519PublicKey)
        issuer_public_key.verify(
            certificate.signature, certificate.tbs_certificate_bytes
        )
        assert certificate.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        ) == x509.load_pem_x509_csr(request).public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        all_exchanges = failed["exchanges"] + recovered["exchanges"]
        assert sum(entry["mode"] == "issue" for entry in all_exchanges) == 1
        assert recovered["exchanges"] and all(
            entry["mode"] == "observe" and entry["status"] == 201
            for entry in recovered["exchanges"]
        )
        assert all(entry["binding"] == binding for entry in all_exchanges)
        assert all(
            entry["csr_sha256"] == binding["csr_sha256"] for entry in all_exchanges
        )
        jtis = [entry["jti"] for entry in all_exchanges]
        assert len(jtis) == len(set(jtis)) and binding["request_id"] not in jtis
        with sessions() as session:
            stored = session.get(AgentCertificate, recovered["serial"])
            assert stored is not None
            assert stored.certificate_pem == recovered["certificate_pem"]
            assert (
                stored.chain_pem
                == issuer.public_bytes(serialization.Encoding.PEM).decode()
            )
            assert stored.generation == binding["generation"]
            if source:
                old = session.get(AgentCertificate, source.serial)
                assert old is not None
                assert old.state == "active" and old.revoked_at is None
                assert old.ca_revoked_at is None
                assert stored.state == "staged"
        provider = _provider(payload["ca"])
        try:
            if source:
                bundle = provider.revocation_bundle(datetime.now(UTC))
                assert isinstance(bundle, bytes)
                crl = x509.load_pem_x509_crl(bundle)
                assert (
                    crl.get_revoked_certificate_by_serial_number(int(source.serial))
                    is None
                )
                EnrollmentService(
                    sessions, provider, clock=lambda: datetime.now(UTC)
                ).activate(NODE_ID, recovered["serial"], recovered["generation"])
                with sessions() as session:
                    activated = session.get(AgentCertificate, recovered["serial"])
                    retired = session.get(AgentCertificate, source.serial)
                    assert activated is not None and retired is not None
                    assert activated.state == "active"
                    assert retired.state == "revoked"
        finally:
            provider._client.close()


if __name__ == "__main__" and sys.argv[1:] == ["--controller-child"]:
    try:
        print(json.dumps(_run_child(json.load(sys.stdin))))
    except Exception as error:  # noqa: BLE001 - safe subprocess error projection
        # Expose code locations and class, never stdin credentials or JWTs.
        print(
            json.dumps(
                {
                    "child_error": type(error).__name__,
                    "frames": [
                        f"{frame.name}:{frame.lineno}"
                        for frame in traceback.extract_tb(error.__traceback__)
                    ],
                }
            )
        )
