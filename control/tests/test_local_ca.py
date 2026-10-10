"""Local CA producer/store/consumer tests on real PostgreSQL, without sleeps."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import CertificateCode
from vonk_agent_protocol.state_machines import CertificateIssuancePurpose
from vonk_control.local_ca import LocalCertificateAuthority
from vonk_control.models import AgentCertificate
from vonk_control.models.fleet import LocalCertificateRevocation
from vonk_control.step_ca import (
    StepCAError,
    StepCAIssuancePending,
    _validate_crl_freshness,
)

from .test_step_ca import NODE_ID, NOW, _csr, _leaf, _write_material


@pytest.fixture
def local_ca(tmp_path: Path, postgres_engine: Engine):
    # Source admission also checks pre-existing Controller-issued certificates.
    from vonk_control.models import Base

    Base.metadata.create_all(postgres_engine)
    material = _write_material(tmp_path)
    key_path = tmp_path / "step-ca-intermediate-key"
    key_path.write_bytes(
        material["intermediate_key"].private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"test-ca-password"),
        )
    )
    password_path = tmp_path / "step-ca-password"
    password_path.write_bytes(b"test-ca-password\n")
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    options = {
        "sessions": sessions,
        "root_certificate_path": material["root_path"],
        "intermediate_certificate_path": material["intermediate_path"],
        "intermediate_key_path": key_path,
        "password_path": password_path,
        "provisioner_name": "vonk-forge-agent",
        "provisioner_kid": material["kid"],
    }
    return LocalCertificateAuthority(**options), material, options


def _binding(ca, csr, *, source=None, generation=1):
    return ca.prepare_request(
        NODE_ID,
        csr,
        NOW,
        purpose=CertificateIssuancePurpose.ROTATION
        if source
        else CertificateIssuancePurpose.ENROLLMENT,
        source_serial=source,
        generation=generation,
    )


def test_production_factory_reads_compose_mounted_signing_secrets(tmp_path):
    """Catches using bundle source paths instead of flat Compose secret targets."""
    from vonk_control.agent_services import build_enrollment_service
    from vonk_control.enrollment import EnrollmentService
    from vonk_control.settings import Settings

    material = _write_material(tmp_path / "material")
    secrets = tmp_path / "run-secrets"
    secrets.mkdir()
    for source, name in (
        (material["root_path"], "step-ca-root-certificate"),
        (material["intermediate_path"], "agent-intermediate-certificate"),
        (material["public_jwk_path"], "agent-ca-provisioner-public-jwk"),
    ):
        (secrets / name).write_bytes(source.read_bytes())
    (secrets / "step-ca-intermediate-key").write_bytes(
        material["intermediate_key"].private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"test-ca-password"),
        )
    )
    (secrets / "step-ca-password").write_bytes(b"test-ca-password\n")
    # This portable check exercises real key/certificate loading. The adjacent
    # PostgreSQL test covers revocation import and enrollment through this factory.
    with patch.object(LocalCertificateAuthority, "_import_revocations"):
        service = build_enrollment_service(
            Settings(database_url="postgresql+psycopg://unused", secrets_root=secrets),
            sessionmaker(),
            lambda: NOW,
        )
    assert isinstance(service, EnrollmentService)


def test_issue_is_equivalent_to_step_policy_and_replays_after_restart(local_ca):
    """Catches wrong issuer, changed client profile, and in-memory-only receipts."""
    ca, material, options = local_ca
    csr = _csr()
    binding = _binding(ca, csr)
    assert ca.observe_node(csr, NOW, request=binding) is None
    issued = ca.issue_node(NODE_ID, csr, NOW, request=binding)
    leaf = x509.load_pem_x509_certificate(issued.certificate_pem)
    leaf.verify_directly_issued_by(material["intermediate"])
    material["intermediate"].verify_directly_issued_by(material["root"])
    reference = _leaf(csr, material, serial=int(binding.serial))
    assert leaf.subject == reference.subject
    assert list(leaf.extensions) == list(reference.extensions)
    assert leaf.not_valid_before_utc == reference.not_valid_before_utc
    assert leaf.not_valid_after_utc == reference.not_valid_after_utc
    assert leaf.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    ) == reference.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    restarted = LocalCertificateAuthority(**options)
    assert restarted.issue_node(NODE_ID, csr, NOW, request=binding) == issued
    assert (
        restarted.observe_node(csr, NOW + timedelta(days=1), request=binding) == issued
    )


def test_conflicting_binding_serial_and_policy_fail_closed(local_ca):
    """Catches journal overwrites, serial reuse, and ignored exact-policy binding."""
    ca, _, _ = local_ca
    csr = _csr()
    binding = _binding(ca, csr)
    ca.issue_node(NODE_ID, csr, NOW, request=binding)
    conflicts = [
        (
            binding.model_copy(update={"generation": 2}),
            CertificateCode.REQUEST_BINDING_MISMATCH,
        ),
        (
            _binding(ca, csr).model_copy(update={"serial": binding.serial}),
            CertificateCode.SERIAL_ALREADY_RESERVED,
        ),
        (
            binding.model_copy(update={"policy_sha256": "0" * 64}),
            CertificateCode.REQUEST_INVALID,
        ),
        (
            binding.model_copy(update={"not_after": "2026-09-05T12:00:00Z"}),
            CertificateCode.REQUEST_INVALID,
        ),
    ]
    for conflicting, reason in conflicts:
        with pytest.raises(StepCAError) as error:
            ca.issue_node(NODE_ID, csr, NOW, request=conflicting)
        assert error.value.reason_code == reason
    with pytest.raises(StepCAError):
        ca.issue_node(NODE_ID, _csr(), NOW, request=binding)


def test_renew_revoke_and_signed_crl(local_ca):
    """Catches hidden replacements after source revocation and unsigned CRLs."""
    ca, material, options = local_ca
    csr = _csr()
    source = ca.issue_node(NODE_ID, csr, NOW, request=_binding(ca, csr))
    replacement_csr = _csr()
    binding = _binding(ca, replacement_csr, source=source.serial, generation=2)
    replacement = ca.renew_node(NODE_ID, replacement_csr, NOW, request=binding)
    assert replacement.serial != source.serial
    assert replacement.generation == 2
    ca.revoke_node(source.serial, NOW)
    ca.revoke_node(source.serial, NOW + timedelta(minutes=1))
    restarted = LocalCertificateAuthority(**options)
    assert restarted.observe_node(replacement_csr, NOW, request=binding) == replacement
    bundle = restarted.revocation_bundle(NOW)
    assert isinstance(bundle, bytes)
    crl = x509.load_pem_x509_crl(bundle)
    assert crl.is_signature_valid(material["intermediate"].public_key())
    assert crl.issuer == material["intermediate"].subject
    _validate_crl_freshness(crl, NOW, timedelta(seconds=30))
    entry = crl.get_revoked_certificate_by_serial_number(int(source.serial))
    assert entry is not None and entry.revocation_date_utc == NOW
    assert crl.get_revoked_certificate_by_serial_number(int(replacement.serial)) is None
    fresh = _csr()
    with pytest.raises(StepCAError) as error:
        restarted.renew_node(
            NODE_ID,
            fresh,
            NOW,
            request=_binding(ca, fresh, source=source.serial, generation=3),
        )
    assert error.value.reason_code == CertificateCode.SOURCE_REVOKED
    ca.revoke_node(replacement.serial, NOW)
    with pytest.raises(StepCAError) as error:
        ca.observe_node(replacement_csr, NOW, request=binding)
    assert error.value.reason_code == CertificateCode.ISSUANCE_REVOKED


def test_pending_survives_failed_signer_and_replays_concurrently(local_ca):
    """Catches crash-poisoned PENDING rows and two different concurrent effects."""
    ca, _, options = local_ca
    csr = _csr()
    binding = _binding(ca, csr)
    # The child dies after the real claim commit, before returning a signature.
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json, os, sys
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.ca_issuance_contract import CertificateIssuanceBinding
from vonk_control.local_ca import LocalCertificateAuthority
body = json.load(sys.stdin)
ca = LocalCertificateAuthority(sessions=sessionmaker(create_engine(body['url'])), **body['options'])
ca._certificate = lambda *args: os._exit(73)
ca.issue_node(body['node'], body['csr'].encode(), datetime.fromisoformat(body['now']),
    request=CertificateIssuanceBinding.model_validate_json(json.dumps(body['binding'])))
""",
        ],
        input=json.dumps(
            {
                "url": options["sessions"]
                .kw["bind"]
                .url.render_as_string(hide_password=False),
                "options": {
                    key: str(value)
                    for key, value in options.items()
                    if key != "sessions"
                },
                "node": NODE_ID,
                "csr": csr.decode("ascii"),
                "now": NOW.isoformat(),
                "binding": binding.model_dump(mode="json"),
            }
        ),
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert child.returncode == 73, child.stderr
    restarted = LocalCertificateAuthority(**options)
    with pytest.raises(StepCAIssuancePending):
        restarted.observe_node(csr, NOW, request=binding)
    entered, release = Event(), Event()
    sign = ca._certificate

    def paused_sign(*args):
        entered.set()
        assert release.wait(5)
        return sign(*args)

    with (
        ThreadPoolExecutor(max_workers=1) as executor,
        patch.object(ca, "_certificate", side_effect=paused_sign),
    ):
        first = executor.submit(ca.issue_node, NODE_ID, csr, NOW, request=binding)
        try:
            assert entered.wait(5)
            issued = restarted.issue_node(NODE_ID, csr, NOW, request=binding)
        finally:
            release.set()
        assert first.result(timeout=5) == issued


def test_revocation_fences_a_pending_signature(local_ca):
    """Catches publishing an issued effect after its exact serial was revoked."""
    ca, _, options = local_ca
    csr = _csr()
    binding = _binding(ca, csr)
    sign = ca._certificate

    def revoke_during_sign(*args):
        LocalCertificateAuthority(**options).revoke_node(binding.serial, NOW)
        return sign(*args)

    with (
        patch.object(ca, "_certificate", side_effect=revoke_during_sign),
        pytest.raises(StepCAError) as error,
    ):
        ca.issue_node(NODE_ID, csr, NOW, request=binding)
    assert error.value.reason_code == CertificateCode.ISSUANCE_REVOKED


def test_preexisting_certificate_can_rotate(local_ca):
    """Catches requiring every rotation source to have been issued locally."""
    ca, material, options = local_ca
    from vonk_control.models import AgentNode

    csr = _csr()
    leaf = _leaf(csr, material, serial=1234)
    with options["sessions"].begin() as session:
        session.add(AgentNode(node_id=NODE_ID, state="active"))
        session.flush()
        session.add(
            AgentCertificate(
                serial="1234",
                node_id=NODE_ID,
                not_before=leaf.not_valid_before_utc,
                not_after=leaf.not_valid_after_utc,
                fingerprint=leaf.fingerprint(hashes.SHA256()).hex(),
                certificate_pem=leaf.public_bytes(serialization.Encoding.PEM).decode(
                    "ascii"
                ),
                generation=1,
            )
        )
    fresh = _csr()
    assert (
        ca.renew_node(
            NODE_ID,
            fresh,
            NOW,
            request=_binding(ca, fresh, source="1234", generation=2),
        ).generation
        == 2
    )
    from vonk_control.telemetry_maintenance import TelemetryMaintenance

    ca.revoke_node("1234", NOW)
    expiry = leaf.not_valid_after_utc
    TelemetryMaintenance(options["sessions"], clock=lambda: expiry).run_once()
    with options["sessions"]() as session:
        assert session.get(LocalCertificateRevocation, "1234") is not None
    TelemetryMaintenance(
        options["sessions"], clock=lambda: expiry + timedelta(seconds=1)
    ).run_once()
    with options["sessions"]() as session:
        assert session.get(LocalCertificateRevocation, "1234") is None


def test_source_revocation_fences_pending_rotation(local_ca):
    """Catches a source revoked between admission and durable publication."""
    ca, _, options = local_ca
    csr = _csr()
    source = ca.issue_node(NODE_ID, csr, NOW, request=_binding(ca, csr))
    fresh = _csr()
    binding = _binding(ca, fresh, source=source.serial, generation=2)
    sign = ca._certificate

    def revoke_source_during_sign(*args):
        LocalCertificateAuthority(**options).revoke_node(source.serial, NOW)
        return sign(*args)

    with (
        patch.object(ca, "_certificate", side_effect=revoke_source_during_sign),
        pytest.raises(StepCAError) as error,
    ):
        ca.renew_node(NODE_ID, fresh, NOW, request=binding)
    assert error.value.reason_code == CertificateCode.ROTATION_SOURCE_REVOKED


def test_intermediate_key_identity_is_checked(local_ca, tmp_path: Path):
    """Catches signing with a valid encrypted key belonging to a different CA."""
    from cryptography.hazmat.primitives.asymmetric import ed25519

    _, _, options = local_ca
    wrong_key = tmp_path / "other-intermediate-key"
    wrong_key.write_bytes(
        ed25519.Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"test-ca-password"),
        )
    )
    with pytest.raises(ValueError, match="does not match"):
        LocalCertificateAuthority(**{**options, "intermediate_key_path": wrong_key})


def test_issue_refuses_foreign_node_identity(local_ca):
    """Catches signing a valid CSR for an identity outside its accepted binding."""
    ca, _, _ = local_ca
    csr = _csr()
    binding = _binding(ca, csr)
    with pytest.raises(ValueError, match="node identity does not match"):
        ca.issue_node("spk_" + "b" * 32, csr, NOW, request=binding)
    assert ca.observe_node(csr, NOW, request=binding) is None


def test_local_journal_retention_preserves_recovery_and_live_revocations(local_ca):
    """Catches early receipt/CRL deletion, unbounded pruning and immortal PENDING."""
    from sqlalchemy import select
    from vonk_control.models.fleet import (
        LocalCertificateIssuance,
    )
    from vonk_control.telemetry_maintenance import TelemetryMaintenance

    ca, _, options = local_ca
    sessions = options["sessions"]
    csr = _csr()
    bindings = [
        ca.prepare_request(
            NODE_ID,
            csr,
            NOW + timedelta(seconds=offset),
            purpose=CertificateIssuancePurpose.ENROLLMENT,
            source_serial=None,
            generation=1,
        )
        for offset in (-1, 0, 1)
    ]
    for binding in bindings[:2]:
        ca.issue_node(NODE_ID, csr, NOW, request=binding)
    with (
        patch.object(ca, "_certificate", side_effect=RuntimeError("signer stopped")),
        pytest.raises(RuntimeError, match="signer stopped"),
    ):
        ca.issue_node(NODE_ID, csr, NOW, request=bindings[2])
    for binding in bindings:
        ca.revoke_node(binding.serial, NOW)
    ca.revoke_node("1234", NOW)  # Unknown expiry must never prove safe deletion.

    clock = NOW + timedelta(days=30)
    maintenance = TelemetryMaintenance(sessions, clock=lambda: clock)
    maintenance.run_once(delete_limit=1)
    with sessions() as session:
        assert session.get(LocalCertificateRevocation, bindings[0].serial) is None
        assert session.get(LocalCertificateRevocation, bindings[1].serial) is not None
        assert session.get(LocalCertificateRevocation, bindings[2].serial) is not None
        assert session.get(LocalCertificateIssuance, bindings[0].request_id) is not None
    bundle = ca.revocation_bundle(clock)
    assert isinstance(bundle, bytes)
    crl = x509.load_pem_x509_crl(bundle)
    assert (
        crl.get_revoked_certificate_by_serial_number(int(bindings[1].serial))
        is not None
    )

    clock = NOW + timedelta(days=60)
    maintenance.run_once(delete_limit=1)
    with sessions() as session:
        assert session.get(LocalCertificateIssuance, bindings[0].request_id) is None
        assert session.get(LocalCertificateIssuance, bindings[1].request_id) is not None
        assert session.get(LocalCertificateIssuance, bindings[2].request_id) is not None

    clock += timedelta(seconds=2)
    maintenance.run_once(delete_limit=1)
    with sessions() as session:
        assert session.get(LocalCertificateIssuance, bindings[1].request_id) is None
        assert session.get(LocalCertificateIssuance, bindings[2].request_id) is not None
    maintenance.run_once(delete_limit=1)
    maintenance.run_once(delete_limit=1)
    with sessions() as session:
        assert session.scalars(select(LocalCertificateIssuance.request_id)).all() == []
        assert session.scalars(select(LocalCertificateRevocation.serial)).all() == [
            "1234"
        ]


def test_production_factory_enrolls_renews_and_keeps_cutover_revocations(
    local_ca, tmp_path
):
    """Catches old HTTP wiring and losing pre-cutover or uncertain revocations."""
    from vonk_control.agent_services import build_enrollment_service
    from vonk_control.enrollment_contract import EnrollmentGrant
    from vonk_control.models import (
        AgentIssuedCertificateRevocation,
        AgentNode,
    )
    from vonk_control.pki import IssuedCertificate
    from vonk_control.settings import Settings

    from .test_enrollment import OTHER_NODE_ID, evidence

    _ca, material, options = local_ca
    secrets = tmp_path / "production-secrets"
    secrets.mkdir()
    for source, destination in (
        (material["root_path"], secrets / "step-ca-root-certificate"),
        (material["intermediate_path"], secrets / "agent-intermediate-certificate"),
        (options["intermediate_key_path"], secrets / "step-ca-intermediate-key"),
        (options["password_path"], secrets / "step-ca-password"),
        (material["public_jwk_path"], secrets / "agent-ca-provisioner-public-jwk"),
    ):
        destination.write_bytes(Path(source).read_bytes())
    settings = Settings(
        database_url="postgresql+psycopg://unused", secrets_root=secrets
    )
    service = build_enrollment_service(settings, options["sessions"], lambda: NOW)
    csr = _csr()
    grant = service.create(NODE_ID, "admin", 600)
    assert isinstance(grant, EnrollmentGrant)
    issued = service.submit(grant.token, csr, evidence(csr))
    assert isinstance(issued, IssuedCertificate)
    renewed = service.renew(NODE_ID, issued.serial, _csr())
    assert isinstance(renewed, IssuedCertificate)
    service.activate(NODE_ID, renewed.serial, renewed.generation)
    # Simulate the pre-cutover Controller records, not local signer revocation.
    with options["sessions"].begin() as session:
        from sqlalchemy import delete

        session.execute(delete(LocalCertificateRevocation))
        certificate = session.get(AgentCertificate, issued.serial)
        assert certificate is not None
        certificate.revoked_at = NOW
        certificate.ca_revoked_at = NOW
        # A certificate issued by the old CA has no local issuance journal row.
        old_leaf = _leaf(_csr(OTHER_NODE_ID), material, serial=5678)
        session.add(AgentNode(node_id=OTHER_NODE_ID, state="retired"))
        session.flush()
        session.add(
            AgentCertificate(
                serial="5678",
                node_id=OTHER_NODE_ID,
                not_before=old_leaf.not_valid_before_utc,
                not_after=old_leaf.not_valid_after_utc,
                fingerprint=old_leaf.fingerprint(hashes.SHA256()).hex(),
                certificate_pem=old_leaf.public_bytes(
                    serialization.Encoding.PEM
                ).decode("ascii"),
                generation=1,
                ca_revoked_at=NOW,
            )
        )
        session.add(
            AgentIssuedCertificateRevocation(
                serial="1234",
                node_id=NODE_ID,
                provider_request_id="b" * 64,
                fingerprint=None,
                generation=1,
                state="revocation_pending",
                created_at=NOW,
                updated_at=NOW,
            )
        )
    for _ in range(2):
        restarted = LocalCertificateAuthority(**options)
        bundle = restarted.revocation_bundle(NOW)
        assert isinstance(bundle, bytes)
        crl = x509.load_pem_x509_crl(bundle)
        assert crl.is_signature_valid(material["intermediate"].public_key())
        assert (
            crl.get_revoked_certificate_by_serial_number(int(issued.serial)) is not None
        )
        assert crl.get_revoked_certificate_by_serial_number(1234) is not None
        assert crl.get_revoked_certificate_by_serial_number(5678) is not None
        assert crl.get_revoked_certificate_by_serial_number(int(renewed.serial)) is None
