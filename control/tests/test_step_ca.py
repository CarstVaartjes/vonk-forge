from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TypedDict

import httpx2
import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol.state_machines import CertificateIssuancePurpose
from vonk_control.agent_api import AgentApiServices
from vonk_control.api import build_agent_services
from vonk_control.ca_issuance_contract import (
    CertificateIssuanceBinding,
    CertificateIssuedReply,
)
from vonk_control.models import Base

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


def _der(tag: int, payload: bytes) -> bytes:
    """Encode one definite-length DER element."""
    if len(payload) < 0x80:
        length = bytes([len(payload)])
    else:
        encoded = len(payload).to_bytes((len(payload).bit_length() + 7) // 8, "big")
        length = bytes([0x80 | len(encoded)]) + encoded
    return bytes([tag]) + length + payload


def _crl_without_next_update() -> x509.CertificateRevocationList:
    """A real CRL whose TBSCertList omits the optional ``nextUpdate`` field.

    ``CertificateRevocationListBuilder.sign`` refuses to produce this shape, but
    a CA that omits the window is exactly what the freshness check must reject,
    so the bytes are encoded here rather than the input being faked.
    """
    ecdsa_with_sha256 = _der(0x06, bytes.fromhex("2a8648ce3d040302"))
    algorithm = _der(0x30, ecdsa_with_sha256)
    common_name = _der(0x06, bytes.fromhex("550403"))
    issuer = _der(
        0x30, _der(0x31, _der(0x30, common_name + _der(0x0C, b"Vonk Forge Test CA")))
    )
    this_update = _der(0x17, b"260804120000Z")
    tbs_cert_list = _der(0x30, algorithm + issuer + this_update)
    signature = _der(0x03, b"\x00" + bytes(64))
    return x509.load_der_x509_crl(_der(0x30, tbs_cert_list + algorithm + signature))


class _Material(TypedDict):
    root: x509.Certificate
    root_path: Path
    intermediate: x509.Certificate
    intermediate_key: ed25519.Ed25519PrivateKey
    intermediate_path: Path
    credential_path: Path
    public_jwk_path: Path
    public_jwk: dict[str, str]
    kid: str


class _SignRequestBody(TypedDict):
    csr: str
    ott: str
    request: dict[str, object]
    mode: str


class _SignExchange(TypedDict):
    request: httpx2.Request
    body: _SignRequestBody


def _b64(value: int) -> str:
    return base64.urlsafe_b64encode(value.to_bytes(32, "big")).rstrip(b"=").decode()


def _write_material(tmp_path: Path) -> _Material:
    tmp_path.mkdir(parents=True, exist_ok=True)
    root_key = ed25519.Ed25519PrivateKey.generate()
    root_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Vonk Forge Offline Root")]
    )
    root = (
        x509.CertificateBuilder()
        .subject_name(root_name)
        .issuer_name(root_name)
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            critical=True,
        )
        .sign(root_key, algorithm=None)
    )
    intermediate_key = ed25519.Ed25519PrivateKey.generate()
    intermediate_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Vonk Forge Agent Intermediate")]
    )
    intermediate = (
        x509.CertificateBuilder()
        .subject_name(intermediate_name)
        .issuer_name(root.subject)
        .public_key(intermediate_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            critical=True,
        )
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
    public_jwk = {
        "kty": "EC",
        "crv": "P-256",
        "use": "sig",
        "alg": "ES256",
        "x": _b64(numbers.x),
        "y": _b64(numbers.y),
    }
    thumbprint_input = json.dumps(
        {name: public_jwk[name] for name in ("crv", "kty", "x", "y")},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    import hashlib

    kid = (
        base64.urlsafe_b64encode(hashlib.sha256(thumbprint_input).digest())
        .rstrip(b"=")
        .decode()
    )
    public_jwk["kid"] = kid
    private_jwk = public_jwk | {
        "d": _b64(provisioner_key.private_numbers().private_value)
    }
    credential_path.write_text(json.dumps(private_jwk))
    public_jwk_path.write_text(json.dumps(public_jwk))
    credential_path.chmod(0o600)
    return {
        "root": root,
        "root_path": root_path,
        "intermediate": intermediate,
        "intermediate_key": intermediate_key,
        "intermediate_path": intermediate_path,
        "credential_path": credential_path,
        "public_jwk_path": public_jwk_path,
        "public_jwk": public_jwk,
        "kid": kid,
    }


def _csr(node_id: str = NODE_ID) -> bytes:
    key = ed25519.Ed25519PrivateKey.generate()
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id)]))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(
                        f"spiffe://vonk-forge.local/node/{node_id}"
                    )
                ]
            ),
            critical=False,
        )
        .sign(key, algorithm=None)
        .public_bytes(serialization.Encoding.PEM)
    )


def _leaf(
    csr_pem: bytes,
    material: _Material,
    *,
    now: datetime = NOW,
    serial: int = 1234,
    lifetime_seconds: int = 2592000,
) -> x509.Certificate:
    request = x509.load_pem_x509_csr(csr_pem)
    return (
        x509.CertificateBuilder()
        .subject_name(request.subject)
        .issuer_name(material["intermediate"].subject)
        .public_key(request.public_key())
        .serial_number(serial)
        .not_valid_before(now)
        .not_valid_after(now + timedelta(seconds=lifetime_seconds))
        .add_extension(
            x509.KeyUsage(True, False, False, False, False, False, False, False, False),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
        )
        .add_extension(
            request.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value,
            critical=False,
        )
        .sign(material["intermediate_key"], algorithm=None)
    )


def _provider(
    tmp_path: Path,
    handler,
    *,
    certificate_lifetime_seconds: int = 2592000,
    max_response_bytes: int = 64 * 1024,
) -> tuple[StepCertificateAuthority, _Material]:
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
        certificate_lifetime_seconds=certificate_lifetime_seconds,
        max_response_bytes=max_response_bytes,
        transport=httpx2.MockTransport(handler),
    )
    return provider, material


def _issue(provider: StepCertificateAuthority, node_id: str, csr: bytes, now: datetime):
    binding = provider.prepare_request(
        node_id,
        csr,
        now,
        purpose=CertificateIssuancePurpose.ENROLLMENT,
        source_serial=None,
        generation=1,
    )
    return provider.issue_node(node_id, csr, now, request=binding)


def _renew(
    provider: StepCertificateAuthority,
    node_id: str,
    csr: bytes,
    now: datetime,
    *,
    request_id: str,
):
    binding = provider.prepare_request(
        node_id, csr, now, purpose="rotation", source_serial="1", generation=2
    )
    binding = CertificateIssuanceBinding.model_validate(
        {**binding.model_dump(mode="json"), "request_id": request_id}
    )
    return provider.renew_node(node_id, csr, now, request=binding)


def _helper_key(path: Path) -> Path:
    path.write_bytes(
        ed25519.Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    return path


def _sessions(tmp_path: Path) -> sessionmaker:
    engine = create_engine(f"sqlite:///{tmp_path / 'runtime.sqlite'}")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _builder_settings(tmp_path: Path, *, direct_fabric_cidrs: str) -> SimpleNamespace:
    material = _write_material(tmp_path)
    return SimpleNamespace(
        secrets_root=tmp_path,
        agent_runtime_enabled=True,
        agent_controller_origin="https://agents.example.test:8443",
        agent_enrollment_origin="https://enroll.example.test:8443",
        nas_lan_ip="192.168.1.231",
        agent_service_hostnames=(
            "control.example.test",
            "enroll.example.test",
            "agents.example.test",
            "registry.example.test",
        ),
        install_channel="stable",
        controller_ca_path=material["root_path"],
        agent_intermediate_certificate_path=material["intermediate_path"],
        agent_ca_root_path=material["root_path"],
        agent_ca_provisioner_public_jwk_path=material["public_jwk_path"],
        agent_ca_provisioner_kid=material["kid"],
        agent_artifact_root=tmp_path / "artifacts",
        host_runtime_grant_private_key_path=_helper_key(tmp_path / "host-key"),
        management_cidrs="10.0.0.0/24",
        direct_fabric_cidrs=direct_fabric_cidrs,
    )


def _success_response(
    request: httpx2.Request,
    material: _Material,
    seen: list[_SignExchange],
    *,
    serial: int = 1234,
) -> httpx2.Response:
    body = json.loads(request.content)
    seen.append({"request": request, "body": body})
    leaf = _leaf(
        body["csr"].encode(),
        material,
        serial=int(body["request"]["serial"]),
        now=datetime.fromisoformat(body["request"]["not_before"]),
    )
    leaf_pem = leaf.public_bytes(serialization.Encoding.PEM).decode()
    intermediate_pem = (
        material["intermediate"].public_bytes(serialization.Encoding.PEM).decode()
    )
    return httpx2.Response(
        201,
        json={
            "state": "issued",
            "request": body["request"],
            "crt": leaf_pem,
            "ca": intermediate_pem,
            "certChain": [leaf_pem, intermediate_pem],
        },
    )


def test_sign_uses_fixed_policy_short_lived_one_use_authorization_and_node_signed_csr(
    tmp_path: Path,
) -> None:
    seen: list[_SignExchange] = []
    holder: dict[str, _Material] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        return _success_response(request, holder["material"], seen)

    provider, material = _provider(tmp_path, handler)
    holder["material"] = material
    request_pem = _csr()
    issued = _issue(provider, NODE_ID, request_pem, NOW)

    assert issued.node_id == NODE_ID
    assert len(seen) == 1
    request = seen[0]["request"]
    assert request.url == f"{CA_URL}/1.0/vonk/sign"
    assert request.headers["content-type"] == "application/json"
    assert seen[0]["body"]["mode"] == "issue"
    assert seen[0]["body"]["csr"] == request_pem.decode()
    assert seen[0]["body"]["request"]["not_before"] == "2026-08-04T12:00:00Z"
    assert seen[0]["body"]["request"]["not_after"] == "2026-09-03T12:00:00Z"
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


@pytest.mark.parametrize("lifetime", (True, 0, -1))
def test_rejects_invalid_configured_certificate_lifetime(
    tmp_path: Path,
    lifetime: int,
) -> None:
    calls = []

    def unavailable(request):
        calls.append(request)
        return httpx2.Response(500)

    configured = None
    try:
        configured, _ = _provider(
            tmp_path, unavailable, certificate_lifetime_seconds=lifetime
        )
    except Exception:  # noqa: BLE001, S110 -- invalid input cannot construct an authority or cause HTTP
        pass
    assert configured is None
    assert calls == []
    repaired, _ = _provider(tmp_path, unavailable, certificate_lifetime_seconds=60)
    accepted = repaired.prepare_request(
        NODE_ID,
        _csr(),
        NOW,
        purpose=CertificateIssuancePurpose.ENROLLMENT,
        source_serial=None,
        generation=1,
    )
    assert datetime.fromisoformat(accepted.not_after) - datetime.fromisoformat(
        accepted.not_before
    ) == timedelta(seconds=60)
    assert calls == []
    repaired.close()


def test_renewal_uses_new_signed_csr_and_fresh_serial(tmp_path: Path) -> None:
    seen: list[_SignExchange] = []
    holder: dict[str, _Material] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        return _success_response(request, holder["material"], seen, serial=5678)

    provider, material = _provider(tmp_path, handler)
    holder["material"] = material
    request_pem = _csr()
    request_id = "r" * 43
    issued = _renew(
        provider,
        NODE_ID,
        request_pem,
        NOW,
        request_id=request_id,
    )

    assert seen[0]["body"]["csr"] == request_pem.decode()
    claims = jwt.decode(seen[0]["body"]["ott"], options={"verify_signature": False})
    assert claims["jti"] != request_id
    assert claims["vonk"]["request_id"] == request_id
    assert issued.serial != "1"


def test_revocation_is_authenticated_passive_and_idempotent_in_effect(
    tmp_path: Path,
) -> None:
    from vonk_control.step_ca import _RevokeRequest, _TokenClaims

    revoked: set[str] = set()
    tokens: set[str] = set()
    requests: list[_RevokeRequest] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = _RevokeRequest.model_validate_json(request.content)
        try:
            claims = _TokenClaims.model_validate(
                jwt.decode(
                    body.ott,
                    jwt.PyJWK.from_dict(material["public_jwk"]).key,
                    algorithms=["ES256"],
                    audience=f"{CA_URL}/1.0/revoke",
                    issuer="vonk-forge-agent",
                    options={
                        "verify_exp": False,
                        "verify_nbf": False,
                        "verify_iat": False,
                    },
                )
            )
        except jwt.InvalidTokenError:
            return httpx2.Response(401)
        if (
            claims.sub != body.serial
            or claims.jti in tokens
            or not claims.nbf <= int(NOW.timestamp()) < claims.exp
        ):
            return httpx2.Response(401)
        assert body.passive is True
        tokens.add(claims.jti)
        revoked.add(body.serial)
        requests.append(body)
        return httpx2.Response(200, json={"status": "ok"})

    provider, material = _provider(tmp_path, handler)
    provider.revoke_node("5678", NOW)
    provider.revoke_node("5678", NOW)
    assert revoked == {"5678"}
    assert requests[0].ott != requests[1].ott
    forged = requests[0].model_copy(
        update={"serial": "9999", "ott": requests[0].ott + "tampered"}
    )
    with pytest.raises(Exception):  # noqa: B017 -- forged ingress cannot change revocation effects
        provider._request("POST", "/1.0/revoke", forged, accept="application/json")
    assert revoked == {"5678"}
    provider.revoke_node("9999", NOW)
    assert revoked == {"5678", "9999"}


def _crl_response(
    material: _Material, *, last_update: datetime, next_update: datetime | None
) -> httpx2.Response:
    builder = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(material["intermediate"].subject)
        .last_update(last_update)
    )
    if next_update is not None:
        builder = builder.next_update(next_update)
    crl = builder.sign(material["intermediate_key"], algorithm=None)
    return httpx2.Response(
        200,
        content=crl.public_bytes(serialization.Encoding.PEM),
        headers={"content-type": "application/x-pem-file"},
    )


def test_revocation_bundle_accepts_current_bounded_signed_crl(tmp_path: Path) -> None:
    holder: dict[str, _Material] = {}

    def handler(_: httpx2.Request) -> httpx2.Response:
        return _crl_response(
            holder["material"],
            last_update=NOW - timedelta(minutes=1),
            next_update=NOW + timedelta(minutes=59),
        )

    provider, material = _provider(tmp_path, handler)
    holder["material"] = material
    bundle = provider.revocation_bundle(NOW)
    assert isinstance(bundle, bytes)
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
def test_revocation_bundle_unknown_ends_and_fresh_signed_observation_succeeds(
    tmp_path: Path,
    last_update: datetime,
    next_update: datetime | None,
) -> None:
    holder: dict[str, _Material] = {}
    calls = []
    repaired = False

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return _crl_response(
            holder["material"],
            last_update=NOW - timedelta(minutes=1) if repaired else last_update,
            next_update=NOW + timedelta(minutes=59) if repaired else next_update,
        )

    provider, material = _provider(tmp_path, handler)
    holder["material"] = material
    unknown = provider.revocation_bundle(NOW)
    assert not isinstance(unknown, bytes)
    assert len(calls) == 4
    repaired = True
    fresh = provider.revocation_bundle(NOW)
    assert isinstance(fresh, bytes)
    assert x509.load_pem_x509_crl(fresh).next_update_utc == NOW + timedelta(minutes=59)
    assert len(calls) == 5


def test_revocation_bundle_missing_window_cannot_be_used_then_fresh_window_validates() -> (
    None
):
    crl_without_window = _crl_without_next_update()
    assert crl_without_window.next_update_utc is None
    with pytest.raises(Exception):  # noqa: B017 -- unknown is unusable; corrected observation below
        _validate_crl_freshness(crl_without_window, NOW, timedelta(seconds=30))
    # No missing-window observation is adopted as revocation evidence.
    key = ed25519.Ed25519PrivateKey.generate()
    fresh = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(crl_without_window.issuer)
        .last_update(NOW)
        .next_update(NOW + timedelta(minutes=59))
        .sign(key, algorithm=None)
    )
    _validate_crl_freshness(fresh, NOW, timedelta(seconds=30))


@pytest.mark.parametrize(
    "mutation",
    (
        "key",
        "subject",
        "san",
        "eku",
        "usage",
        "issuer",
        "lifetime",
        "chain",
        "extra-chain",
    ),
)
def test_unverified_certificate_identity_or_signature_has_no_adopted_effect(
    tmp_path: Path, mutation: str
) -> None:
    holder: dict[str, _Material] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        material = holder["material"]
        body = json.loads(request.content)
        request_pem = body["csr"].encode()
        leaf = _leaf(request_pem, material)
        other_intermediate = (
            _write_material(tmp_path / "other")
            if mutation in {"issuer", "chain"}
            else None
        )
        if mutation == "key":
            request_pem = _csr()
        if mutation in {"subject", "san", "eku", "usage", "lifetime", "key", "issuer"}:
            request_obj = x509.load_pem_x509_csr(request_pem)
            node = (
                "spk_fedcba9876543210fedcba9876543210"
                if mutation in {"subject", "san"}
                else NODE_ID
            )
            if mutation == "issuer":
                assert other_intermediate is not None
                signer = other_intermediate["intermediate_key"]
                issuer = other_intermediate["intermediate"].subject
            else:
                signer = material["intermediate_key"]
                issuer = material["intermediate"].subject
            builder = (
                x509.CertificateBuilder()
                .subject_name(
                    x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node)])
                )
                .issuer_name(issuer)
                .public_key(request_obj.public_key())
                .serial_number(9876)
                .not_valid_before(NOW)
                .not_valid_after(
                    NOW
                    + (
                        timedelta(days=30, hours=1)
                        if mutation == "lifetime"
                        else timedelta(days=30)
                    )
                )
                .add_extension(
                    x509.BasicConstraints(ca=False, path_length=None), critical=True
                )
                .add_extension(
                    x509.KeyUsage(
                        mutation != "usage",
                        False,
                        False,
                        False,
                        False,
                        False,
                        False,
                        False,
                        False,
                    ),
                    critical=True,
                )
                .add_extension(
                    x509.ExtendedKeyUsage(
                        [
                            ExtendedKeyUsageOID.SERVER_AUTH
                            if mutation == "eku"
                            else ExtendedKeyUsageOID.CLIENT_AUTH
                        ]
                    ),
                    critical=True,
                )
                .add_extension(
                    x509.SubjectAlternativeName(
                        [
                            x509.UniformResourceIdentifier(
                                f"spiffe://vonk-forge.local/node/{node}"
                            )
                        ]
                    ),
                    critical=False,
                )
            )
            leaf = builder.sign(signer, algorithm=None)
        if mutation == "chain":
            assert other_intermediate is not None
            chain_ca = other_intermediate["intermediate"]
        else:
            chain_ca = material["intermediate"]
        leaf_pem = leaf.public_bytes(serialization.Encoding.PEM).decode()
        ca_pem = chain_ca.public_bytes(serialization.Encoding.PEM).decode()
        chain = [leaf_pem, ca_pem]
        if mutation == "extra-chain":
            chain.append(
                material["root"].public_bytes(serialization.Encoding.PEM).decode()
            )
        return httpx2.Response(
            201,
            json={
                "state": "issued",
                "request": body["request"],
                "crt": leaf_pem,
                "ca": ca_pem,
                "certChain": chain,
            },
        )

    (tmp_path / "other").mkdir(exist_ok=True)
    provider, material = _provider(tmp_path, handler)
    holder["material"] = material
    with pytest.raises(StepCAError):
        _issue(provider, NODE_ID, _csr(), NOW)


def test_rejects_redirects_proxy_environment_oversize_and_secret_leakage(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.invalid:3128")
    requests: list[httpx2.Request] = []

    def redirect(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            307, headers={"location": "https://attacker.invalid/sign"}
        )

    provider, _ = _provider(tmp_path, redirect)
    with pytest.raises(Exception) as caught:
        _issue(provider, NODE_ID, _csr(), NOW)
    assert len(requests) == 1 and requests[0].url.host == "step-ca"
    assert "eyJ" not in str(caught.value)

    holder: dict[str, _Material] = {}
    seen: list[_SignExchange] = []
    unavailable = True

    def oversized(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if unavailable:
            return httpx2.Response(201, content=b"{" + b"x" * 65536 + b"}")
        return _success_response(request, holder["material"], seen)

    bounded, material = _provider(
        tmp_path / "bounded", oversized, max_response_bytes=1024
    )
    holder["material"] = material
    csr = _csr()
    binding = bounded.prepare_request(
        NODE_ID,
        csr,
        NOW,
        purpose=CertificateIssuancePurpose.ENROLLMENT,
        source_serial=None,
        generation=1,
    )
    with pytest.raises(Exception):  # noqa: B017 -- ending witness; no credential returned and exact recovery below
        bounded.issue_node(NODE_ID, csr, NOW, request=binding)
    assert len(requests) == 2
    assert not seen
    unavailable = False
    issued = bounded.issue_node(NODE_ID, csr, NOW, request=binding)
    assert issued.serial == binding.serial
    assert seen[-1]["body"]["request"]["request_id"] == binding.request_id
    fresh = _issue(bounded, NODE_ID, _csr(), NOW)
    assert fresh.serial != issued.serial


def test_sign_wire_budget_stays_bounded_with_larger_crl_transport_budget(
    tmp_path: Path,
) -> None:
    holder: dict[str, _Material] = {}
    seen: list[_SignExchange] = []
    fail_sign = True

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/1.0/crl":
            return httpx2.Response(200, content=b"x" * (70 * 1024))
        if fail_sign:
            return httpx2.Response(201, content=b"x" * (64 * 1024 + 1))
        return _success_response(request, holder["material"], seen)

    provider, material = _provider(tmp_path, handler, max_response_bytes=1024 * 1024)
    holder["material"] = material
    assert (
        len(provider._request("GET", "/1.0/crl", None, accept="application/pkix-crl"))
        == 70 * 1024
    )
    csr = _csr()
    binding = provider.prepare_request(
        NODE_ID,
        csr,
        NOW,
        purpose=CertificateIssuancePurpose.ENROLLMENT,
        source_serial=None,
        generation=1,
    )
    with pytest.raises(Exception):  # noqa: B017 -- ending witness; same-binding success below
        provider.issue_node(NODE_ID, csr, NOW, request=binding)
    fail_sign = False
    issued = provider.issue_node(NODE_ID, csr, NOW, request=binding)
    assert issued.serial == binding.serial


@pytest.mark.parametrize(
    "url",
    (
        "http://step-ca:9000",
        "https://step-ca:9000/path",
        "https://user@step-ca:9000",
        "https://step-ca:9000?x=1",
    ),
)
def test_rejects_nonfixed_or_non_https_ca_urls(tmp_path: Path, url: str) -> None:
    material = _write_material(tmp_path)
    with pytest.raises(ValueError):
        StepCertificateAuthority(
            ca_url=url,
            root_certificate_path=material["root_path"],
            intermediate_certificate_path=material["intermediate_path"],
            provisioner_name="vonk-forge-agent",
            provisioner_kid=material["kid"],
            credential_path=material["credential_path"],
            provisioner_public_jwk_path=material["public_jwk_path"],
        )


def test_rejects_symlinked_root_and_credential_files(tmp_path: Path) -> None:
    material = _write_material(tmp_path)
    for argument, target in (
        ("root_certificate_path", material["root_path"]),
        ("credential_path", material["credential_path"]),
    ):
        link = tmp_path / f"{argument}.link"
        link.symlink_to(target)
        values = {
            "ca_url": CA_URL,
            "root_certificate_path": material["root_path"],
            "intermediate_certificate_path": material["intermediate_path"],
            "provisioner_name": "vonk-forge-agent",
            "provisioner_kid": material["kid"],
            "credential_path": material["credential_path"],
            "provisioner_public_jwk_path": material["public_jwk_path"],
        }
        values[argument] = link
        with pytest.raises(ValueError):
            StepCertificateAuthority(**values)


def test_rejects_public_provisioner_key_with_copied_configured_kid(
    tmp_path: Path,
) -> None:
    material = _write_material(tmp_path)
    other = ec.generate_private_key(ec.SECP256R1()).public_key().public_numbers()
    copied = dict(material["public_jwk"])
    copied["x"], copied["y"] = _b64(other.x), _b64(other.y)
    material["public_jwk_path"].write_text(json.dumps(copied))

    with pytest.raises(ValueError):
        StepCertificateAuthority(
            ca_url=CA_URL,
            root_certificate_path=material["root_path"],
            intermediate_certificate_path=material["intermediate_path"],
            provisioner_name="vonk-forge-agent",
            provisioner_kid=material["kid"],
            credential_path=material["credential_path"],
            provisioner_public_jwk_path=material["public_jwk_path"],
        )


def test_health_probe_is_bounded_get_without_body(tmp_path: Path) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"status": "ok"})

    provider, _ = _provider(tmp_path, handler)
    provider.check_health()

    assert len(seen) == 1
    assert seen[0].method == "GET" and seen[0].url == f"{CA_URL}/health"
    assert seen[0].content == b""


def test_production_agent_service_builder_does_not_block_startup_on_step_ca(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[dict[str, object]] = []

    class FakeStepAuthority:
        def __init__(self, **kwargs) -> None:
            calls.append(kwargs)

        def check_health(self) -> None:
            raise AssertionError("API construction must not contact Step CA")

    monkeypatch.setattr(
        "vonk_control.local_ca.LocalCertificateAuthority", FakeStepAuthority
    )
    settings = _builder_settings(tmp_path, direct_fabric_cidrs="192.168.100.0/24")

    services = build_agent_services(settings, _sessions(tmp_path), lambda: NOW)

    assert isinstance(services, AgentApiServices)
    assert calls == []
    assert services.enrollment is not None
    assert services.presence is not None


def test_production_agent_service_builder_defers_step_ca(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[dict[str, object]] = []

    class DeferredStepAuthority:
        def __init__(self, **kwargs) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(
        "vonk_control.local_ca.LocalCertificateAuthority", DeferredStepAuthority
    )
    settings = _builder_settings(tmp_path, direct_fabric_cidrs="192.168.100.0/24")
    build_agent_services(settings, _sessions(tmp_path), lambda: NOW)

    assert calls == []


# Slow by design: fixture PKI uses the pinned step CLI, then the candidate
# journal CA serves the existing keys and persistent database.


def _assert_unavailable_enrollment(client, request) -> None:
    from vonk_control.capability_contract import (
        CapabilityUnavailableReply,
        ControllerCapability,
    )

    response = client.post("/agent/enroll", json=request.model_dump(mode="json"))
    assert response.status_code == 503
    reply = CapabilityUnavailableReply.model_validate_json(response.content)
    assert (
        reply.capability == ControllerCapability.CERTIFICATE_AUTHORITY
        and reply.retryable
    )


@pytest.mark.parametrize(
    "lifetime", (1, 89, 90, 86400, 2592000, 2592001, 120 * 86400, 2 * 365 * 86400)
)
def test_configured_ca_lifetime_is_trusted(
    tmp_path: Path,
    lifetime: int,
) -> None:
    """The installed step-ca configuration owns the agent certificate lifetime.

    A Controller whose CA is configured shorter than 30 days still starts and
    signs with that lifetime; it never refuses to start over it.
    """
    provider, _material = _provider(
        tmp_path,
        lambda _: httpx2.Response(500),
        certificate_lifetime_seconds=lifetime,
    )
    binding = provider.prepare_request(
        NODE_ID,
        _csr(),
        NOW,
        purpose=CertificateIssuancePurpose.ENROLLMENT,
        source_serial=None,
        generation=1,
    )
    assert (
        datetime.fromisoformat(binding.not_after)
        - datetime.fromisoformat(binding.not_before)
    ).total_seconds() == lifetime


@pytest.mark.parametrize("fault", ("transport", "health", "json", "server"))
def test_provider_unavailability_is_typed_unknown_and_recovers(tmp_path, fault):
    """Catches transport damage being denied or poisoning later observation."""
    damaged = True
    requests = 0

    def handler(request):
        nonlocal requests
        requests += 1
        if not damaged:
            return httpx2.Response(200, json={"status": "ok"})
        if fault == "transport":
            raise httpx2.ConnectError("unavailable", request=request)
        if fault == "json":
            return httpx2.Response(200, content=b"broken")
        if fault == "server":
            return httpx2.Response(503)
        return httpx2.Response(200, json={"status": "unavailable"})

    provider, _ = _provider(tmp_path, handler)
    provider.check_health()
    assert requests == 4
    damaged = False
    assert provider.check_health() is None
    assert requests == 5


def test_accepted_content_replays_after_compatible_local_policy_change(tmp_path):
    """Catches current lifetime configuration preventing exact old-content adoption."""
    stored = []
    holder = {}
    requests = []

    def handler(request):
        requests.append(request)
        if stored:
            return httpx2.Response(200, json=stored[0].model_dump(mode="json"))
        reply = _success_response(request, holder["material"], [])
        stored.append(CertificateIssuedReply.model_validate_json(reply.content))
        return reply

    first, material = _provider(tmp_path, handler)
    holder["material"] = material
    request = _csr()
    binding = first.prepare_request(
        NODE_ID,
        request,
        NOW,
        purpose=CertificateIssuancePurpose.ENROLLMENT,
        source_serial=None,
        generation=1,
    )
    issued = first.issue_node(NODE_ID, request, NOW, request=binding)
    repaired = StepCertificateAuthority(
        ca_url=CA_URL,
        root_certificate_path=material["root_path"],
        intermediate_certificate_path=material["intermediate_path"],
        provisioner_name="vonk-forge-agent",
        provisioner_kid=material["kid"],
        credential_path=material["credential_path"],
        provisioner_public_jwk_path=material["public_jwk_path"],
        certificate_lifetime_seconds=2 * 365 * 86400,
        transport=httpx2.MockTransport(handler),
    )
    replay = repaired.observe_node(request, NOW, request=binding)
    assert replay == issued
    assert len(requests) == 2
    assert (
        json.loads(requests[-1].content)["request"]
        == json.loads(requests[0].content)["request"]
    )
    first.close()
    repaired.close()


@pytest.mark.parametrize("damage", ["json", "pem", "crl"])
def test_peer_reply_unknown_is_bounded_and_fresh_authority_observation_succeeds(
    tmp_path, damage
):
    # Wrong implementation: unreadable peer bytes become an irrevocable refusal,
    # or malformed certificates/CRLs are adopted as successful effects.
    holder = {}
    calls = []
    seen = []
    repaired = False

    def handler(request):
        calls.append(request)
        if damage == "crl":
            if not repaired:
                return httpx2.Response(200, content=b"unreadable crl")
            return _crl_response(
                holder["material"],
                last_update=NOW,
                next_update=NOW + timedelta(minutes=59),
            )
        response = _success_response(request, holder["material"], seen)
        if not repaired and damage == "json":
            return httpx2.Response(201, content=b"{")
        if not repaired:
            document = response.json()
            document["crt"] = "unreadable pem"
            document["certChain"][0] = document["crt"]
            return httpx2.Response(201, json=document)
        return response

    provider, material = _provider(tmp_path, handler)
    holder["material"] = material
    request_pem = _csr()
    binding = provider.prepare_request(
        NODE_ID,
        request_pem,
        NOW,
        purpose=CertificateIssuancePurpose.ENROLLMENT,
        source_serial=None,
        generation=1,
    )
    if damage == "crl":
        assert not isinstance(provider.revocation_bundle(NOW), bytes)
    else:
        adopted = []
        for _attempt in range(4):
            try:
                adopted.append(provider.observe_node(request_pem, NOW, request=binding))
            except Exception:  # noqa: BLE001, S110 -- no unverified effect and repaired exact observation decide
                pass
        assert not adopted
    assert len(calls) == 4
    repaired = True
    if damage == "crl":
        assert isinstance(provider.revocation_bundle(NOW), bytes)
    else:
        issued = provider.observe_node(request_pem, NOW, request=binding)
        assert issued is not None and issued.serial == binding.serial
        assert _issue(provider, NODE_ID, _csr(), NOW).node_id == NODE_ID
