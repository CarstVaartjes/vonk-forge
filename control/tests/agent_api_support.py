from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import CompiledExecutionPlan as AgentCompiledExecutionPlan
from vonk_agent_protocol import (
    RecipeStopPayload,
    canonical_message,
)
from vonk_control.agent_api import (
    AgentApiServices,
)
from vonk_control.agent_jobs import AgentJobService
from vonk_control.api import create_app
from vonk_control.auth import Actor, TokenCodec
from vonk_control.ca_issuance_contract import CertificateIssuanceBinding
from vonk_control.enrollment import EnrollmentService
from vonk_control.enrollment.bootstrap import EnrollmentBootstrapConfig
from vonk_control.enrollment.contract import EnrollmentGrant
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    Base,
    Job,
)
from vonk_control.pki import IssuedCertificate
from vonk_control.presence import AgentPresenceService, ManagementAddressPolicy
from vonk_control.source_bundles import SourceBundleStore
from vonk_forge_contracts import RecipeDefinition, document_sha256

from .ca_test_authority import FixtureCertificateAuthority

NODE_A = "spk_" + "a" * 32


NODE_B = "spk_" + "b" * 32


NODE_C = "spk_" + "c" * 32


CAPABILITIES = sorted(
    [
        "agent.runtime.rust.v1",
        "runtime.vonk.v1",
        "recipe.image.pull.v1",
        "agent.upgrade.v1",
        "artifact.distribution.v1",
        "recipe.build.v1",
        "recipe.image.import.v1",
        "recipe.install",
        "recipe.start",
        "recipe.job.run.v1",
        "recipe.stop",
        "recipe.uninstall",
    ]
)


_STOP_PLAN = AgentCompiledExecutionPlan.model_validate_json(
    (Path(__file__).parent / "fixtures/compiled_workload_v2.json").read_text(
        encoding="utf-8"
    )
)


STOP_PAYLOAD = RecipeStopPayload(
    run_id="00000000-0000-4000-8000-000000000001",
    target_runtime_id="00000000-0000-4000-8000-000000000001",
    run_generation=1,
    installation_id="00000000-0000-4000-8000-000000000002",
    recipe_revision_id="00000000-0000-4000-8000-000000000003",
    mapping_id="00000000-0000-4000-8000-000000000004",
    plan_digest="a" * 64,
    rank=_STOP_PLAN.runtime.placement.rank,
    role=_STOP_PLAN.runtime.placement.role,
    recipe_content_sha256=_STOP_PLAN.identity.recipe_revision_sha256,
    stop_timeout_seconds=_STOP_PLAN.lifecycle.stop_timeout_seconds,
    cancel_pending_start=True,
).model_dump(mode="json")


def upgrade_payload(digest: str, package_bytes: int) -> dict[str, object]:
    from .package_upgrade_fixtures import source_transport

    return {
        "schema_version": 1,
        "architecture": "linux-arm64",
        "package_bytes": package_bytes,
        "package_sha256": digest,
        "package_signature": "a" * 128,
        "package_url": "https://install.vonkforge.ai/releases/test/vonk-forge-agent.deb",
        "package_version": "1.2.3",
        "target_binary_digest": "b" * 64,
        "target_build_digest": "sha256:" + "c" * 64,
        **source_transport(),
    }


PACKAGED_RUNTIME_IDENTITY = {
    "architecture": "linux-amd64",
    "binary_digest": "c" * 64,
    "build_digest": "sha256:" + "b" * 64,
    "semantic_version": "1.2.3",
}


def _canonical_recipe_fixture(
    slug: str, *, source: str = "published"
) -> tuple[dict[str, object], str]:
    del source
    example = "recipe-source-build.json"
    raw = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", example)
        .read_text(encoding="utf-8")
    )
    raw["identity"]["slug"] = slug
    recipe = RecipeDefinition.model_validate(raw)
    document = recipe.model_dump(mode="json")
    return document, document_sha256(recipe.model_dump(mode="json"))


def _compiled_plan_fixture(
    recipe_digest: str, *, source: str = "published", build_id: str | None = None
) -> dict[str, object]:
    payload = json.loads(
        (Path(__file__).parent / "fixtures/compiled_workload_v2.json").read_text(
            encoding="utf-8"
        )
    )
    payload["identity"]["recipe_revision_sha256"] = recipe_digest
    runtime_image = payload["runtime_image"]
    runtime_image["source"] = source
    runtime_image["build_id"] = build_id
    if source == "controller-build":
        runtime_image["registry_manifest_digest"] = None
    return payload


def _controller_ca() -> tuple[str, str]:
    key = ed25519.Ed25519PrivateKey.generate()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "controller-ca")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime(2026, 8, 2, tzinfo=UTC))
        .not_valid_after(datetime(2027, 8, 3, tzinfo=UTC))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, algorithm=None)
    )
    pem = certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
    return pem, certificate.fingerprint(hashes.SHA256()).hex()


class Jobs:
    """Minimal current JobQueue stand-in for tests that exercise agent routes."""

    def enqueue(
        self,
        kind: str,
        actor: str,
        authority_revision: str,
        targets: Sequence[str],
        payload: Mapping[str, object],
        *,
        request_id: str,
    ) -> object:
        raise AssertionError

    def get(self, job_id: str) -> object:
        raise KeyError


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 3, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class Authority(FixtureCertificateAuthority):
    def __init__(self) -> None:
        self.fail_revoke = False

    def issue_node(
        self,
        node_id: str,
        public_key_pem: bytes,
        now: datetime,
        *,
        request: CertificateIssuanceBinding,
    ) -> IssuedCertificate:
        self._begin(request)
        csr_request = x509.load_pem_x509_csr(public_key_pem)
        key = ed25519.Ed25519PrivateKey.generate()
        leaf = (
            x509.CertificateBuilder()
            .subject_name(csr_request.subject)
            .issuer_name(csr_request.subject)
            .public_key(csr_request.public_key())
            .serial_number(int(request.serial))
            .not_valid_before(datetime.fromisoformat(request.not_before))
            .not_valid_after(datetime.fromisoformat(request.not_after))
            .sign(key, algorithm=None)
        )
        pem = leaf.public_bytes(serialization.Encoding.PEM)
        return self._finish(
            request,
            IssuedCertificate(
                node_id,
                pem,
                pem,
                request.serial,
                leaf.fingerprint(hashes.SHA256()).hex(),
                leaf.not_valid_before_utc,
                leaf.not_valid_after_utc,
                generation=request.generation,
            ),
        )

    def renew_node(
        self,
        node_id: str,
        public_key_pem: bytes,
        now: datetime,
        *,
        request: CertificateIssuanceBinding,
    ) -> IssuedCertificate:
        return self.issue_node(node_id, public_key_pem, now, request=request)

    def revocation_bundle(self, now: datetime) -> bytes:
        return b""

    def revoke_node(self, serial: str, now: datetime) -> None:
        if self.fail_revoke:
            raise RuntimeError("provider unavailable")


class CurrentAgentClient(TestClient):
    """Supply the current Rust claim envelope for terse authenticated tests."""

    def post(self, url, *args, headers=None, json=None, **kwargs):
        if (
            url == "/agent/claim"
            and headers is not None
            and headers.get("x-vonk-agent-verified") == "1"
        ):
            body = {
                "protocol_version": 4,
                "runtime_identity": PACKAGED_RUNTIME_IDENTITY,
                "wait_seconds": 0,
            }
            if isinstance(json, dict):
                body.update(json)
            json = body
        return super().post(url, *args, headers=headers, json=json, **kwargs)


@pytest.fixture
def agent_system(tmp_path):
    return make_agent_system(tmp_path)


def make_agent_system(tmp_path, *, engine=None, authority=None):
    if engine is None:
        engine = create_engine(
            f"sqlite:///{tmp_path / 'agent-api.sqlite'}",
            connect_args={"check_same_thread": False},
        )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    clock = Clock()
    with sessions.begin() as session:
        for node, serial in ((NODE_A, "serial-a"), (NODE_B, "serial-b")):
            session.add(
                AgentNode(
                    node_id=node,
                    state="active",
                    workload_intent_ordinal=1,
                )
            )
            session.flush()
            session.add(
                AgentCertificate(
                    serial=serial,
                    node_id=node,
                    fingerprint=f"fingerprint-{serial}",
                    not_before=clock.now - timedelta(seconds=1),
                    not_after=clock.now + timedelta(hours=1),
                )
            )
    presence = AgentPresenceService(
        sessions,
        ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=clock,
    )
    operations = AgentJobService(sessions, clock=clock)
    controller_ca_pem, controller_ca_fingerprint = _controller_ca()
    services = AgentApiServices(
        enrollment=EnrollmentService(sessions, authority or Authority(), clock=clock),
        operations=operations,
        sessions=sessions,
        clock=clock,
        presence=presence,
        artifact_root=tmp_path / "artifacts",
        # The edge mounts one fixed root; tests keep objects in temporary directories.
        served_root=Path("/"),
        source_bundles=SourceBundleStore(tmp_path / "source-bundles"),
        fabric_policy=ManagementAddressPolicy.parse("192.168.100.0/24"),
        bootstrap=EnrollmentBootstrapConfig(
            controller_endpoint="https://agents.example.test:8443",
            enrollment_endpoint="https://enroll.example.test:8443",
            ca_fingerprint=controller_ca_fingerprint,
            ca_pem=controller_ca_pem,
            controller_address="192.168.1.231",
            service_hostnames=(
                "control.example.test",
                "enroll.example.test",
                "agents.example.test",
            ),
        ),
    )
    services.artifact_root.mkdir()
    codec = TokenCodec(b"k" * 32)
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        now=lambda: 0,
        agent=services,
        trusted_agent_proxy_auth=b"p" * 32,
    )
    return CurrentAgentClient(app), services, codec, clock


def agent_headers(node: str, serial: str) -> dict[str, str]:
    return {
        "x-vonk-agent-node": node,
        "x-vonk-agent-serial": serial,
        "x-vonk-agent-fingerprint": f"fingerprint-{serial}",
        "x-vonk-agent-verified": "1",
        "x-vonk-agent-proxy-auth": "p" * 32,
        "x-vonk-agent-source": "10.0.0.42",
    }


def admin_headers(codec: TokenCodec, role: str = "administrator") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {codec.issue(Actor(role, role), ttl_seconds=100, now=0)}"
    }


def enrollment_grant(services: AgentApiServices) -> str:
    enrollment = services.enrollment
    assert enrollment is not None
    grant = enrollment.create(NODE_C, "administrator", 60)
    assert isinstance(grant, EnrollmentGrant)
    return grant.token


def valid_enrollment_body(token: str) -> bytes:
    key = ed25519.Ed25519PrivateKey.generate()
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, NODE_C)]))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(
                        f"spiffe://vonk-forge.local/node/{NODE_C}"
                    )
                ]
            ),
            critical=False,
        )
        .sign(key, algorithm=None)
        .public_bytes(serialization.Encoding.PEM)
    )
    public = (
        x509.load_pem_x509_csr(csr)
        .public_key()
        .public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return json.dumps(
        {
            "grant_token": token,
            "csr": csr.decode("ascii"),
            "evidence": {
                "node_id": NODE_C,
                "csr_public_key_fingerprint": hashlib.sha256(public).hexdigest(),
                "host_key_fingerprint": "host",
                "hardware_fingerprint": "hardware",
                "agent_digest": "a" * 64,
                "boot_id": "boot",
            },
        }
    ).encode("utf-8")


def parent(sessions, clock: Clock) -> Job:
    payload = {"workload_intent_ordinal": 1}
    job = Job(
        request_id=str(uuid.uuid4()),
        kind="agent.operations",
        state="queued",
        actor="administrator",
        authority_revision="a" * 64,
        targets=[NODE_A],
        payload_digest=hashlib.sha256(canonical_message(payload)).hexdigest(),
        payload=payload,
        current_attempt=0,
        created_at=clock.now,
        updated_at=clock.now,
    )
    with sessions.begin() as session:
        session.add(job)
    return job


def _csr_for(node_id: str) -> bytes:
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


def _damage_storage_entry(path, outside, damage):
    if damage == "symlink":
        path.symlink_to(outside)
    elif damage == "directory":
        path.mkdir()
        (path / "damaged").write_bytes(b"damaged")
    else:
        os.mkfifo(path)
