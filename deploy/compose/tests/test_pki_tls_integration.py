from __future__ import annotations

import os
import shutil
import socket
import ssl
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ROOT = Path(__file__).resolve().parents[3]
CADDY_IMAGE = (
    "caddy:2.11.4@sha256:"
    "844f60b64e4724a5aa8245e019dace0d3f199f7433ce6c57676cb30a920dbad9"
)
HOSTNAMES = (
    "enroll.test.example",
    "agents.test.example",
    "registry.test.example",
)


def _docker_failure_or_skip(message: str) -> None:
    if os.environ.get("CI"):
        pytest.fail(message)
    pytest.skip(message)


def _require_docker() -> str:
    docker = shutil.which("docker")
    if docker is None:
        _docker_failure_or_skip("Docker CLI is required for PKI/TLS integration")
    result = subprocess.run(
        [docker, "info"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        _docker_failure_or_skip("Docker daemon is required for PKI/TLS integration")
    return docker


def _key_bytes(key: ed25519.Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _write_pki(directory: Path) -> dict[str, Path | x509.Certificate]:
    now = datetime.now(UTC).replace(microsecond=0)
    root_key = ed25519.Ed25519PrivateKey.generate()
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Vonk Test Root")])
    root = (
        x509.CertificateBuilder()
        .subject_name(root_name)
        .issuer_name(root_name)
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=7))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(root_key, algorithm=None)
    )

    server_key = ed25519.Ed25519PrivateKey.generate()
    server = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOSTNAMES[0])]))
        .issuer_name(root.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name) for name in HOSTNAMES]),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(root_key, algorithm=None)
    )

    client_key = ed25519.Ed25519PrivateKey.generate()
    node_id = "spk_" + "a" * 32
    client = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id)]))
        .issuer_name(root.subject)
        .public_key(client_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .sign(root_key, algorithm=None)
    )

    values = {
        "root": root.public_bytes(serialization.Encoding.PEM),
        "server-certificate": server.public_bytes(serialization.Encoding.PEM),
        "server-key": _key_bytes(server_key),
        "client-certificate": client.public_bytes(serialization.Encoding.PEM),
        "client-key": _key_bytes(client_key),
        "proxy-auth": b"A" * 32 + b"\n",
    }
    paths: dict[str, Path | x509.Certificate] = {"server": server}
    for name, value in values.items():
        path = directory / name
        path.write_bytes(value)
        paths[name] = path
    return paths


def _published_port(docker: str, container: str, target: int) -> int:
    output = subprocess.run(
        [docker, "port", container, f"{target}/tcp"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()
    return int(output.rsplit(":", 1)[1])


def _tls_request(
    port: int,
    hostname: str,
    root: Path,
    path: str,
    *,
    client_certificate: Path | None = None,
    client_key: Path | None = None,
) -> tuple[bytes, bytes]:
    context = ssl.create_default_context(cafile=str(root))
    if client_certificate is not None and client_key is not None:
        context.load_cert_chain(client_certificate, client_key)
    with (
        socket.create_connection(("127.0.0.1", port), timeout=5) as connection,
        context.wrap_socket(connection, server_hostname=hostname) as tls,
    ):
        peer = tls.getpeercert(binary_form=True)
        tls.sendall(
            f"GET {path} HTTP/1.1\r\nHost: {hostname}\r\nConnection: close\r\n\r\n".encode()
        )
        response = bytearray()
        while block := tls.recv(65536):
            response.extend(block)
    return peer, bytes(response)


def test_caddy_serves_one_generated_controller_identity_on_each_pki_sni(
    tmp_path: Path,
) -> None:
    docker = _require_docker()
    material = _write_pki(tmp_path)
    container = "vonk-caddy-pki-" + uuid.uuid4().hex
    command = [
        docker,
        "run",
        "--detach",
        "--name",
        container,
        "--read-only",
        "--publish",
        "127.0.0.1::8080",
        "--publish",
        "127.0.0.1::8443",
        "--tmpfs",
        "/tmp",
        "--env",
        "VONK_CONTROL_HOSTNAME=control.test.example",
        "--env",
        f"VONK_AGENT_ENROLL_HOSTNAME={HOSTNAMES[0]}",
        "--env",
        f"VONK_AGENT_HOSTNAME={HOSTNAMES[1]}",
        "--env",
        f"VONK_REGISTRY_HOSTNAME={HOSTNAMES[2]}",
        "--env",
        "VONK_BACKEND_PORT=8443",
        "--volume",
        f"{ROOT / 'deploy/compose/Caddyfile'}:/etc/caddy/Caddyfile:ro",
        "--volume",
        f"{ROOT / 'deploy/compose/caddy/entrypoint.sh'}:/usr/local/bin/vonk-caddy-entrypoint:ro",
        "--volume",
        f"{material['server-certificate']}:/run/secrets/controller-server-certificate:ro",
        "--volume",
        f"{material['server-key']}:/run/secrets/controller-server-key:ro",
        "--volume",
        f"{material['root']}:/run/secrets/agent-client-ca:ro",
        "--volume",
        f"{material['proxy-auth']}:/run/secrets/agent-proxy-auth:ro",
        "--entrypoint",
        "/bin/sh",
        CADDY_IMAGE,
        "/usr/local/bin/vonk-caddy-entrypoint",
    ]
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
    try:
        for _ in range(60):
            health = subprocess.run(
                [
                    docker,
                    "exec",
                    container,
                    "wget",
                    "-q",
                    "-O",
                    "/dev/null",
                    "http://127.0.0.1:8082/healthz",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if health.returncode == 0:
                break
            time.sleep(0.25)
        else:
            logs = subprocess.run(
                [docker, "logs", container],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            pytest.fail(f"Caddy did not become ready:\n{logs.stderr}")

        tls_port = _published_port(docker, container, 8443)
        server = material["server"]
        assert isinstance(server, x509.Certificate)
        expected_der = server.public_bytes(serialization.Encoding.DER)

        peer, enrollment = _tls_request(
            tls_port, HOSTNAMES[0], material["root"], "/agent/v1/enroll"
        )
        assert peer == expected_der
        assert enrollment.startswith(b"HTTP/1.1 502")

        with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
            _tls_request(tls_port, HOSTNAMES[1], material["root"], "/agent/v1/claim")

        for hostname, path in (
            (HOSTNAMES[1], "/agent/v1/claim"),
            (HOSTNAMES[2], "/v2/"),
        ):
            peer, response = _tls_request(
                tls_port,
                hostname,
                material["root"],
                path,
                client_certificate=material["client-certificate"],
                client_key=material["client-key"],
            )
            assert peer == expected_der
            assert response.startswith(b"HTTP/1.1 502")

        browser_port = _published_port(docker, container, 8080)
        with socket.create_connection(("127.0.0.1", browser_port), timeout=5) as browser:
            browser.sendall(
                b"GET / HTTP/1.1\r\nHost: control.test.example\r\nConnection: close\r\n\r\n"
            )
            assert browser.recv(4096).startswith(b"HTTP/1.1 502")
    finally:
        subprocess.run(
            [docker, "rm", "--force", container],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
