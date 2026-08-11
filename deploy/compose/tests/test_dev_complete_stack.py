from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from vonk_control.development_tokens import issue_development_admin_token

ROOT = Path(__file__).resolve().parents[3]
COMPOSE = ROOT / "scripts/dev-compose"
API_IMAGE = "vonk-forge-api:dev-local"
WORKER_IMAGE = "vonk-forge-worker:dev-local"
ENROLL_HOST = "enroll.vonk-forge.lan"
AGENT_HOST = "agents.vonk-forge.lan"
NODE_ID = "spk_" + "7" * 32


def _run(
    arguments: list[str],
    *,
    environment: dict[str, str] | None = None,
    check: bool = True,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments,
        cwd=ROOT,
        env=environment,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _require_local_images() -> None:
    if _run(["docker", "info"], check=False, timeout=15).returncode != 0:
        pytest.skip("a reachable Docker daemon is required")
    for image in (API_IMAGE, WORKER_IMAGE):
        if _run(["docker", "image", "inspect", image], check=False).returncode != 0:
            pytest.skip(f"the prebuilt {image} acceptance image is required")


def _unused_ports() -> tuple[int, int]:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as api_listener,
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as agent_listener,
    ):
        api_listener.bind(("127.0.0.1", 0))
        agent_listener.bind(("0.0.0.0", 0))
        return (
            int(api_listener.getsockname()[1]),
            int(agent_listener.getsockname()[1]),
        )


def _write_client_identity(secrets: Path, destination: Path) -> tuple[Path, Path]:
    ca_certificate = x509.load_pem_x509_certificate(
        (secrets / "agent-ca-certificate").read_bytes()
    )
    ca_key = serialization.load_pem_private_key(
        (secrets / "agent-ca-key").read_bytes(), password=None
    )
    assert isinstance(ca_key, Ed25519PrivateKey)
    key = Ed25519PrivateKey.generate()
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, NODE_ID)]))
        .issuer_name(ca_certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=True,
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(
                        f"spiffe://vonk-forge.local/node/{NODE_ID}"
                    )
                ]
            ),
            critical=False,
        )
        .sign(ca_key, algorithm=None)
    )
    key_path = destination / "agent-client-key.pem"
    certificate_path = destination / "agent-client-certificate.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.chmod(0o600)
    certificate_path.chmod(0o600)
    return certificate_path, key_path


def _enrollment_body(destination: Path, grant_token: str) -> tuple[Path, Path]:
    key = Ed25519PrivateKey.generate()
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, NODE_ID)]))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(
                        f"spiffe://vonk-forge.local/node/{NODE_ID}"
                    )
                ]
            ),
            critical=False,
        )
        .sign(key, algorithm=None)
    )
    public = csr.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    body = {
        "csr": csr.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        "evidence": {
            "agent_digest": "a" * 64,
            "boot_id": "complete-stack-smoke",
            "csr_public_key_fingerprint": hashlib.sha256(public).hexdigest(),
            "hardware_fingerprint": "complete-stack-hardware",
            "host_key_fingerprint": "complete-stack-host-key",
            "node_id": NODE_ID,
        },
        "grant_token": grant_token,
    }
    path = destination / "enrollment.json"
    key_path = destination / "enrollment-key.pem"
    path.write_text(json.dumps(body), encoding="utf-8")
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    key_path.chmod(0o600)
    return path, key_path


def _api_json(
    *,
    port: int,
    path: str,
    token: str,
    payload: object | None = None,
    expected: int,
) -> dict[str, object]:
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == expected
        value = json.loads(response.read())
    assert isinstance(value, dict)
    return value


def _curl_status(
    *,
    hostname: str,
    port: int,
    ca: Path,
    path: str,
    body: Path | None = None,
    certificate: Path | None = None,
    key: Path | None = None,
    headers: tuple[str, ...] = (),
    output: Path | None = None,
) -> tuple[int, str, str]:
    arguments = [
        "curl",
        "--noproxy",
        "*",
        "--silent",
        "--show-error",
        "--output",
        str(output) if output is not None else "/dev/null",
        "--write-out",
        "%{http_code}",
        "--cacert",
        str(ca),
        "--resolve",
        f"{hostname}:{port}:127.0.0.1",
        "--request",
        "POST",
    ]
    if body is not None:
        arguments.extend(
            ["--header", "Content-Type: application/json", "--data-binary", f"@{body}"]
        )
    else:
        arguments.extend(["--data", "{}"])
    if certificate is not None and key is not None:
        arguments.extend(["--cert", str(certificate), "--key", str(key)])
    for header in headers:
        arguments.extend(["--header", header])
    arguments.append(f"https://{hostname}:{port}{path}")
    result = _run(arguments, check=False, timeout=20)
    return result.returncode, result.stdout, result.stderr


def _assert_direct_spoof_is_rejected(api_port: int) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{api_port}/agent/v1/claim",
        data=b"{}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Vonk-Agent-Fingerprint": "f" * 64,
            "X-Vonk-Agent-Node": "spk_" + "9" * 32,
            "X-Vonk-Agent-Proxy-Auth": "untrusted-proxy",
            "X-Vonk-Agent-Serial": "123",
            "X-Vonk-Agent-Source": "127.0.0.1",
            "X-Vonk-Agent-Verified": "1",
        },
    )
    with pytest.raises(urllib.error.HTTPError) as denied:
        urllib.request.urlopen(request, timeout=5)
    assert denied.value.code == 401


def _publish_activation(api_container: str) -> str:
    code = """
from datetime import UTC, datetime
from pathlib import Path
from vonk_control.presence import ManagementAddressPolicy
from vonk_control.route_runtime import AtomicRouteBundlePublisher

publisher = AtomicRouteBundlePublisher(
    Path('/routes'),
    management_policy=ManagementAddressPolicy.parse(
        Path('/run/secrets/management-cidrs').read_text()
    ),
    clock=lambda: datetime.now(UTC),
)
marker = publisher.withdraw(
    reconciliation_id='17e11f67-7e5a-4bd3-b6af-45c86f8736c8',
    plan_digest='d' * 64,
    targets=('spk_' + '7' * 32,),
    reason='complete stack acceptance',
)
print(marker.digest)
"""
    result = _run(["docker", "exec", api_container, "python", "-c", code])
    digest = result.stdout.strip()
    assert len(digest) == 64
    return digest


def _wait_for_exact_ack(litellm_container: str, marker_digest: str) -> None:
    code = f"""
import hashlib
import json
from pathlib import Path

activation_bytes = Path('/routes/activation.json').read_bytes()
activation = json.loads(activation_bytes)
ack = json.loads(Path('/supervisor/ack.json').read_bytes())
assert hashlib.sha256(activation_bytes).hexdigest() == ack['activation_sha256']
assert hashlib.sha256(activation_bytes).hexdigest() == {marker_digest!r}
assert ack['generation'] == activation['generation']
assert ack['litellm_sha256'] == activation['litellm_sha256']
assert ack['state'] == activation['state'] == 'maintenance'
print('exact')
"""
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        result = _run(
            ["docker", "exec", litellm_container, "python", "-c", code],
            check=False,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip() == "exact":
            return
        time.sleep(1)
    pytest.fail("LiteLLM did not acknowledge the exact route activation marker")


def _assert_litellm_health(container: str) -> None:
    code = (
        "import urllib.request; "
        "response=urllib.request.urlopen("
        "'http://127.0.0.1:4000/health/liveliness', timeout=3); "
        "assert response.status == 200"
    )
    _run(["docker", "exec", container, "python", "-c", code], timeout=15)


def test_complete_development_stack_enforces_tls_identity_and_acks_routes(
    tmp_path: Path,
) -> None:
    _require_local_images()
    project = f"vonk-stack-{uuid.uuid4().hex[:12]}"
    api_port, agent_port = _unused_ports()
    secrets = tmp_path / "secrets"
    environment = {
        **os.environ,
        "VONK_AGENT_PORT": str(agent_port),
        "VONK_DEV_PORT": str(api_port),
        "VONK_DEV_MANAGEMENT_CIDRS": (
            "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
        ),
        "VONK_DEV_PROJECT_NAME": project,
        "VONK_DEV_SECRETS_DIR": str(secrets),
    }
    api_container = f"{project}-control-api-1"
    litellm_container = f"{project}-litellm-1"

    try:
        _run([str(COMPOSE), "up", "-d"], environment=environment, timeout=240)
        certificate, key = _write_client_identity(secrets, tmp_path)
        controller_ca = secrets / "controller-ca"

        admin_token = issue_development_admin_token(
            signing_key=(secrets / "token-signing-key").read_bytes().strip(),
            ttl_seconds=600,
            now=int(time.time()),
        )
        grant = _api_json(
            port=api_port,
            path="/api/v1/agents/enrollments/grants",
            token=admin_token,
            payload={"node_id": NODE_ID, "ttl_seconds": 300},
            expected=201,
        )
        assert isinstance(grant.get("token"), str)
        enrollment, enrollment_key = _enrollment_body(tmp_path, grant["token"])
        enrollment_response = tmp_path / "enrollment-response.json"

        enroll_exit, enroll_status, _enroll_error = _curl_status(
            hostname=ENROLL_HOST,
            port=agent_port,
            ca=controller_ca,
            path="/agent/v1/enroll",
            body=enrollment,
            output=enrollment_response,
        )
        assert enroll_exit == 0
        assert int(enroll_status) == 202, enrollment_response.read_text()
        pending = json.loads(enrollment_response.read_text())
        assert pending["node_id"] == NODE_ID
        _api_json(
            port=api_port,
            path=f"/api/v1/agents/enrollments/{pending['id']}/approve",
            token=admin_token,
            expected=200,
        )
        pickup_response = tmp_path / "pickup-response.json"
        pickup_exit, pickup_status, _pickup_error = _curl_status(
            hostname=ENROLL_HOST,
            port=agent_port,
            ca=controller_ca,
            path="/agent/v1/enroll",
            body=enrollment,
            output=pickup_response,
        )
        assert pickup_exit == 0
        assert int(pickup_status) == 200
        pickup = json.loads(pickup_response.read_text())
        approved_certificate = tmp_path / "approved-agent-certificate.pem"
        approved_certificate.write_text(pickup["certificate_pem"], encoding="ascii")
        approved_certificate.chmod(0o600)
        claim_body = tmp_path / "rust-claim.json"
        claim_body.write_text(
            json.dumps(
                {
                    "agent_implementation": "rust",
                    "capabilities": [
                        "agent.runtime.rust.v1",
                        "recipe.build.v1",
                        "recipe.image.import.v1",
                        "recipe.install",
                        "recipe.start",
                        "recipe.stop",
                        "recipe.uninstall",
                    ],
                    "lease_seconds": 60,
                    "node_id": NODE_ID,
                    "protocol_version": 3,
                    "wait_seconds": 0,
                }
            ),
            encoding="utf-8",
        )
        claim_body.chmod(0o600)

        approved_claim_exit, approved_claim_status, _approved_claim_error = (
            _curl_status(
                hostname=AGENT_HOST,
                port=agent_port,
                ca=controller_ca,
                path="/agent/v1/claim",
                body=claim_body,
                certificate=approved_certificate,
                key=enrollment_key,
            )
        )
        assert approved_claim_exit == 0
        assert approved_claim_status == "204"

        claim_exit, claim_status, _claim_error = _curl_status(
            hostname=AGENT_HOST,
            port=agent_port,
            ca=controller_ca,
            path="/agent/v1/claim",
        )
        assert claim_exit != 0
        assert claim_status == "000"

        authenticated_exit, authenticated_status, _authenticated_error = _curl_status(
            hostname=AGENT_HOST,
            port=agent_port,
            ca=controller_ca,
            path="/agent/v1/claim",
            certificate=certificate,
            key=key,
            headers=(
                "X-Vonk-Agent-Node: spk_" + "9" * 32,
                "X-Vonk-Agent-Verified: 1",
            ),
        )
        assert authenticated_exit == 0
        assert authenticated_status in {"401", "403"}
        _assert_direct_spoof_is_rejected(api_port)

        _assert_litellm_health(litellm_container)
        marker_digest = _publish_activation(api_container)
        _wait_for_exact_ack(litellm_container, marker_digest)
        _assert_litellm_health(litellm_container)
    finally:
        _run(
            [str(COMPOSE), "down", "--volumes", "--remove-orphans"],
            environment=environment,
            check=False,
            timeout=180,
        )
        remaining = _run(
            [
                "docker",
                "volume",
                "ls",
                "--quiet",
                "--filter",
                f"label=com.docker.compose.project={project}",
            ],
            check=False,
        )
        assert remaining.stdout.strip() == ""
