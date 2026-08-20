from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, ExtensionOID, NameOID
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.agent_api import AgentApiServices
from vonk_control.api import build_agent_services
from vonk_control.models import Base
from vonk_control.presence import AgentPresenceService

# Keep this import first so the TDD RED proves the provider is absent before
# any new runtime dependency is imported.
from vonk_control.step_ca import (
    StepCAError,
    StepCertificateAuthority,
    _validate_crl_freshness,
)

NODE_ID = "spk_0123456789abcdef0123456789abcdef"
NOW = datetime(2026, 8, 4, 12, tzinfo=UTC)
CA_URL = "https://step-ca:9000"
STEP_CA_IMAGE = "smallstep/step-ca:0.30.2@sha256:a2b17872915c193259b75a5474c398326f41bd199f0842093e52cf4182bc8270"


def _b64(value: int) -> str:
    return base64.urlsafe_b64encode(value.to_bytes(32, "big")).rstrip(b"=").decode()


def _write_material(tmp_path: Path) -> dict[str, object]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    root_key = ed25519.Ed25519PrivateKey.generate()
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Vonk Forge Offline Root")])
    root = (
        x509.CertificateBuilder().subject_name(root_name).issuer_name(root_name)
        .public_key(root_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1)).not_valid_after(NOW + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
        .sign(root_key, algorithm=None)
    )
    intermediate_key = ed25519.Ed25519PrivateKey.generate()
    intermediate_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Vonk Forge Agent Intermediate")])
    intermediate = (
        x509.CertificateBuilder().subject_name(intermediate_name).issuer_name(root.subject)
        .public_key(intermediate_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1)).not_valid_after(NOW + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
        .sign(root_key, algorithm=None)
    )
    provisioner_key = ec.generate_private_key(ec.SECP256R1())
    root_path = tmp_path / "root.pem"
    intermediate_path = tmp_path / "intermediate.pem"
    credential_path = tmp_path / "provisioner.pem"
    public_jwk_path = tmp_path / "provisioner-public.jwk"
    root_path.write_bytes(root.public_bytes(serialization.Encoding.PEM))
    intermediate_path.write_bytes(intermediate.public_bytes(serialization.Encoding.PEM))
    numbers = provisioner_key.public_key().public_numbers()
    public_jwk = {"kty": "EC", "crv": "P-256", "use": "sig", "alg": "ES256", "x": _b64(numbers.x), "y": _b64(numbers.y)}
    thumbprint_input = json.dumps({name: public_jwk[name] for name in ("crv", "kty", "x", "y")}, sort_keys=True, separators=(",", ":")).encode()
    import hashlib
    kid = base64.urlsafe_b64encode(hashlib.sha256(thumbprint_input).digest()).rstrip(b"=").decode()
    public_jwk["kid"] = kid
    private_jwk = public_jwk | {"d": _b64(provisioner_key.private_numbers().private_value)}
    credential_path.write_text(json.dumps(private_jwk))
    public_jwk_path.write_text(json.dumps(public_jwk))
    credential_path.chmod(0o600)
    return {
        "root": root, "root_path": root_path,
        "intermediate": intermediate, "intermediate_key": intermediate_key,
        "intermediate_path": intermediate_path, "credential_path": credential_path,
        "public_jwk_path": public_jwk_path,
        "public_jwk": public_jwk, "kid": kid,
    }


def _csr(node_id: str = NODE_ID) -> bytes:
    key = ed25519.Ed25519PrivateKey.generate()
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id)]))
        .add_extension(x509.SubjectAlternativeName([
            x509.UniformResourceIdentifier(f"spiffe://vonk-forge.local/node/{node_id}")
        ]), critical=False)
        .sign(key, algorithm=None)
        .public_bytes(serialization.Encoding.PEM)
    )


def _leaf(
    csr_pem: bytes,
    material: dict[str, object],
    *,
    now: datetime = NOW,
    serial: int = 1234,
    lifetime: timedelta = timedelta(hours=24),
) -> x509.Certificate:
    request = x509.load_pem_x509_csr(csr_pem)
    return (
        x509.CertificateBuilder().subject_name(request.subject)
        .issuer_name(material["intermediate"].subject).public_key(request.public_key())
        .serial_number(serial).not_valid_before(now).not_valid_after(now + lifetime)
        .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .add_extension(request.extensions.get_extension_for_class(x509.SubjectAlternativeName).value, critical=False)
        .sign(material["intermediate_key"], algorithm=None)
    )


def _provider(tmp_path: Path, handler, *, max_response_bytes: int = 64 * 1024) -> tuple[StepCertificateAuthority, dict[str, object]]:
    material = _write_material(tmp_path)
    provider = StepCertificateAuthority(
        ca_url=CA_URL,
        root_certificate_path=material["root_path"],
        intermediate_certificate_path=material["intermediate_path"],
        provisioner_name="vonk-forge-agent",
        provisioner_kid=material["kid"],
        credential_path=material["credential_path"],
        provisioner_public_jwk_path=material["public_jwk_path"],
        timeout_seconds=2.0,
        max_response_bytes=max_response_bytes,
        transport=httpx.MockTransport(handler),
    )
    return provider, material


def _builder_settings(tmp_path: Path, *, direct_fabric_cidrs: str) -> SimpleNamespace:
    material = _write_material(tmp_path)
    return SimpleNamespace(
        agent_runtime="enabled",
        agent_controller_origin="https://agents.example.test:8443",
        agent_enrollment_origin="https://enroll.example.test:8443",
        controller_ca_path=material["root_path"],
        agent_intermediate_certificate_path=material["intermediate_path"],
        agent_ca_root_path=material["root_path"],
        agent_ca_credential_path=material["credential_path"], agent_ca_url=CA_URL,
        agent_ca_provisioner_public_jwk_path=material["public_jwk_path"],
        agent_ca_provisioner_name="vonk-forge-agent", agent_ca_provisioner_kid=material["kid"],
        agent_ca_timeout_seconds=2.0, agent_ca_max_response_bytes=4096,
        agent_artifact_root=tmp_path / "artifacts",
        management_cidrs="10.0.0.0/24", direct_fabric_cidrs=direct_fabric_cidrs,
    )


def _success_response(request: httpx.Request, material: dict[str, object], seen: list[dict[str, object]], *, serial: int = 1234) -> httpx.Response:
    body = json.loads(request.content)
    seen.append({"request": request, "body": body})
    leaf = _leaf(body["csr"].encode(), material, serial=serial)
    leaf_pem = leaf.public_bytes(serialization.Encoding.PEM).decode()
    intermediate_pem = material["intermediate"].public_bytes(serialization.Encoding.PEM).decode()
    return httpx.Response(201, json={"crt": leaf_pem, "ca": intermediate_pem, "certChain": [leaf_pem, intermediate_pem]})


def test_sign_uses_fixed_policy_short_lived_one_use_authorization_and_node_signed_csr(tmp_path: Path) -> None:
    seen: list[dict[str, object]] = []
    holder: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        return _success_response(request, holder["material"], seen)

    provider, material = _provider(tmp_path, handler)
    holder["material"] = material
    request_pem = _csr()
    issued = provider.issue_node(NODE_ID, request_pem, NOW)

    assert issued.node_id == NODE_ID
    assert len(seen) == 1
    request = seen[0]["request"]
    assert request.url == f"{CA_URL}/1.0/sign"
    assert request.headers["content-type"] == "application/json"
    assert set(seen[0]["body"]) == {"csr", "ott", "notBefore", "notAfter"}
    assert seen[0]["body"]["csr"] == request_pem.decode()
    assert seen[0]["body"]["notBefore"] == "2026-08-04T12:00:00Z"
    assert seen[0]["body"]["notAfter"] == "2026-08-05T12:00:00Z"
    token = seen[0]["body"]["ott"]
    header = jwt.get_unverified_header(token)
    claims = jwt.decode(token, options={"verify_signature": False})
    assert header == {"alg": "ES256", "kid": material["kid"], "typ": "JWT"}
    assert claims["iss"] == "vonk-forge-agent"
    assert claims["sub"] == NODE_ID
    assert claims["aud"] == f"{CA_URL}/1.0/sign"
    assert claims["sans"] == [f"spiffe://vonk-forge.local/node/{NODE_ID}"]
    assert claims["exp"] - claims["iat"] == 60
    assert claims["nbf"] == claims["iat"] - 30
    assert len(claims["jti"]) >= 43
    certificate = x509.load_pem_x509_certificate(issued.certificate_pem)
    assert issued.serial == str(certificate.serial_number)
    assert issued.fingerprint == certificate.fingerprint(hashes.SHA256()).hex()


def test_renewal_uses_new_signed_csr_and_fresh_serial(tmp_path: Path) -> None:
    seen: list[dict[str, object]] = []
    holder: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        return _success_response(request, holder["material"], seen, serial=5678)

    provider, material = _provider(tmp_path, handler)
    holder["material"] = material
    request_pem = _csr()
    request_id = "r" * 43
    issued = provider.renew_node(
        NODE_ID,
        request_pem,
        NOW,
        request_id=request_id,
    )

    assert seen[0]["body"]["csr"] == request_pem.decode()
    claims = jwt.decode(seen[0]["body"]["ott"], options={"verify_signature": False})
    assert claims["jti"] == request_id
    assert issued.serial == "5678"


def test_provider_issues_the_configured_bounded_acceptance_lifetime(
    tmp_path: Path,
) -> None:
    material = _write_material(tmp_path)
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        leaf = _leaf(
            body["csr"].encode(),
            material,
            lifetime=timedelta(seconds=90),
        )
        return httpx.Response(
            201,
            json={
                "crt": leaf.public_bytes(serialization.Encoding.PEM).decode(),
                "ca": material["intermediate_path"].read_text(),
                "certChain": [
                    leaf.public_bytes(serialization.Encoding.PEM).decode(),
                    material["intermediate_path"].read_text(),
                ],
            },
        )

    provider = StepCertificateAuthority(
        ca_url=CA_URL,
        root_certificate_path=material["root_path"],
        intermediate_certificate_path=material["intermediate_path"],
        provisioner_name="vonk-forge-agent",
        provisioner_kid=material["kid"],
        credential_path=material["credential_path"],
        provisioner_public_jwk_path=material["public_jwk_path"],
        certificate_lifetime_seconds=90,
        transport=httpx.MockTransport(handler),
    )

    issued = provider.issue_node(NODE_ID, _csr(), NOW)

    assert seen[0]["notAfter"] == "2026-08-04T12:01:30Z"
    certificate = x509.load_pem_x509_certificate(issued.certificate_pem)
    assert certificate.not_valid_after_utc - certificate.not_valid_before_utc == (
        timedelta(seconds=90)
    )


def test_revocation_is_authenticated_passive_and_idempotent_in_effect(tmp_path: Path) -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "ok"})

    provider, _ = _provider(tmp_path, handler)
    provider.revoke_node("5678", NOW)
    provider.revoke_node("5678", NOW)

    assert len(seen) == 2
    assert all(set(body) == {"serial", "ott", "reasonCode", "reason", "passive"} for body in seen)
    assert all(body | {"ott": "redacted"} == {"serial": "5678", "ott": "redacted", "reasonCode": 4, "reason": "superseded by Vonk Forge", "passive": True} for body in seen)
    for body in seen:
        claims = jwt.decode(body["ott"], options={"verify_signature": False})
        assert claims["aud"] == f"{CA_URL}/1.0/revoke"
        assert claims["sub"] == "5678"
    assert seen[0]["ott"] != seen[1]["ott"]


def _crl_response(material: dict[str, object], *, last_update: datetime, next_update: datetime | None) -> httpx.Response:
    builder = x509.CertificateRevocationListBuilder().issuer_name(material["intermediate"].subject).last_update(last_update)
    if next_update is not None:
        builder = builder.next_update(next_update)
    crl = builder.sign(material["intermediate_key"], algorithm=None)
    return httpx.Response(200, content=crl.public_bytes(serialization.Encoding.PEM), headers={"content-type": "application/x-pem-file"})


def test_revocation_bundle_accepts_current_bounded_signed_crl(tmp_path: Path) -> None:
    holder: dict[str, object] = {}

    def handler(_: httpx.Request) -> httpx.Response:
        return _crl_response(holder["material"], last_update=NOW - timedelta(minutes=1), next_update=NOW + timedelta(minutes=59))

    provider, material = _provider(tmp_path, handler)
    holder["material"] = material
    bundle = provider.revocation_bundle(NOW)
    assert x509.load_pem_x509_crl(bundle).next_update_utc == NOW + timedelta(minutes=59)


@pytest.mark.parametrize(
    ("last_update", "next_update"),
    (
        (NOW - timedelta(hours=1, minutes=1), NOW + timedelta(minutes=1)),
        (NOW + timedelta(minutes=1), NOW + timedelta(hours=1)),
        (NOW - timedelta(hours=1), NOW - timedelta(seconds=31)),
        (NOW, NOW + timedelta(hours=1, minutes=1)),
    ),
    ids=("stale", "future", "expired", "overlong"),
)
def test_revocation_bundle_rejects_stale_future_expired_or_unbounded_crl(
    tmp_path: Path, last_update: datetime, next_update: datetime | None,
) -> None:
    holder: dict[str, object] = {}

    def handler(_: httpx.Request) -> httpx.Response:
        return _crl_response(holder["material"], last_update=last_update, next_update=next_update)

    provider, material = _provider(tmp_path, handler)
    holder["material"] = material
    with pytest.raises(StepCAError, match="revocation bundle.*freshness"):
        provider.revocation_bundle(NOW)


def test_revocation_bundle_rejects_missing_next_update_window() -> None:
    crl_without_window = SimpleNamespace(last_update_utc=NOW, next_update_utc=None)
    with pytest.raises(StepCAError, match="revocation bundle.*freshness"):
        _validate_crl_freshness(crl_without_window, NOW, timedelta(seconds=30))


@pytest.mark.parametrize("mutation", ("key", "subject", "san", "eku", "usage", "issuer", "lifetime", "chain", "extra-chain"))
def test_rejects_malformed_or_policy_mismatched_sign_responses(tmp_path: Path, mutation: str) -> None:
    holder: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        material = holder["material"]
        body = json.loads(request.content)
        request_pem = body["csr"].encode()
        leaf = _leaf(request_pem, material)
        other_intermediate = _write_material(tmp_path / "other") if mutation in {"issuer", "chain"} else None
        if mutation == "key":
            request_pem = _csr()
        if mutation in {"subject", "san", "eku", "usage", "lifetime", "key", "issuer"}:
            request_obj = x509.load_pem_x509_csr(request_pem)
            node = "spk_fedcba9876543210fedcba9876543210" if mutation in {"subject", "san"} else NODE_ID
            signer = other_intermediate["intermediate_key"] if mutation == "issuer" else material["intermediate_key"]
            issuer = other_intermediate["intermediate"].subject if mutation == "issuer" else material["intermediate"].subject
            builder = (
                x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node)]))
                .issuer_name(issuer).public_key(request_obj.public_key()).serial_number(9876)
                .not_valid_before(NOW).not_valid_after(NOW + (timedelta(hours=25) if mutation == "lifetime" else timedelta(hours=24)))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.KeyUsage(mutation != "usage", False, False, False, False, False, False, False, False), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH if mutation == "eku" else ExtendedKeyUsageOID.CLIENT_AUTH]), critical=True)
                .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(f"spiffe://vonk-forge.local/node/{node}")]), critical=False)
            )
            leaf = builder.sign(signer, algorithm=None)
        chain_ca = other_intermediate["intermediate"] if mutation == "chain" else material["intermediate"]
        leaf_pem = leaf.public_bytes(serialization.Encoding.PEM).decode()
        ca_pem = chain_ca.public_bytes(serialization.Encoding.PEM).decode()
        chain = [leaf_pem, ca_pem]
        if mutation == "extra-chain":
            chain.append(material["root"].public_bytes(serialization.Encoding.PEM).decode())
        return httpx.Response(201, json={"crt": leaf_pem, "ca": ca_pem, "certChain": chain})

    (tmp_path / "other").mkdir(exist_ok=True)
    provider, material = _provider(tmp_path, handler)
    holder["material"] = material
    with pytest.raises(StepCAError):
        provider.issue_node(NODE_ID, _csr(), NOW)


def test_rejects_redirects_proxy_environment_oversize_and_secret_leakage(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.invalid:3128")
    requests: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://attacker.invalid/sign"})

    provider, _ = _provider(tmp_path, redirect)
    with pytest.raises(StepCAError) as caught:
        provider.issue_node(NODE_ID, _csr(), NOW)
    assert len(requests) == 1 and requests[0].url.host == "step-ca"
    assert "eyJ" not in str(caught.value)

    def oversized(_: httpx.Request) -> httpx.Response:
        return httpx.Response(201, content=b"{" + b"x" * 2048 + b"}")

    bounded, _ = _provider(tmp_path / "bounded", oversized, max_response_bytes=1024)
    with pytest.raises(StepCAError, match="too large"):
        bounded.issue_node(NODE_ID, _csr(), NOW)


@pytest.mark.parametrize("url", ("http://step-ca:9000", "https://step-ca:9000/path", "https://user@step-ca:9000", "https://step-ca:9000?x=1"))
def test_rejects_nonfixed_or_non_https_ca_urls(tmp_path: Path, url: str) -> None:
    material = _write_material(tmp_path)
    with pytest.raises(ValueError, match="CA URL"):
        StepCertificateAuthority(
            ca_url=url, root_certificate_path=material["root_path"],
            intermediate_certificate_path=material["intermediate_path"],
            provisioner_name="vonk-forge-agent", provisioner_kid=material["kid"],
            credential_path=material["credential_path"],
            provisioner_public_jwk_path=material["public_jwk_path"],
        )


def test_rejects_symlinked_root_and_credential_files(tmp_path: Path) -> None:
    material = _write_material(tmp_path)
    for argument, target in (("root_certificate_path", material["root_path"]), ("credential_path", material["credential_path"])):
        link = tmp_path / f"{argument}.link"
        link.symlink_to(target)
        values = {
            "ca_url": CA_URL, "root_certificate_path": material["root_path"],
            "intermediate_certificate_path": material["intermediate_path"],
            "provisioner_name": "vonk-forge-agent", "provisioner_kid": material["kid"],
            "credential_path": material["credential_path"],
            "provisioner_public_jwk_path": material["public_jwk_path"],
        }
        values[argument] = link
        with pytest.raises(ValueError, match="regular non-symlink"):
            StepCertificateAuthority(**values)


def test_rejects_public_provisioner_key_with_copied_configured_kid(tmp_path: Path) -> None:
    material = _write_material(tmp_path)
    other = ec.generate_private_key(ec.SECP256R1()).public_key().public_numbers()
    copied = dict(material["public_jwk"])
    copied["x"], copied["y"] = _b64(other.x), _b64(other.y)
    material["public_jwk_path"].write_text(json.dumps(copied))

    with pytest.raises(ValueError, match="does not match private credential"):
        StepCertificateAuthority(
            ca_url=CA_URL, root_certificate_path=material["root_path"],
            intermediate_certificate_path=material["intermediate_path"],
            provisioner_name="vonk-forge-agent", provisioner_kid=material["kid"],
            credential_path=material["credential_path"],
            provisioner_public_jwk_path=material["public_jwk_path"],
        )


def test_health_probe_is_bounded_get_without_body(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "ok"})

    provider, _ = _provider(tmp_path, handler)
    provider.check_health()

    assert len(seen) == 1
    assert seen[0].method == "GET" and seen[0].url == f"{CA_URL}/health"
    assert seen[0].content == b""


def test_production_agent_service_builder_does_not_block_startup_on_step_ca(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[object] = []

    class FakeStepAuthority:
        def __init__(self, **kwargs) -> None:
            calls.append(kwargs)

        def check_health(self) -> None:
            raise AssertionError("API construction must not contact Step CA")

    monkeypatch.setattr("vonk_control.step_ca.StepCertificateAuthority", FakeStepAuthority)
    engine = create_engine(f"sqlite:///{tmp_path / 'runtime.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    settings = _builder_settings(tmp_path, direct_fabric_cidrs="192.168.100.0/24")

    services = build_agent_services(settings, sessions, lambda: NOW)

    assert isinstance(services, AgentApiServices)
    assert len(calls) == 1
    assert calls[0]["ca_url"] == CA_URL
    assert settings.agent_artifact_root.is_dir()
    assert isinstance(services.presence, AgentPresenceService)


def test_production_agent_service_builder_always_constructs_step_ca(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[dict[str, object]] = []

    class DeferredStepAuthority:
        def __init__(self, **kwargs) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(
        "vonk_control.step_ca.StepCertificateAuthority", DeferredStepAuthority
    )
    settings = _builder_settings(
        tmp_path, direct_fabric_cidrs="192.168.100.0/24"
    )
    build_agent_services(settings, object(), lambda: NOW)

    assert len(calls) == 1
    assert calls[0]["ca_url"] == CA_URL


def test_tracked_step_ca_template_is_public_only_and_matches_provider_validation() -> None:
    config_path = Path(__file__).resolve().parents[2] / "deploy/compose/step-ca/ca.json"
    config = json.loads(config_path.read_text())
    provisioner = config["authority"]["provisioners"][0]

    assert provisioner["type"] == "JWK" and provisioner["name"] == "vonk-forge-agent"
    assert "encryptedKey" not in provisioner and "d" not in provisioner["key"]
    assert provisioner["claims"] == {
        "minTLSCertDuration": "24h", "maxTLSCertDuration": "24h",
        "defaultTLSCertDuration": "24h", "disableRenewal": True,
        "disableSmallstepExtensions": True,
    }
    template = provisioner["options"]["x509"]["template"]
    assert "digitalSignature" in template and "clientAuth" in template
    assert "serverAuth" not in template
    assert config["crl"] == {
        "enabled": True, "generateOnRevoke": True,
        "cacheDuration": "1h", "renewPeriod": "30m",
    }


def test_pinned_step_ca_issues_tracked_leaf_profile_and_serves_fresh_crl(tmp_path: Path, monkeypatch) -> None:
    """Exercise the tracked public config against the exact production image."""
    if shutil.which("docker") is None or subprocess.run(
        ["docker", "info"], capture_output=True, check=False
    ).returncode != 0:
        if os.environ.get("CI"):
            pytest.fail("Docker daemon is required for the pinned step-ca integration test")
        pytest.skip("Docker daemon is required for the pinned step-ca integration test")
    tmp_path.chmod(0o777)
    root_password = tmp_path / "root-password"
    intermediate_password = tmp_path / "intermediate-password"
    root_password.write_text("fixture-root-password-with-entropy\n")
    intermediate_password.write_text("fixture-intermediate-password-with-entropy\n")
    user = f"{os.getuid()}:{os.getgid()}"

    def step(*arguments: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([
            "docker", "run", "--rm", "--user", user,
            "-i",
            "-v", f"{tmp_path}:/work", "--entrypoint", "step", STEP_CA_IMAGE,
            *arguments,
        ], input=input_text, capture_output=True, text=True, timeout=60, check=True)

    root = tmp_path / "root_ca.crt"
    root_key = tmp_path / "root_ca.key"  # noqa: F841 - created by step ca init
    intermediate = tmp_path / "intermediate_ca.crt"
    intermediate_key = tmp_path / "intermediate_ca_key"
    public_jwk = tmp_path / "agent-ca-public.jwk"
    private_jwk = tmp_path / "agent-ca-credential"
    step(
        "certificate", "create", "Vonk Forge Test Root", "/work/root_ca.crt", "/work/root_ca.key",
        "--profile", "root-ca", "--kty", "OKP", "--curve", "Ed25519", "--not-after", "87600h",
        "--password-file", "/work/root-password",
    )
    step(
        "certificate", "create", "Vonk Forge Test Intermediate", "/work/intermediate_ca.crt", "/work/intermediate_ca_key",
        "--profile", "intermediate-ca", "--kty", "OKP", "--curve", "Ed25519", "--not-after", "8760h",
        "--ca", "/work/root_ca.crt", "--ca-key", "/work/root_ca.key",
        "--ca-password-file", "/work/root-password", "--password-file", "/work/intermediate-password",
    )
    step(
        "crypto", "jwk", "create", "/work/agent-ca-public.jwk", "/work/agent-ca-credential",
        "--kty", "EC", "--crv", "P-256", "--no-password", "--insecure",
    )
    public = json.loads(public_jwk.read_text())
    kid = step("crypto", "jwk", "thumbprint", input_text=public_jwk.read_text()).stdout.strip()
    public.update({"kid": kid, "alg": "ES256", "use": "sig"})
    private = json.loads(private_jwk.read_text())
    private.update({"kid": kid, "alg": "ES256", "use": "sig"})
    public_jwk.write_text(json.dumps(public))
    private_jwk.write_text(json.dumps(private))
    # Docker bind mounts retain host ownership, unlike the production secret
    # projection (1000:1000, mode 0400). These contain fixed test-only values;
    # make them readable by the image's configured `step` user without relying
    # on the GitHub runner and image having the same numeric UID.
    root.chmod(0o444)
    intermediate.chmod(0o444)
    intermediate_key.chmod(0o444)
    intermediate_password.chmod(0o444)

    config_path = Path(__file__).resolve().parents[2] / "deploy/compose/step-ca/ca.json"
    config = json.loads(config_path.read_text())
    config["authority"]["provisioners"][0]["key"] = public
    generated_config = tmp_path / "ca.json"
    generated_config.write_text(json.dumps(config))
    database = tmp_path / "db"
    database.mkdir(mode=0o777)
    database.chmod(0o777)
    container = f"vonk-step-ca-test-{uuid.uuid4().hex}"
    subprocess.run([
        "docker", "run", "-d", "--name", container, "-p", "127.0.0.1::9000",
        "-v", f"{generated_config}:/home/step/config/ca.json:ro",
        "-v", f"{root}:/run/vonk-normalized-secrets/step-ca/root-certificate:ro",
        "-v", f"{intermediate}:/run/vonk-normalized-secrets/step-ca/intermediate-certificate:ro",
        "-v", f"{intermediate_key}:/run/vonk-normalized-secrets/step-ca/intermediate-key:ro",
        "-v", f"{intermediate_password}:/run/vonk-normalized-secrets/step-ca/password:ro",
        "-v", f"{database}:/home/step/db",
        "--entrypoint", "step-ca", STEP_CA_IMAGE,
        "/home/step/config/ca.json", "--password-file", "/run/vonk-normalized-secrets/step-ca/password",
    ], check=True, capture_output=True, text=True, timeout=30)
    try:
        port_output = subprocess.run(
            ["docker", "port", container, "9000/tcp"], check=True,
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        port = port_output.rsplit(":", 1)[1]
        real_getaddrinfo = socket.getaddrinfo
        monkeypatch.setattr(
            socket, "getaddrinfo",
            lambda host, *args, **kwargs: real_getaddrinfo("127.0.0.1" if host == "step-ca" else host, *args, **kwargs),
        )
        def authority(mapped_port: str) -> StepCertificateAuthority:
            return StepCertificateAuthority(
                ca_url=f"https://step-ca:{mapped_port}",
                root_certificate_path=root,
                intermediate_certificate_path=intermediate,
                provisioner_name="vonk-forge-agent",
                provisioner_kid=kid,
                credential_path=private_jwk,
                provisioner_public_jwk_path=public_jwk,
                timeout_seconds=2.0,
            )

        provider = authority(port)
        deadline = time.monotonic() + 15
        while True:
            try:
                provider.check_health()
                break
            except StepCAError:
                if time.monotonic() >= deadline:
                    logs = subprocess.run(
                        ["docker", "logs", container],
                        capture_output=True,
                        text=True,
                        check=False,
                    ).stderr
                    pytest.fail(f"pinned step-ca did not become healthy: {logs}")
                time.sleep(0.1)
        now = datetime.now(UTC).replace(microsecond=0)
        issued = provider.issue_node(NODE_ID, _csr(), now)
        certificate = x509.load_pem_x509_certificate(issued.certificate_pem)
        extensions = {extension.oid: extension for extension in certificate.extensions}
        assert ExtensionOID.BASIC_CONSTRAINTS not in extensions
        assert extensions[ExtensionOID.KEY_USAGE].critical is True
        assert extensions[ExtensionOID.EXTENDED_KEY_USAGE].critical is False
        assert extensions[ExtensionOID.EXTENDED_KEY_USAGE].value == x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH])
        renewed = provider.renew_node(
            NODE_ID,
            _csr(),
            datetime.now(UTC).replace(microsecond=0),
            request_id="r" * 43,
        )
        assert renewed.serial != issued.serial
        provider.revoke_node(issued.serial, datetime.now(UTC).replace(microsecond=0))
        crl = x509.load_pem_x509_crl(provider.revocation_bundle(datetime.now(UTC).replace(microsecond=0)))
        assert issued.serial in {str(record.serial_number) for record in crl}

        subprocess.run(
            ["docker", "restart", container],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        restarted_port_output = subprocess.run(
            ["docker", "port", container, "9000/tcp"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        restarted_port = restarted_port_output.rsplit(":", 1)[1]
        # Docker may reassign an anonymous host port on restart. Production
        # reaches step-ca through its stable Compose service address, so renew
        # the test client against the container's current test-only mapping.
        provider = authority(restarted_port)
        deadline = time.monotonic() + 45
        while True:
            try:
                provider.check_health()
                break
            except StepCAError:
                running = subprocess.run(
                    ["docker", "inspect", "--format", "{{.State.Running}}", container],
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.strip()
                if running == "false":
                    logs = subprocess.run(
                        ["docker", "logs", container],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    pytest.fail(
                        "pinned step-ca exited during restart:\n"
                        f"{logs.stdout}{logs.stderr}"
                    )
                if time.monotonic() >= deadline:
                    logs = subprocess.run(
                        ["docker", "logs", container],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    pytest.fail(
                        "pinned step-ca did not recover after restart:\n"
                        f"{logs.stdout}{logs.stderr}"
                    )
                time.sleep(0.1)
        persisted_crl = x509.load_pem_x509_crl(
            provider.revocation_bundle(datetime.now(UTC).replace(microsecond=0))
        )
        assert issued.serial in {
            str(record.serial_number) for record in persisted_crl
        }
    finally:
        subprocess.run(
            ["docker", "rm", "--force", container],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
