"""Shared helpers for the agent_api tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.types import Message

from vonk_agent_protocol.compiled_execution_plan import (
    CompiledExecutionPlan as AgentCompiledExecutionPlan,
)
from vonk_agent_protocol.contracts import canonical_message
from vonk_agent_protocol.recipe_operations import RecipeStopPayload
from vonk_control.agent_api.common import (
    AgentApiServices,
)
from vonk_control.agent_jobs.service import AgentJobService
from vonk_control.api.application import create_app
from vonk_control.auth import Actor, TokenCodec
from vonk_control.ca_issuance_contract import CertificateIssuanceBinding
from vonk_control.enrollment.bootstrap import EnrollmentBootstrapConfig
from vonk_control.enrollment.contract import EnrollmentGrant
from vonk_control.enrollment.service import EnrollmentService
from vonk_control.model_primitives import Base
from vonk_control.models.fleet import AgentCertificate, AgentNode
from vonk_control.models.jobs import Job
from vonk_control.pki import IssuedCertificate
from vonk_control.presence import AgentPresenceService, ManagementAddressPolicy
from vonk_control.source_bundles import SourceBundleStore

from .ca_test_authority import FixtureCertificateAuthority
from .package_upgrade_fixtures import source_transport

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


def telemetry_payload(
    clock: Clock,
    *,
    observed_at: datetime | None = None,
    boot_id: str = "00000000-0000-4000-8000-000000000001",
) -> dict[str, object]:
    return {
        "samples": [
            {
                "boot_id": str(boot_id),
                "observed_at": (observed_at or clock.now).isoformat(),
                "memory_total_bytes": 128_000_000_000,
                "memory_available_bytes": 64_000_000_000,
                "disk_total_bytes": 1_000_000_000_000,
                "disk_free_bytes": 750_000_000_000,
                "gpu_utilization_percent": 25.0,
                "gpu_memory_total_bytes": 128_000_000_000,
                "gpu_memory_free_bytes": 63_000_000_000,
            }
        ],
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


def assert_corrected_enrollment_succeeds(
    services: AgentApiServices, token: str
) -> None:
    submitted = json.loads(valid_enrollment_body(token))
    assert services.enrollment is not None
    issued = services.enrollment.submit(
        token, submitted["csr"].encode("ascii"), submitted["evidence"]
    )
    assert isinstance(issued, IssuedCertificate)
    assert issued.node_id == NODE_C
    assert issued.certificate_pem


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


def asgi_post(
    app, path: str, body: bytes, *, content_type: str = "application/json"
) -> tuple[int, bytes]:
    async def request() -> tuple[int, bytes]:
        sent: list[Message] = []
        delivered = False

        async def receive() -> dict[str, object]:
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: Message) -> None:
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": ((b"content-type", content_type.encode("ascii")),),
            "client": ("testclient", 1234),
            "server": ("testserver", 80),
            "root_path": "",
            "state": {},
        }
        # A hang guard, not a latency assertion: a loaded four-worker CI shard
        # can take over a second for the synchronous grant consumption.
        await asyncio.wait_for(app(scope, receive, send), timeout=30)
        start = next(
            message for message in sent if message["type"] == "http.response.start"
        )
        content = b"".join(
            message.get("body", b"")  # type: ignore[arg-type]
            for message in sent
            if message["type"] == "http.response.body"
        )
        return int(start["status"]), content

    return asyncio.run(request())


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
