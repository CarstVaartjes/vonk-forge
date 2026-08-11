from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import canonical_message
from vonk_control.agent_api import (
    AgentApiServices,
    EnrollmentRateLimiter,
    _bounded_enrollment_body,
    _read_chunks,
    _sealed_snapshot,
)
from vonk_control.agent_jobs import AgentJobService
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.enrollment import EnrollmentDenied, EnrollmentService
from vonk_control.metrics import MetricsRegistry, OperationalMetricsCollector
from vonk_control.models import (
    AgentCertificate,
    AgentCertificateRotation,
    AgentEnrollment,
    AgentEnrollmentGrant,
    AgentNode,
    AgentOperationAttempt,
    AgentPresence,
    Base,
    ClusterMapping,
    ClusterMappingNode,
    InstallationNode,
    Job,
    LocalRecipe,
    LocalRecipeRevision,
    NodeInventorySnapshot,
    Observation,
    RecipeBuild,
    RecipeInstallation,
    RecipeSourceBundle,
)
from vonk_control.pki import CertificateAuthority, IssuedCertificate
from vonk_control.presence import AgentPresenceService, ManagementAddressPolicy
from vonk_control.recipe_contract import recipe_content_sha256
from vonk_control.source_bundles import SourceBundleStore, generate_source_bundle

NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32
NODE_C = "spk_" + "c" * 32
CAPABILITIES = [
    "node.probe",
    "release.install",
    "workload.health",
    "workload.prepare",
    "workload.start",
    "workload.stop",
    "workload.verify",
]
PROBE_RESULT = {
    "status": "ok",
    "evidence": {
        "vonk_forge": {
            "schema_version": 1,
            "memory": {"available_bytes": 1_000, "total_bytes": 4_000},
            "storage": {"available_bytes": 2_000, "total_bytes": 8_000},
            "accelerator": {
                "available": True,
                "active_nvidia_compute_processes": 0,
            },
        },
        "nvidia": {"tools": {}},
    },
}


class Jobs:
    def list(self):
        return []

    def get(self, _):
        raise KeyError

    def enqueue(self, *_args, **_kwargs):
        raise AssertionError


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 3, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class ChunkedEnrollmentRequest:
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks
        self.received = 0

    async def stream(self):
        for chunk in self.chunks:
            self.received += 1
            yield chunk


class CopyBoundedChunk(bytes):
    def __new__(cls, value: bytes):
        instance = super().__new__(cls, value)
        instance.largest_slice = 0
        return instance

    def __getitem__(self, key):
        if isinstance(key, slice):
            start = key.start or 0
            stop = len(self) if key.stop is None else key.stop
            self.largest_slice = max(self.largest_slice, max(0, stop - start))
        return super().__getitem__(key)

    def __radd__(self, _other):
        raise AssertionError("an incoming ASGI chunk must never be concatenated whole")


class Authority(CertificateAuthority):
    def __init__(self) -> None:
        self.fail_revoke = False

    def issue_node(
        self, node_id: str, public_key_pem: bytes, now: datetime
    ) -> IssuedCertificate:
        return IssuedCertificate(
            node_id,
            b"certificate",
            b"chain",
            "issued-serial",
            "issued-fingerprint",
            now,
            now + timedelta(days=1),
        )

    def renew_node(
        self,
        node_id: str,
        public_key_pem: bytes,
        now: datetime,
        *,
        request_id: str,
    ) -> IssuedCertificate:
        return self.issue_node(node_id, public_key_pem, now)

    def revocation_bundle(self, now: datetime) -> bytes:
        return b""

    def revoke_node(self, serial: str, now: datetime) -> None:
        if self.fail_revoke:
            raise RuntimeError("provider unavailable")


@pytest.fixture
def agent_system(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'agent-api.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    clock = Clock()
    with sessions.begin() as session:
        for node, serial in ((NODE_A, "serial-a"), (NODE_B, "serial-b")):
            session.add(AgentNode(node_id=node, state="active", capabilities=[]))
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
    operations.set_contact_consumer(presence.observe_in_session)
    services = AgentApiServices(
        enrollment=EnrollmentService(sessions, Authority(), clock=clock),
        operations=operations,
        sessions=sessions,
        clock=clock,
        presence=presence,
        artifact_root=tmp_path / "artifacts",
        source_bundles=SourceBundleStore(tmp_path / "source-bundles"),
        tuf_metadata_root=tmp_path / "tuf-metadata",
        tuf_target_root=tmp_path / "tuf-targets",
        workload_tuf_metadata_root=tmp_path / "workload-tuf-metadata",
        workload_tuf_target_root=tmp_path / "workload-tuf-targets",
        max_tuf_metadata_bytes=128,
        max_tuf_target_bytes=128,
        fabric_policy=ManagementAddressPolicy.parse("192.168.100.0/24"),
    )
    services.artifact_root.mkdir()
    services.tuf_metadata_root.mkdir()
    services.tuf_target_root.mkdir()
    services.workload_tuf_metadata_root.mkdir()
    services.workload_tuf_target_root.mkdir()
    codec = TokenCodec(b"k" * 32)
    audits = MemoryAuditStore()
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        audits=audits,
        fleet=dict,
        now=lambda: 0,
        agent=services,
        trusted_agent_proxy_auth=b"p" * 32,
    )
    app.state.test_audits = audits
    return TestClient(app), services, codec, clock


def agent_headers(node: str, serial: str) -> dict[str, str]:
    return {
        "x-vonk-agent-node": node,
        "x-vonk-agent-serial": serial,
        "x-vonk-agent-fingerprint": f"fingerprint-{serial}",
        "x-vonk-agent-verified": "1",
        "x-vonk-agent-proxy-auth": "p" * 32,
        "x-vonk-agent-source": "10.0.0.42",
    }


def test_agent_posts_authenticated_runtime_and_fabric_inventory(agent_system) -> None:
    client, services, _, clock = agent_system
    payload = {
        "schema_version": 1,
        "observed_at": clock.now.isoformat(),
        "disk_total_bytes": 1000,
        "disk_free_bytes": 700,
        "host_memory_total_bytes": 2000,
        "host_memory_free_bytes": 1500,
        "gpu_memory_total_bytes": 1000,
        "gpu_memory_free_bytes": 800,
        "gpu_count": 1,
        "artifact_store_read_only": False,
        "capabilities": ["runtime.vonk.v1", "fabric.tcp.mbps.200000"],
        "fabric_address": "192.168.100.2",
        "fabric_bandwidth_mbps": 200000,
        "nvidia_driver_version": "580.65.06",
        "container_runtime_version": "28.3.3",
    }

    response = client.post(
        "/agent/v1/inventory",
        headers=agent_headers(NODE_A, "serial-a"),
        json=payload,
    )
    assert response.status_code == 204
    with services.sessions() as session:
        row = session.scalar(
            select(NodeInventorySnapshot).where(NodeInventorySnapshot.node_id == NODE_A)
        )
        assert row is not None
        assert row.fabric_address == "192.168.100.2"
        assert row.fabric_bandwidth_mbps == 200000
        assert row.capabilities == sorted(payload["capabilities"])

    denied = payload | {"fabric_address": "10.0.0.42"}
    assert (
        client.post(
            "/agent/v1/inventory",
            headers=agent_headers(NODE_B, "serial-b"),
            json=denied,
        ).status_code
        == 422
    )


def test_agent_posts_authenticated_complete_recipe_run_observation_snapshot(
    agent_system,
) -> None:
    client, _services, _, clock = agent_system
    payload = {
        "schema_version": 1,
        "observed_at": clock.now.isoformat(),
        "runs": [],
    }

    assert (
        client.post(
            "/agent/v1/recipe-runs/observations",
            headers=agent_headers(NODE_A, "serial-a"),
            json=payload,
        ).status_code
        == 204
    )
    assert (
        client.post("/agent/v1/recipe-runs/observations", json=payload).status_code
        == 401
    )
    assert (
        client.post(
            "/agent/v1/recipe-runs/observations",
            headers=agent_headers(NODE_A, "serial-a"),
            json=payload | {"runs": [{"run_id": "not-a-uuid", "ready": True}]},
        ).status_code
        == 422
    )


def test_builder_can_download_only_its_authorized_canonical_source_bundle(
    agent_system,
) -> None:
    client, services, _, clock = agent_system
    bundle = generate_source_bundle(
        {
            "Dockerfile": (
                f"FROM ghcr.io/vonkforge/base@sha256:{'a' * 64}\nUSER 10001:10001\n"
            ).encode()
        }
    )
    stored = services.source_bundles.put(bundle.sha256, io.BytesIO(bundle.archive))
    recipe_id = str(uuid.uuid4())
    revision_id = str(uuid.uuid4())
    with services.sessions.begin() as session:
        session.add(
            LocalRecipe(
                id=recipe_id,
                slug="bundle-download",
                title="Bundle download",
                description="Agent source authorization fixture",
                source_kind="local",
                created_by="administrator",
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
        session.add(
            LocalRecipeRevision(
                id=revision_id,
                recipe_id=recipe_id,
                revision_number=1,
                lifecycle="resolved",
                schema_version=1,
                document={},
                content_sha256="c" * 64,
                created_by="administrator",
                created_at=clock.now,
            )
        )
        session.add(
            RecipeSourceBundle(
                sha256=bundle.sha256,
                media_type="application/vnd.vonk-forge.source-bundle.v1+tar",
                archive_bytes=len(bundle.archive),
                total_bytes=bundle.manifest.total_bytes,
                file_count=len(bundle.manifest.files),
                storage_key=str(stored.path),
                manifest={"schema_version": 1},
                verified_at=clock.now,
            )
        )
        session.add(
            RecipeBuild(
                recipe_revision_id=revision_id,
                builder_node_id=NODE_A,
                source_bundle_sha256=bundle.sha256,
                build_input_sha256="d" * 64,
                state="planned",
                policy_report={"passed": True},
                plan={},
                created_at=clock.now,
                updated_at=clock.now,
            )
        )

    response = client.get(
        f"/agent/v1/source-bundles/{bundle.sha256}",
        headers=agent_headers(NODE_A, "serial-a"),
    )
    assert response.status_code == 200
    assert response.content == bundle.archive
    assert response.headers["etag"] == f'"sha256:{bundle.sha256}"'
    assert (
        client.get(
            f"/agent/v1/source-bundles/{bundle.sha256}",
            headers=agent_headers(NODE_B, "serial-b"),
        ).status_code
        == 404
    )
    assert client.get(f"/agent/v1/source-bundles/{bundle.sha256}").status_code == 401


def test_builder_uploads_digest_verified_oci_archive_without_a_registry(
    agent_system,
) -> None:
    client, services, _, clock = agent_system
    recipe_id = str(uuid.uuid4())
    revision_id = str(uuid.uuid4())
    build_id = str(uuid.uuid4())
    payload = b"exact oci layout"
    layout_digest = hashlib.sha256(payload).hexdigest()
    image_digest = "sha256:" + "d" * 64
    with services.sessions.begin() as session:
        session.add(
            LocalRecipe(
                id=recipe_id,
                slug="image-upload",
                title="Image upload",
                description="Exact OCI upload fixture",
                source_kind="local",
                created_by="administrator",
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
        session.add(
            LocalRecipeRevision(
                id=revision_id,
                recipe_id=recipe_id,
                revision_number=1,
                lifecycle="resolved",
                schema_version=1,
                document={},
                content_sha256="c" * 64,
                created_by="administrator",
                created_at=clock.now,
            )
        )
        session.add(
            RecipeBuild(
                id=build_id,
                recipe_revision_id=revision_id,
                builder_node_id=NODE_A,
                source_bundle_sha256="a" * 64,
                build_input_sha256="b" * 64,
                state="building",
                policy_report={"passed": True},
                plan={},
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
    headers = agent_headers(NODE_A, "serial-a") | {
        "content-type": "application/vnd.oci.image.layout.v1.tar",
        "x-vonk-image-digest": image_digest,
        "x-vonk-oci-layout-sha256": layout_digest,
    }

    response = client.put(
        f"/agent/v1/recipe-builds/{build_id}/image",
        headers=headers,
        content=payload,
    )

    assert response.status_code == 204
    assert (services.artifact_root / layout_digest).read_bytes() == payload
    with services.sessions() as session:
        build = session.get(RecipeBuild, build_id)
        assert build.image_digest == image_digest
        assert build.oci_layout_sha256 == layout_digest
        assert build.image_bytes == len(payload)
    assert (
        client.put(
            f"/agent/v1/recipe-builds/{build_id}/image",
            headers=agent_headers(NODE_B, "serial-b")
            | {
                "x-vonk-image-digest": image_digest,
                "x-vonk-oci-layout-sha256": layout_digest,
            },
            content=payload,
        ).status_code
        == 404
    )


def admin_headers(codec: TokenCodec, role: str = "administrator") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {codec.issue(Actor(role, role), ttl_seconds=100, now=0)}"
    }


def enrollment_grant(services: AgentApiServices) -> str:
    return services.enrollment.create(NODE_A, "administrator", 60).token


def assert_grant_consumed(services: AgentApiServices, token: str) -> None:
    with pytest.raises(EnrollmentDenied, match="consumed"):
        services.enrollment.submit(token, b"", {})


def valid_enrollment_body(token: str) -> bytes:
    key = ed25519.Ed25519PrivateKey.generate()
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, NODE_A)])
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(
                        f"spiffe://vonk-forge.local/node/{NODE_A}"
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
                "node_id": NODE_A,
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
        sent: list[dict[str, object]] = []
        delivered = False

        async def receive() -> dict[str, object]:
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: dict[str, object]) -> None:
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
        await asyncio.wait_for(app(scope, receive, send), timeout=1)
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
    job = Job(
        request_id=str(uuid.uuid4()),
        kind="agent.operations",
        state="queued",
        actor="administrator",
        base_commit="a" * 40,
        targets=[NODE_A],
        payload_digest=hashlib.sha256(b"{}").hexdigest(),
        payload={},
        current_attempt=0,
        created_at=clock.now,
        updated_at=clock.now,
    )
    with sessions.begin() as session:
        session.add(job)
    return job


def test_spoofed_agent_header_is_rejected() -> None:
    app = create_app(
        jobs=Jobs(),
        tokens=TokenCodec(b"k" * 32),
        audits=MemoryAuditStore(),
        fleet=dict,
    )

    response = TestClient(app).post(
        "/agent/v1/claim", headers={"x-vonk-agent-node": NODE_A}
    )

    assert response.status_code == 401


def test_unauthenticated_agent_gate_returns_without_reading_request_body() -> None:
    app = create_app(
        jobs=Jobs(),
        tokens=TokenCodec(b"k" * 32),
        audits=MemoryAuditStore(),
        fleet=dict,
    )
    sent: list[dict[str, object]] = []
    body_reads = 0

    async def receive() -> dict[str, object]:
        nonlocal body_reads
        body_reads += 1
        return {"type": "http.request", "body": b"untrusted", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/agent/v1/claim",
        "raw_path": b"/agent/v1/claim",
        "query_string": b"",
        "headers": (),
        "client": ("untrusted", 1234),
        "server": ("testserver", 80),
        "root_path": "",
        "state": {},
    }

    asyncio.run(asyncio.wait_for(app(scope, receive, send), timeout=0.5))

    start = next(
        message for message in sent if message["type"] == "http.response.start"
    )
    headers = dict(start["headers"])  # type: ignore[arg-type]
    assert start["status"] == 401
    assert body_reads == 0
    assert headers[b"x-content-type-options"] == b"nosniff"
    uuid.UUID(headers[b"x-request-id"].decode("ascii"))


def test_agent_routes_do_not_require_human_bearer_tokens() -> None:
    app = create_app(
        jobs=Jobs(),
        tokens=TokenCodec(b"k" * 32),
        audits=MemoryAuditStore(),
        fleet=dict,
    )

    response = TestClient(app).post(
        "/agent/v1/claim", headers={"Authorization": "Bearer invalid"}
    )

    assert response.status_code == 401


def test_untrusted_proxy_and_malformed_forwarded_identity_are_rejected(
    agent_system,
) -> None:
    client, _, _, _ = agent_system
    assert client.post("/agent/v1/claim").status_code == 401
    assert (
        client.post(
            "/agent/v1/claim",
            headers={
                **agent_headers(NODE_A, "serial-a"),
                "x-vonk-agent-verified": "false",
            },
        ).status_code
        == 401
    )

    app = create_app(
        jobs=Jobs(), tokens=TokenCodec(b"k" * 32), audits=MemoryAuditStore(), fleet=dict
    )
    assert (
        TestClient(app)
        .post("/agent/v1/claim", headers=agent_headers(NODE_A, "serial-a"))
        .status_code
        == 401
    )


def test_verified_identity_cannot_claim_other_node(agent_system) -> None:
    client, _, _, _ = agent_system
    response = client.post(
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"node_id": NODE_B},
    )
    assert response.status_code == 403


def test_claim_requires_a_trusted_policy_bounded_source(agent_system) -> None:
    client, services, _, _ = agent_system
    missing = agent_headers(NODE_A, "serial-a")
    missing.pop("x-vonk-agent-source")

    assert client.post("/agent/v1/claim", headers=missing).status_code == 401
    outside = client.post(
        "/agent/v1/claim",
        headers={
            **agent_headers(NODE_A, "serial-a"),
            "x-vonk-agent-source": "10.1.0.42",
        },
    )
    assert outside.status_code == 422
    with services.sessions() as session:
        assert session.get(AgentPresence, NODE_A) is None
        assert session.get(AgentNode, NODE_A).last_seen_at is None


def test_authenticated_claim_persists_certificate_bound_source(agent_system) -> None:
    client, services, _, clock = agent_system

    response = client.post("/agent/v1/claim", headers=agent_headers(NODE_A, "serial-a"))

    assert response.status_code == 204
    with services.sessions() as session:
        presence = session.get(AgentPresence, NODE_A)
        assert presence is not None
        assert presence.certificate_serial == "serial-a"
        assert presence.certificate_fingerprint == "fingerprint-serial-a"
        assert presence.management_address == "10.0.0.42"
        assert presence.observed_at.replace(tzinfo=UTC) == clock.now


def test_claim_uses_atomic_presence_consumer_not_post_commit(
    agent_system,
    monkeypatch,
) -> None:
    client, services, _, _ = agent_system

    def reject_post_commit(_source) -> None:
        raise AssertionError("presence must be written inside the queue transaction")

    monkeypatch.setattr(services.presence, "observe", reject_post_commit)

    response = client.post("/agent/v1/claim", headers=agent_headers(NODE_A, "serial-a"))

    assert response.status_code == 204
    with services.sessions() as session:
        assert session.get(AgentPresence, NODE_A) is not None


def test_authenticated_claim_records_protocol_contact_for_metrics(agent_system) -> None:
    client, services, _, clock = agent_system
    clock.now += timedelta(seconds=15)

    response = client.post(
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={
            "capabilities": CAPABILITIES,
            "lease_seconds": 30,
            "node_id": NODE_A,
            "protocol_version": 1,
            "runtime_identity": {
                "active_slot": "B",
                "architecture": "linux-arm64",
                "agent_sha256": "c" * 64,
                "build_digest": "sha256:" + "b" * 64,
                "platform_version": "1.2.3",
                "supervisor_generation": 7,
            },
            "wait_seconds": 0,
        },
    )

    assert response.status_code == 204
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.last_seen_at.replace(tzinfo=UTC) == clock.now
        assert node.protocol_version == 1
        assert node.capabilities == CAPABILITIES
        assert node.platform_version == "1.2.3"
        assert node.build_digest == "sha256:" + "b" * 64
        assert node.active_slot == "B"
        assert node.architecture == "linux-arm64"
        assert node.agent_sha256 == "c" * 64
        assert node.supervisor_generation == 7
        assert node.supervisor_ready_generation is None
        assert node.self_test_passed is False
        assert node.contact_certificate_serial == "serial-a"
        assert node.contact_observation_digest is not None
    metrics = MetricsRegistry()
    OperationalMetricsCollector(metrics, services.sessions, clock=clock).refresh()
    rendered = metrics.render()
    assert f'vonk_agent_last_seen_age_seconds{{node_id="{NODE_A}"}} 0' in rendered
    assert (
        f'vonk_agent_version_compatibility{{node_id="{NODE_A}",version_bucket="supported"}} 1'
        in rendered
    )


def test_authenticated_claim_records_generation_bound_self_test_readiness(
    agent_system,
) -> None:
    client, services, _, _clock = agent_system
    response = client.post(
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={
            "node_id": NODE_A,
            "runtime_identity": {
                "active_slot": "B",
                "architecture": "linux-arm64",
                "agent_sha256": "c" * 64,
                "build_digest": "sha256:" + "b" * 64,
                "platform_version": "1.2.3",
                "self_test_passed": True,
                "supervisor_generation": 7,
                "supervisor_ready_generation": 7,
            },
        },
    )

    assert response.status_code == 204
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.self_test_passed is True
        assert node.supervisor_ready_generation == 7
        assert node.contact_certificate_serial == "serial-a"
        assert node.contact_observation_digest is not None


@pytest.mark.parametrize("architecture", ("linux-riscv64", True, 7))
def test_claim_api_rejects_noncanonical_runtime_architecture(
    agent_system, architecture: object
) -> None:
    client, services, _, _ = agent_system

    response = client.post(
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={
            "node_id": NODE_A,
            "runtime_identity": {
                "active_slot": "B",
                "architecture": architecture,
                "agent_sha256": "c" * 64,
                "build_digest": "sha256:" + "b" * 64,
                "platform_version": "1.2.3",
                "supervisor_generation": 7,
            },
        },
    )

    assert response.status_code == 422
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.architecture is None


def test_unauthenticated_claim_cannot_change_runtime_architecture(agent_system) -> None:
    client, services, _, _ = agent_system

    response = client.post(
        "/agent/v1/claim",
        json={
            "node_id": NODE_A,
            "runtime_identity": {
                "active_slot": "B",
                "architecture": "linux-arm64",
                "agent_sha256": "c" * 64,
                "build_digest": "sha256:" + "b" * 64,
                "platform_version": "1.2.3",
                "supervisor_generation": 7,
            },
        },
    )

    assert response.status_code in {401, 403}
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.architecture is None


def test_unknown_claim_capability_is_rejected_without_contact(agent_system) -> None:
    client, services, _, _ = agent_system

    response = client.post(
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={
            "capabilities": CAPABILITIES + ["shell.exec"],
            "lease_seconds": 30,
            "node_id": NODE_A,
            "protocol_version": 1,
            "wait_seconds": 0,
        },
    )

    assert response.status_code == 422
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.capabilities == []
        assert node.protocol_version is None
        assert node.last_seen_at is None
        assert session.get(AgentPresence, NODE_A) is None


def test_control_accepts_next_agent_update_capabilities_during_rollout(
    agent_system,
) -> None:
    client, services, _, _ = agent_system
    capabilities = CAPABILITIES + ["agent.rollback", "agent.update"]

    response = client.post(
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={
            "capabilities": capabilities,
            "lease_seconds": 30,
            "node_id": NODE_A,
            "protocol_version": 1,
            "wait_seconds": 0,
        },
    )

    assert response.status_code == 204
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.capabilities == sorted(capabilities)


def test_authenticated_heartbeat_preserves_claim_advertised_protocol_after_exact_fence_validation(
    agent_system,
    monkeypatch,
) -> None:
    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id, NODE_A, "node.probe", "a" * 40, {}
    )
    claim = client.post(
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"protocol_version": 2},
    ).json()
    with services.sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.last_seen_at = None
    clock.now += timedelta(seconds=5)
    progress = {
        key: claim[key]
        for key in (
            "schema_version",
            "job_id",
            "operation_id",
            "attempt",
            "fence",
            "node_id",
            "deadline",
        )
    } | {"progress": {"phase": "checking"}}

    def reject_post_commit(_source) -> None:
        raise AssertionError("presence must be written inside the queue transaction")

    monkeypatch.setattr(services.presence, "observe", reject_post_commit)

    response = client.post(
        "/agent/v1/heartbeat",
        headers={
            **agent_headers(NODE_A, "serial-a"),
            "x-vonk-agent-source": "10.0.0.43",
        },
        json=progress,
    )

    assert response.status_code == 200
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.last_seen_at.replace(tzinfo=UTC) == clock.now
        assert node.protocol_version == 2
        presence = session.get(AgentPresence, NODE_A)
        assert presence is not None
        assert presence.management_address == "10.0.0.43"
        assert presence.observed_at.replace(tzinfo=UTC) == clock.now


def test_authenticated_result_preserves_claim_advertised_protocol_after_exact_fence_validation(
    agent_system,
    monkeypatch,
) -> None:
    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id, NODE_A, "node.probe", "a" * 40, {}
    )
    claim = client.post(
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"protocol_version": 2},
    ).json()
    with services.sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.last_seen_at = None
    clock.now += timedelta(seconds=5)
    result = {
        key: claim[key]
        for key in (
            "schema_version",
            "job_id",
            "operation_id",
            "attempt",
            "fence",
            "node_id",
            "deadline",
        )
    } | {"state": "succeeded", "result": PROBE_RESULT}

    def reject_post_commit(_source) -> None:
        raise AssertionError("presence must be written inside the queue transaction")

    monkeypatch.setattr(services.presence, "observe", reject_post_commit)

    response = client.post(
        "/agent/v1/result",
        headers={
            **agent_headers(NODE_A, "serial-a"),
            "x-vonk-agent-source": "10.0.0.44",
        },
        json=result,
    )

    assert response.status_code == 204
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.last_seen_at.replace(tzinfo=UTC) == clock.now
        assert node.protocol_version == 2
        presence = session.get(AgentPresence, NODE_A)
        assert presence is not None
        assert presence.management_address == "10.0.0.44"
        assert presence.observed_at.replace(tzinfo=UTC) == clock.now


def test_exact_fenced_probe_success_writes_bounded_durable_health(agent_system) -> None:
    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id, NODE_A, "node.probe", "a" * 40, {}
    )
    claim = client.post(
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={
            "capabilities": CAPABILITIES,
            "lease_seconds": 30,
            "node_id": NODE_A,
            "protocol_version": 1,
            "wait_seconds": 0,
        },
    ).json()
    clock.now += timedelta(seconds=2)
    result = {
        key: claim[key]
        for key in (
            "schema_version",
            "job_id",
            "operation_id",
            "attempt",
            "fence",
            "node_id",
            "deadline",
        )
    } | {"state": "succeeded", "result": PROBE_RESULT}

    response = client.post(
        "/agent/v1/result",
        headers=agent_headers(NODE_A, "serial-a"),
        json=result,
    )

    assert response.status_code == 204
    with services.sessions() as session:
        observations = list(
            session.scalars(select(Observation).where(Observation.node_id == NODE_A))
        )
        assert len(observations) == 1
        assert observations[0].kind == "health"
        assert observations[0].observed_at.replace(tzinfo=UTC) == clock.now
        assert observations[0].payload == {
            "active_nvidia_compute_processes": 0,
            "compute_occupancy": "clean",
            "disk_available_bytes": 2_000,
            "disk_total_bytes": 8_000,
            "memory_available_bytes": 1_000,
            "memory_total_bytes": 4_000,
            "status": "healthy",
        }


def test_failed_probe_result_never_writes_health_observation(agent_system) -> None:
    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id, NODE_A, "node.probe", "a" * 40, {}
    )
    claim = client.post(
        "/agent/v1/claim", headers=agent_headers(NODE_A, "serial-a")
    ).json()
    result = {
        key: claim[key]
        for key in (
            "schema_version",
            "job_id",
            "operation_id",
            "attempt",
            "fence",
            "node_id",
            "deadline",
        )
    } | {
        "state": "failed",
        "result": {"status": "failed", "error_code": "probe_failed"},
    }

    assert (
        client.post(
            "/agent/v1/result",
            headers=agent_headers(NODE_A, "serial-a"),
            json=result,
        ).status_code
        == 204
    )
    with services.sessions() as session:
        assert session.scalar(select(Observation)) is None


def test_untrusted_and_stale_requests_do_not_record_agent_contact(agent_system) -> None:
    client, services, _, clock = agent_system
    untrusted = client.post(
        "/agent/v1/claim",
        json={
            "lease_seconds": 30,
            "node_id": NODE_A,
            "protocol_version": 1,
            "wait_seconds": 0,
        },
    )
    assert untrusted.status_code == 401
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.last_seen_at is None
        assert node.protocol_version is None

    services.operations.enqueue(
        parent(services.sessions, clock).id, NODE_A, "node.probe", "a" * 40, {}
    )
    claim = client.post(
        "/agent/v1/claim", headers=agent_headers(NODE_A, "serial-a")
    ).json()
    with services.sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        presence = session.get(AgentPresence, NODE_A)
        assert node is not None
        assert presence is not None
        observed_at = presence.observed_at.replace(tzinfo=UTC)
        node.last_seen_at = None
        node.protocol_version = None
    clock.now += timedelta(seconds=5)
    stale = {
        key: claim[key]
        for key in (
            "schema_version",
            "job_id",
            "operation_id",
            "attempt",
            "node_id",
            "deadline",
        )
    } | {
        "fence": str(uuid.uuid4()),
        "state": "succeeded",
        "result": {"healthy": True},
    }

    rejected = client.post(
        "/agent/v1/result",
        headers={
            **agent_headers(NODE_A, "serial-a"),
            "x-vonk-agent-source": "10.0.0.43",
        },
        json=stale,
    )

    assert rejected.status_code == 409
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.last_seen_at is None
        assert node.protocol_version is None
        presence = session.get(AgentPresence, NODE_A)
        assert presence is not None
        assert presence.management_address == "10.0.0.42"
        assert presence.observed_at.replace(tzinfo=UTC) == observed_at


def test_boolean_protocol_advertisement_is_rejected_without_recording_contact(
    agent_system,
) -> None:
    client, services, _, _ = agent_system

    response = client.post(
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={
            "lease_seconds": 30,
            "node_id": NODE_A,
            "protocol_version": True,
            "wait_seconds": 0,
        },
    )

    assert response.status_code == 422
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.last_seen_at is None
        assert node.protocol_version is None


@pytest.mark.parametrize("mutation", ("revoked", "retired", "expired", "fingerprint"))
def test_persisted_certificate_state_is_checked_on_every_agent_request(
    agent_system, mutation: str
) -> None:
    client, services, _, clock = agent_system
    with services.sessions.begin() as session:
        certificate = session.get(AgentCertificate, "serial-a")
        node = session.get(AgentNode, NODE_A)
        assert certificate is not None and node is not None
        if mutation == "revoked":
            certificate.revoked_at = clock.now
        elif mutation == "retired":
            node.state = "retired"
        elif mutation == "expired":
            certificate.not_after = clock.now
        else:
            certificate.fingerprint = "different"
    assert (
        client.post(
            "/agent/v1/claim", headers=agent_headers(NODE_A, "serial-a")
        ).status_code
        == 401
    )


def test_fence_and_cross_node_result_updates_are_denied(agent_system) -> None:
    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id, NODE_A, "node.probe", "a" * 40, {}
    )
    claim = client.post(
        "/agent/v1/claim", headers=agent_headers(NODE_A, "serial-a")
    ).json()
    result = {
        key: claim[key]
        for key in (
            "schema_version",
            "job_id",
            "operation_id",
            "attempt",
            "fence",
            "node_id",
            "deadline",
        )
    }
    foreign = {
        **result,
        "node_id": NODE_B,
        "state": "succeeded",
        "result": {"healthy": True},
    }
    assert (
        client.post(
            "/agent/v1/result", headers=agent_headers(NODE_A, "serial-a"), json=foreign
        ).status_code
        == 403
    )
    stale = {
        **result,
        "fence": str(uuid.uuid4()),
        "state": "succeeded",
        "result": {"healthy": True},
    }
    assert (
        client.post(
            "/agent/v1/result", headers=agent_headers(NODE_A, "serial-a"), json=stale
        ).status_code
        == 409
    )


def test_enrollment_routes_are_admin_only_and_pending_exact_replay_is_idempotent(
    agent_system,
) -> None:
    client, _, codec, _ = agent_system
    assert (
        client.post(
            "/api/v1/agents/enrollments/grants",
            headers=admin_headers(codec, "operator"),
            json={"node_id": NODE_A, "ttl_seconds": 60},
        ).status_code
        == 403
    )
    # Existing node is deliberately unrelated to submitting a one-use grant;
    # approval remains the point that rejects a duplicate immutable node.
    grant = client.post(
        "/api/v1/agents/enrollments/grants",
        headers=admin_headers(codec),
        json={"node_id": NODE_A, "ttl_seconds": 60},
    ).json()
    key = ed25519.Ed25519PrivateKey.generate()
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, NODE_A)])
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(
                        f"spiffe://vonk-forge.local/node/{NODE_A}"
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
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    body = {
        "grant_token": grant["token"],
        "csr": csr.decode(),
        "evidence": {
            "node_id": NODE_A,
            "csr_public_key_fingerprint": hashlib.sha256(public).hexdigest(),
            "host_key_fingerprint": "host",
            "hardware_fingerprint": "hardware",
            "agent_digest": "a" * 64,
            "boot_id": "boot",
        },
    }
    first = client.post("/agent/v1/enroll", json=body)
    replay = client.post("/agent/v1/enroll", json=body)
    assert first.status_code == replay.status_code == 202
    assert first.content == replay.content == canonical_message(first.json())


def test_rust_migration_grant_is_admin_only_and_bound_to_legacy_node(
    agent_system,
) -> None:
    client, _services, codec, _clock = agent_system
    endpoint = f"/api/v1/agents/nodes/{NODE_A}/migration-grant"

    assert (
        client.post(
            endpoint,
            headers=admin_headers(codec, "operator"),
            json={"ttl_seconds": 60},
        ).status_code
        == 403
    )
    response = client.post(
        endpoint,
        headers=admin_headers(codec),
        json={"ttl_seconds": 60},
    )

    assert response.status_code == 201
    assert response.json()["node_id"] == NODE_A
    assert response.json()["purpose"] == "rust-migration"
    assert len(response.json()["token"]) == 43


def test_rust_agent_enrollment_shape_remains_controller_compatible(
    agent_system,
) -> None:
    client, services, _, _ = agent_system
    audits = client.app.state.test_audits
    fixture = json.loads(
        (
            Path(__file__).parents[2]
            / "agent_protocol/fixtures/enrollment-request.json"
        ).read_text()
    )
    body = json.loads(valid_enrollment_body(enrollment_grant(services)))

    assert set(body) == set(fixture) == {"csr", "evidence", "grant_token"}
    assert set(body["evidence"]) == set(fixture["evidence"])
    response = client.post("/agent/v1/enroll", json=body)

    assert response.status_code == 202
    assert response.json()["node_id"] == NODE_A
    event = audits.for_request(response.headers["x-request-id"])
    assert event.action == "agent.enrollment.submit.pending-approval"
    assert event.targets[1:] == (response.json()["id"], NODE_A)
    assert body["grant_token"] not in repr(event)


def test_agent_reads_only_its_installation_bound_built_recipe_spec(
    agent_system,
) -> None:
    client, services, _, clock = agent_system
    document = json.loads(
        (Path(__file__).parent / "fixtures/global/recipe-v1-minimal.json").read_text()
    )
    digest = recipe_content_sha256(document)
    recipe_id = str(uuid.uuid4())
    revision_id = str(uuid.uuid4())
    mapping_id = str(uuid.uuid4())
    build_id = str(uuid.uuid4())
    installation_id = str(uuid.uuid4())
    image_digest = "sha256:" + "d" * 64
    parameters = {item["name"]: item["default"] for item in document["parameters"]}
    with services.sessions.begin() as session:
        session.add(
            LocalRecipe(
                id=recipe_id,
                slug="agent-spec",
                title="Agent spec",
                description="Digest-bound agent fixture",
                source_kind="local",
                created_by="administrator",
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
        session.add(
            LocalRecipeRevision(
                id=revision_id,
                recipe_id=recipe_id,
                revision_number=1,
                lifecycle="resolved",
                schema_version=1,
                document=document,
                content_sha256=digest,
                created_by="administrator",
                created_at=clock.now,
            )
        )
        session.add(
            ClusterMapping(
                id=mapping_id,
                recipe_revision_id=revision_id,
                profile_name=document["deployment_profiles"][0]["name"],
                generation=1,
                node_count=1,
                state="ready",
                parameters=parameters,
                placement_digest="e" * 64,
                endpoint_owner_node_id=NODE_A,
                created_by="administrator",
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
        session.add(
            ClusterMappingNode(
                mapping_id=mapping_id,
                node_id=NODE_A,
                rank=0,
                role="entrypoint",
                endpoint_owner=True,
                created_at=clock.now,
            )
        )
        session.add(
            RecipeBuild(
                id=build_id,
                recipe_revision_id=revision_id,
                builder_node_id=NODE_A,
                source_bundle_sha256=document["build"]["context"]["sha256"],
                build_input_sha256="f" * 64,
                state="succeeded",
                policy_report={"passed": True},
                plan={},
                image_digest=image_digest,
                oci_layout_sha256="a" * 64,
                image_bytes=1024,
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
        session.add(
            RecipeInstallation(
                id=installation_id,
                recipe_revision_id=revision_id,
                mapping_id=mapping_id,
                mapping_generation=1,
                recipe_build_id=build_id,
                image_digest=image_digest,
                plan_digest="b" * 64,
                plan={},
                state="installing",
                actor="administrator",
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
        session.add(
            InstallationNode(
                installation_id=installation_id,
                node_id=NODE_A,
                rank=0,
                role="entrypoint",
                state="installing",
                required_bytes=1024,
                installed_bytes=0,
                updated_at=clock.now,
            )
        )

    response = client.get(
        f"/agent/v1/recipe-installations/{installation_id}/spec",
        headers=agent_headers(NODE_A, "serial-a"),
    )

    assert response.status_code == 200
    assert response.content == canonical_message(response.json())
    assert set(response.json()) == {
        "artifacts",
        "endpoint",
        "runtime",
        "security",
        "lifecycle",
    }
    assert response.json()["runtime"]["image"].endswith("@" + image_digest)
    assert (
        client.get(
            f"/agent/v1/recipe-installations/{uuid.uuid4()}/spec",
            headers=agent_headers(NODE_A, "serial-a"),
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/agent/v1/recipe-installations/{installation_id}/spec",
            headers=agent_headers(NODE_B, "serial-b"),
        ).status_code
        == 404
    )
    assert (
        client.get(f"/agent/v1/recipe-installations/{installation_id}/spec").status_code
        == 401
    )


def test_approved_exact_enrollment_replay_picks_up_certificate_and_mismatch_is_denied(
    agent_system,
) -> None:
    client, services, codec, _ = agent_system
    grant = services.enrollment.create(NODE_C, "administrator", 60)
    key = ed25519.Ed25519PrivateKey.generate()
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, NODE_C)])
        )
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
    body = {
        "grant_token": grant.token,
        "csr": csr.decode(),
        "evidence": {
            "node_id": NODE_C,
            "csr_public_key_fingerprint": hashlib.sha256(public).hexdigest(),
            "host_key_fingerprint": "host",
            "hardware_fingerprint": "hardware",
            "agent_digest": "a" * 64,
            "boot_id": "boot",
        },
    }
    pending = client.post("/agent/v1/enroll", json=body)
    enrollment_id = pending.json()["id"]
    approval = client.post(
        f"/api/v1/agents/enrollments/{enrollment_id}/approve",
        headers=admin_headers(codec),
    )

    assert approval.status_code == 200
    assert approval.json() == {
        "id": enrollment_id,
        "node_id": NODE_C,
        "state": "approved",
    }
    assert "certificate" not in approval.text.lower()
    assert "chain" not in approval.text.lower()

    pickup = client.post("/agent/v1/enroll", json=body)
    mismatch = client.post(
        "/agent/v1/enroll",
        json={**body, "evidence": {**body["evidence"], "boot_id": "different"}},
    )

    assert pickup.status_code == 200
    assert pickup.content == canonical_message(pickup.json())
    assert pickup.json()["generation"] == 1
    assert "certificate_pem" in pickup.json()
    assert mismatch.status_code == 403
    assert "certificate" not in mismatch.text.lower()


def test_human_enrollment_mutations_audit_only_success_with_request_actor_and_targets(
    agent_system,
) -> None:
    client, services, codec, _clock = agent_system
    headers = admin_headers(codec)
    audits = client.app.state.test_audits

    grant_response = client.post(
        "/api/v1/agents/enrollments/grants",
        headers=headers,
        json={"node_id": NODE_C, "ttl_seconds": 60},
    )
    grant = grant_response.json()
    csr = _csr_for(NODE_C)
    pending = client.post(
        "/agent/v1/enroll",
        json={
            "grant_token": grant["token"],
            "csr": csr.decode(),
            "evidence": {
                "node_id": NODE_C,
                "csr_public_key_fingerprint": _csr_fingerprint(csr),
                "host_key_fingerprint": "host-c",
                "hardware_fingerprint": "hardware-c",
                "agent_digest": "c" * 64,
                "boot_id": "boot-c",
            },
        },
    )
    approval = client.post(
        f"/api/v1/agents/enrollments/{pending.json()['id']}/approve",
        headers=headers,
    )

    node_d = "spk_" + "d" * 32
    direct_grant = services.enrollment.create(node_d, "administrator", 60)
    reject_csr = _csr_for(node_d)
    rejected_pending = client.post(
        "/agent/v1/enroll",
        json={
            "grant_token": direct_grant.token,
            "csr": reject_csr.decode(),
            "evidence": {
                "node_id": node_d,
                "csr_public_key_fingerprint": _csr_fingerprint(reject_csr),
                "host_key_fingerprint": "host-d",
                "hardware_fingerprint": "hardware-d",
                "agent_digest": "d" * 64,
                "boot_id": "boot-d",
            },
        },
    )
    rejection = client.post(
        f"/api/v1/agents/enrollments/{rejected_pending.json()['id']}/reject",
        headers=headers,
        json={"reason": "evidence mismatch"},
    )
    revocation = client.post(
        f"/api/v1/agents/nodes/{NODE_A}/revoke",
        headers=headers,
    )

    assert [
        grant_response.status_code,
        approval.status_code,
        rejection.status_code,
        revocation.status_code,
    ] == [201, 200, 200, 204]
    expected = {
        grant_response.headers["x-request-id"]: (
            "agent.enrollment.grant.create",
            (NODE_C,),
        ),
        approval.headers["x-request-id"]: (
            "agent.enrollment.approve",
            (pending.json()["id"], NODE_C),
        ),
        rejection.headers["x-request-id"]: (
            "agent.enrollment.reject",
            (rejected_pending.json()["id"], node_d),
        ),
        revocation.headers["x-request-id"]: (
            "agent.node.revoke",
            (NODE_A,),
        ),
    }
    for request_id, (action, targets) in expected.items():
        event = audits.for_request(request_id)
        assert event.actor == "administrator"
        assert event.action == action
        assert event.base_commit is None
        assert event.targets == targets

    successful_count = len(audits.list())
    failures = [
        client.post(
            "/api/v1/agents/enrollments/grants",
            headers=headers,
            json={"node_id": "invalid", "ttl_seconds": 60},
        ),
        client.post("/api/v1/agents/enrollments/unknown/approve", headers=headers),
        client.post(
            "/api/v1/agents/enrollments/unknown/reject",
            headers=headers,
            json={"reason": "invalid"},
        ),
        client.post(
            f"/api/v1/agents/nodes/{'spk_' + 'f' * 32}/revoke",
            headers=headers,
        ),
    ]
    assert [response.status_code for response in failures] == [422, 409, 409, 404]
    assert len(audits.list()) == successful_count


def _csr_for(node_id: str) -> bytes:
    key = ed25519.Ed25519PrivateKey.generate()
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, node_id)])
        )
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


def _csr_fingerprint(csr_pem: bytes) -> str:
    public_key = (
        x509.load_pem_x509_csr(csr_pem)
        .public_key()
        .public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return hashlib.sha256(public_key).hexdigest()


def test_fresh_rotation_follower_receives_canonical_retryable_response(
    agent_system,
) -> None:
    client, services, _, clock = agent_system
    request = _csr_for(NODE_A)
    with services.sessions.begin() as session:
        session.add(
            AgentCertificateRotation(
                node_id=NODE_A,
                source_serial="serial-a",
                generation=2,
                csr_pem=request.decode("ascii"),
                csr_public_key_fingerprint=_csr_fingerprint(request),
                provider_request_id="r" * 43,
                state="issuing",
                created_at=clock.now,
                updated_at=clock.now,
            )
        )

    response = client.post(
        "/agent/v1/renew",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"node_id": NODE_A, "csr": request.decode()},
    )

    assert response.status_code == 503
    assert response.content == canonical_message(response.json())
    assert response.json() == {"detail": "certificate rotation issuance is in progress"}


def test_staged_certificate_can_only_activate_and_activation_is_idempotent_after_response_loss(
    agent_system,
) -> None:
    client, services, _, _ = agent_system
    csr = _csr_for(NODE_A)
    first = client.post(
        "/agent/v1/renew",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"node_id": NODE_A, "csr": csr.decode()},
    )
    replay = client.post(
        "/agent/v1/renew",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"node_id": NODE_A, "csr": csr.decode()},
    )
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    issued = first.json()
    staged_headers = agent_headers(NODE_A, issued["serial"])
    staged_headers["x-vonk-agent-fingerprint"] = issued["fingerprint"]

    assert client.post("/agent/v1/claim", headers=staged_headers).status_code == 401
    assert (
        client.post(
            "/agent/v1/heartbeat", headers=staged_headers, json={"invalid": True}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/agent/v1/result", headers=staged_headers, json={"invalid": True}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/agent/v1/renew",
            headers=staged_headers,
            json={"node_id": NODE_A, "csr": _csr_for(NODE_A).decode()},
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/agent/v1/artifacts/" + "a" * 64,
            headers=staged_headers,
        ).status_code
        == 401
    )

    activation = {"node_id": NODE_A, "generation": issued["generation"]}
    assert (
        client.post(
            "/agent/v1/renew/activate", headers=staged_headers, json=activation
        ).status_code
        == 204
    )
    assert (
        client.post(
            "/agent/v1/renew/activate", headers=staged_headers, json=activation
        ).status_code
        == 204
    )
    assert (
        client.post(
            "/agent/v1/claim", headers=agent_headers(NODE_A, "serial-a")
        ).status_code
        == 401
    )
    assert client.post("/agent/v1/claim", headers=staged_headers).status_code == 204
    with services.sessions() as session:
        old = session.get(AgentCertificate, "serial-a")
        new = session.get(AgentCertificate, issued["serial"])
        assert old is not None and old.state == "revoked" and old.revoked_at is not None
        assert new is not None and new.state == "active" and new.revoked_at is None


def test_failed_result_preserves_canonical_evidence_and_maps_parent_reason(
    agent_system,
) -> None:
    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id, NODE_A, "node.probe", "a" * 40, {}
    )
    claim = client.post(
        "/agent/v1/claim", headers=agent_headers(NODE_A, "serial-a")
    ).json()
    result = {
        key: claim[key]
        for key in (
            "schema_version",
            "job_id",
            "operation_id",
            "attempt",
            "fence",
            "node_id",
            "deadline",
        )
    } | {
        "state": "failed",
        "result": {"status": "failed", "error_code": "probe_failed"},
    }

    response = client.post(
        "/agent/v1/result", headers=agent_headers(NODE_A, "serial-a"), json=result
    )

    assert response.status_code == 204
    with services.sessions() as session:
        attempt = (
            session.query(AgentOperationAttempt).filter_by(fence=claim["fence"]).one()
        )
        parent_job = session.get(Job, claim["job_id"])
        assert attempt.result == {"status": "failed", "error_code": "probe_failed"}
        assert parent_job is not None and parent_job.status_reason == "probe_failed"


def test_agent_validation_errors_are_canonical_json(agent_system) -> None:
    client, _, _, _ = agent_system

    response = client.post(
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"lease_seconds": 0, "node_id": NODE_A, "wait_seconds": 0},
    )

    assert response.status_code == 422
    assert response.content == canonical_message(response.json())


def test_claim_endpoint_long_poll_wakes_when_work_is_enqueued(agent_system) -> None:
    client, services, _, clock = agent_system
    parent_job = parent(services.sessions, clock)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(
            client.post,
            "/agent/v1/claim",
            headers=agent_headers(NODE_A, "serial-a"),
            json={"node_id": NODE_A, "lease_seconds": 30, "wait_seconds": 1},
        )
        time.sleep(0.05)
        operation = services.operations.enqueue(
            parent_job.id, NODE_A, "node.probe", "a" * 40, {}
        )
        response = waiting.result(timeout=1)

    assert response.status_code == 200
    assert response.json()["operation_id"] == operation.id
    assert time.monotonic() - started < 0.8


def test_enrollment_rate_limit_rejects_before_reading_request_body(
    agent_system,
) -> None:
    _, services, codec, _ = agent_system
    limiter = EnrollmentRateLimiter(maximum=1, window_seconds=60, clock=lambda: 0.0)
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        audits=MemoryAuditStore(),
        fleet=dict,
        agent=services,
        enrollment_rate_limiter=limiter,
    )
    assert (
        asgi_post(
            app, "/agent/v1/enroll", valid_enrollment_body(enrollment_grant(services))
        )[0]
        == 202
    )
    sent: list[dict[str, object]] = []
    reads = 0

    async def receive() -> dict[str, object]:
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"never-read", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/agent/v1/enroll",
        "raw_path": b"/agent/v1/enroll",
        "query_string": b"",
        "headers": ((b"content-type", b"application/json"),),
        "client": ("testclient", 1234),
        "server": ("testserver", 80),
        "root_path": "",
        "state": {},
    }
    asyncio.run(asyncio.wait_for(app(scope, receive, send), timeout=0.5))

    assert (
        next(message for message in sent if message["type"] == "http.response.start")[
            "status"
        ]
        == 429
    )
    assert reads == 0


def test_duplicate_enrollment_grants_consume_unicode_escaped_token_values(
    agent_system,
) -> None:
    client, services, _, _ = agent_system
    first = enrollment_grant(services)
    second = enrollment_grant(services)
    escaped_second = "".join(f"\\u{ord(character):04x}" for character in second)
    raw = (
        f'{{"grant_token":"{first}","gr\\u0061nt_token":"{escaped_second}"}}'
    ).encode("ascii")

    status_code, _ = asgi_post(client.app, "/agent/v1/enroll", raw)

    assert status_code == 422
    assert_grant_consumed(services, first)
    assert_grant_consumed(services, second)


def test_normal_enrollment_object_still_succeeds(agent_system) -> None:
    client, services, _, _ = agent_system
    token = enrollment_grant(services)

    status_code, response = asgi_post(
        client.app,
        "/agent/v1/enroll",
        valid_enrollment_body(token),
    )

    assert status_code == 202
    assert json.loads(response)["state"] == "pending-approval"


def test_oversized_enrollment_preserves_split_discovery_prefix(agent_system) -> None:
    _, services, _, _ = agent_system
    token = enrollment_grant(services)
    first = b" " * 1000 + b'{"grant_to'
    second = b'ken":"' + token.encode("ascii") + b'","padding":"' + b"x" * (64 * 1024)
    request = ChunkedEnrollmentRequest(first, second, b"must-not-be-received")

    with pytest.raises(HTTPException) as denied:
        asyncio.run(_bounded_enrollment_body(request, services))  # type: ignore[arg-type]

    assert denied.value.status_code == 413
    assert request.received == 2
    assert_grant_consumed(services, token)


def test_one_huge_enrollment_chunk_is_only_copied_through_fixed_prefix(
    agent_system,
) -> None:
    _, services, _, _ = agent_system
    token = enrollment_grant(services)
    huge = CopyBoundedChunk(
        b'{"grant_token":"'
        + token.encode("ascii")
        + b'","padding":"'
        + b"x" * (1024 * 1024)
    )
    request = ChunkedEnrollmentRequest(huge, b"must-not-be-received")

    with pytest.raises(HTTPException) as denied:
        asyncio.run(_bounded_enrollment_body(request, services))  # type: ignore[arg-type]

    assert denied.value.status_code == 413
    assert request.received == 1
    assert huge.largest_slice <= 2048
    assert_grant_consumed(services, token)


@pytest.mark.parametrize(
    "raw",
    (b"[1]", b"[]", b'"scalar"', b"0", b"true", b"false", b"null"),
    ids=("array", "empty-array", "string", "number", "true", "false", "null"),
)
def test_enrollment_rejects_non_object_json_without_server_error(
    agent_system, raw: bytes
) -> None:
    client, _, _, _ = agent_system

    status_code, _ = asgi_post(client.app, "/agent/v1/enroll", raw)

    assert status_code == 422


def test_non_object_enrollment_consumes_identifiable_nested_grant(agent_system) -> None:
    client, services, _, _ = agent_system
    token = enrollment_grant(services)
    raw = f'[{{"grant_token":"{token}"}}]'.encode("ascii")

    status_code, _ = asgi_post(client.app, "/agent/v1/enroll", raw)

    assert status_code == 422
    assert_grant_consumed(services, token)


def test_service_denied_enrollment_consumes_every_discovered_grant(
    agent_system,
) -> None:
    client, services, _, _ = agent_system
    effective = enrollment_grant(services)
    nested = enrollment_grant(services)
    body = json.loads(valid_enrollment_body(effective))
    body["evidence"]["extra"] = {"grant_token": nested}

    status_code, _ = asgi_post(
        client.app,
        "/agent/v1/enroll",
        json.dumps(body).encode("utf-8"),
    )

    assert status_code == 403
    assert_grant_consumed(services, effective)
    assert_grant_consumed(services, nested)


@pytest.mark.parametrize(
    ("prefix", "suffix"),
    (
        (b'{"grant_token":"', b'",]'),
        (b'{"grant_token":"', b'","invalid-utf8":"\xff"}'),
        (b"[" * 1500 + b'{"grant_token":"', b'"}' + b"]" * 1500),
    ),
    ids=("malformed-json", "invalid-utf8", "deep-nesting"),
)
def test_invalid_enrollment_json_consumes_identifiable_grant(
    agent_system,
    prefix: bytes,
    suffix: bytes,
) -> None:
    client, services, _, _ = agent_system
    token = enrollment_grant(services)

    status_code, _ = asgi_post(
        client.app,
        "/agent/v1/enroll",
        prefix + token.encode("ascii") + suffix,
    )

    assert status_code == 422
    assert_grant_consumed(services, token)


def test_wrong_enrollment_content_type_consumes_identifiable_grant(
    agent_system,
) -> None:
    client, services, _, _ = agent_system
    token = enrollment_grant(services)

    status_code, _ = asgi_post(
        client.app,
        "/agent/v1/enroll",
        f'{{"grant_token":"{token}"}}'.encode("ascii"),
        content_type="text/plain",
    )

    assert status_code == 415
    assert_grant_consumed(services, token)


def test_enrollment_evidence_has_a_fixed_bounded_schema(agent_system) -> None:
    client, _, _, _ = agent_system
    response = client.post(
        "/agent/v1/enroll",
        json={
            "grant_token": "a" * 43,
            "csr": "x",
            "evidence": {
                "node_id": NODE_A,
                "csr_public_key_fingerprint": "a" * 64,
                "host_key_fingerprint": "host",
                "hardware_fingerprint": "hardware",
                "agent_digest": "a" * 64,
                "boot_id": "boot",
                "unexpected": "x",
            },
        },
    )
    assert response.status_code == 403


def test_artifact_access_is_owned_content_addressed_and_range_bounded(
    agent_system,
) -> None:
    client, services, _, clock = agent_system
    digest = hashlib.sha256(b"artifact").hexdigest()
    (services.artifact_root / digest).write_bytes(b"artifact")
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "node.probe",
        "a" * 40,
        {"artifact_digest": digest},
    )
    response = client.get(
        f"/agent/v1/artifacts/{digest}",
        headers={**agent_headers(NODE_A, "serial-a"), "Range": "bytes=1-3"},
    )
    assert (
        response.status_code,
        response.content,
        response.headers["content-range"],
    ) == (206, b"rti", "bytes 1-3/8")
    assert (
        client.get(
            f"/agent/v1/artifacts/{digest}", headers=agent_headers(NODE_B, "serial-b")
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/agent/v1/artifacts/../secret", headers=agent_headers(NODE_A, "serial-a")
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/agent/v1/artifacts/{digest}",
            headers={**agent_headers(NODE_A, "serial-a"), "Range": "bytes=0-99999999"},
        ).status_code
        == 416
    )


def test_artifact_symlink_is_never_served(agent_system, tmp_path) -> None:
    client, services, _, clock = agent_system
    digest = "a" * 64
    (services.artifact_root / digest).symlink_to(tmp_path / "outside")
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "node.probe",
        "a" * 40,
        {"artifact_digest": digest},
    )
    assert (
        client.get(
            f"/agent/v1/artifacts/{digest}", headers=agent_headers(NODE_A, "serial-a")
        ).status_code
        == 404
    )


def test_artifact_digest_is_verified_from_open_descriptor(agent_system) -> None:
    client, services, _, clock = agent_system
    digest = hashlib.sha256(b"expected").hexdigest()
    (services.artifact_root / digest).write_bytes(b"tampered")
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "node.probe",
        "a" * 40,
        {"artifact_digest": digest},
    )
    assert (
        client.get(
            f"/agent/v1/artifacts/{digest}", headers=agent_headers(NODE_A, "serial-a")
        ).status_code
        == 404
    )


def test_authenticated_agents_can_fetch_bounded_platform_tuf_files(
    agent_system,
) -> None:
    client, services, _, _ = agent_system
    metadata = b'{"signed":{"_type":"timestamp"}}'
    target = b'{"platform_version":"1.2.3"}\n'
    target_name = f"platform/releases/1.2.3/{'a' * 64}.json"
    (services.tuf_metadata_root / "timestamp.json").write_bytes(metadata)
    target_path = services.tuf_target_root / target_name
    target_path.parent.mkdir(parents=True)
    target_path.write_bytes(target)

    metadata_response = client.get(
        "/agent/v1/tuf/metadata/timestamp.json",
        headers=agent_headers(NODE_A, "serial-a"),
    )
    target_response = client.get(
        f"/agent/v1/tuf/targets/{target_name}",
        headers=agent_headers(NODE_A, "serial-a"),
    )

    assert metadata_response.status_code == 200
    assert metadata_response.content == metadata
    assert metadata_response.headers["content-type"] == "application/json"
    assert target_response.status_code == 200
    assert target_response.content == target
    assert target_response.headers["content-type"] == "application/octet-stream"
    assert client.get("/agent/v1/tuf/metadata/timestamp.json").status_code == 401
    assert client.get(f"/agent/v1/tuf/targets/{target_name}").status_code == 401


def test_authenticated_agents_can_fetch_only_signed_workload_tuf_targets(
    agent_system,
) -> None:
    client, services, _, _ = agent_system
    raw = b'{"schema_version":1,"workload":"unknown"}'
    digest = hashlib.sha256(raw).hexdigest()
    services.workload_tuf_metadata_root.mkdir(parents=True, exist_ok=True)
    services.workload_tuf_target_root.mkdir(parents=True, exist_ok=True)
    (services.workload_tuf_metadata_root / "timestamp.json").write_bytes(
        b'{"signed":{"_type":"timestamp"}}'
    )
    (services.workload_tuf_target_root / digest).write_bytes(raw)

    metadata = client.get(
        "/agent/v1/workload-tuf/metadata/timestamp.json",
        headers=agent_headers(NODE_A, "serial-a"),
    )
    target = client.get(
        f"/agent/v1/workload-tuf/targets/releases/{digest}.json",
        headers=agent_headers(NODE_A, "serial-a"),
    )
    assert metadata.status_code == 200
    assert metadata.content == b'{"signed":{"_type":"timestamp"}}'
    assert target.status_code == 200
    assert target.content == raw
    assert (
        client.get(
            "/agent/v1/workload-tuf/targets/platform/releases/1.2.3/"
            + "a" * 64
            + ".json",
            headers=agent_headers(NODE_A, "serial-a"),
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    ("kind", "name"),
    (
        ("metadata", "../timestamp.json"),
        ("metadata", "0.root.json"),
        ("metadata", "Timestamp.json"),
        ("targets", "../platform-release.json"),
        ("targets", "platform-release.json"),
        ("targets", f"platform/releases/latest/{'a' * 64}.json"),
        ("targets", f"platform/releases/01.2.3/{'a' * 64}.json"),
        ("targets", f"platform/releases/1.2.3/{'A' * 64}.json"),
        ("targets", f"platform/releases/1.2.3/{'a' * 63}.json"),
        ("targets", f"platform/releases/1.2.3//{'a' * 64}.json"),
        ("targets", ".hidden"),
        ("targets", "UPPER"),
    ),
)
def test_platform_tuf_routes_reject_unsafe_names(
    agent_system,
    kind: str,
    name: str,
) -> None:
    client, _, _, _ = agent_system

    response = client.get(
        f"/agent/v1/tuf/{kind}/{name}",
        headers=agent_headers(NODE_A, "serial-a"),
    )

    assert response.status_code == 404


def test_platform_tuf_routes_reject_symlinks_writable_files_and_oversize(
    agent_system,
    tmp_path,
) -> None:
    client, services, _, _ = agent_system
    target_name = f"platform/releases/1.2.3/{'a' * 64}.json"
    target_directory = services.tuf_target_root / "platform/releases/1.2.3"
    target_directory.mkdir(parents=True)
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"{}")
    (services.tuf_metadata_root / "timestamp.json").symlink_to(outside)
    writable = target_directory / f"{'a' * 64}.json"
    writable.write_bytes(b"{}")
    writable.chmod(0o666)
    executable = target_directory / f"{'b' * 64}.json"
    executable.write_bytes(b"{}")
    executable.chmod(0o555)
    oversized = target_directory / f"{'c' * 64}.json"
    oversized.write_bytes(b"x" * 129)

    headers = agent_headers(NODE_A, "serial-a")
    assert (
        client.get("/agent/v1/tuf/metadata/timestamp.json", headers=headers).status_code
        == 404
    )
    assert (
        client.get(f"/agent/v1/tuf/targets/{target_name}", headers=headers).status_code
        == 404
    )
    assert (
        client.get(
            f"/agent/v1/tuf/targets/platform/releases/1.2.3/{'b' * 64}.json",
            headers=headers,
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/agent/v1/tuf/targets/platform/releases/1.2.3/{'c' * 64}.json",
            headers=headers,
        ).status_code
        == 413
    )


def test_platform_tuf_target_rejects_nested_symlinks_and_hardlinks(
    agent_system,
    tmp_path,
) -> None:
    client, services, _, _ = agent_system
    headers = agent_headers(NODE_A, "serial-a")
    outside = tmp_path / "outside"
    outside_target = outside / "releases/1.2.3" / f"{'d' * 64}.json"
    outside_target.parent.mkdir(parents=True)
    outside_target.write_bytes(b"outside")
    (services.tuf_target_root / "platform").symlink_to(outside)

    assert (
        client.get(
            f"/agent/v1/tuf/targets/platform/releases/1.2.3/{'d' * 64}.json",
            headers=headers,
        ).status_code
        == 404
    )

    (services.tuf_target_root / "platform").unlink()
    target_directory = services.tuf_target_root / "platform/releases/1.2.3"
    target_directory.mkdir(parents=True)
    source = target_directory / "source"
    source.write_bytes(b"hard-linked")
    os.link(source, target_directory / f"{'e' * 64}.json")

    assert (
        client.get(
            f"/agent/v1/tuf/targets/platform/releases/1.2.3/{'e' * 64}.json",
            headers=headers,
        ).status_code
        == 404
    )


def test_platform_tuf_target_rejects_file_changed_while_reading(
    agent_system,
    monkeypatch,
) -> None:
    client, services, _, _ = agent_system
    target_name = f"platform/releases/1.2.3/{'f' * 64}.json"
    target = services.tuf_target_root / target_name
    target.parent.mkdir(parents=True)
    target.write_bytes(b"original")
    real_read = os.read
    changed = False

    def changing_read(descriptor: int, count: int) -> bytes:
        nonlocal changed
        result = real_read(descriptor, count)
        if not changed:
            changed = True
            target.write_bytes(b"replaced")
        return result

    monkeypatch.setattr("vonk_control.agent_api.os.read", changing_read)

    response = client.get(
        f"/agent/v1/tuf/targets/{target_name}",
        headers=agent_headers(NODE_A, "serial-a"),
    )

    assert response.status_code == 404


def test_invalid_ranges_do_not_leak_artifact_descriptors(agent_system) -> None:
    client, services, _, clock = agent_system
    digest = hashlib.sha256(b"artifact").hexdigest()
    (services.artifact_root / digest).write_bytes(b"artifact")
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "node.probe",
        "a" * 40,
        {"artifact_digest": digest},
    )
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(25):
        assert (
            client.get(
                f"/agent/v1/artifacts/{digest}",
                headers={
                    **agent_headers(NODE_A, "serial-a"),
                    "Range": "bytes=" + "9" * 5000 + "-1",
                },
            ).status_code
            == 416
        )
    assert len(os.listdir("/proc/self/fd")) <= before + 1


def test_artifact_stream_close_releases_its_descriptor(tmp_path) -> None:
    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"artifact")
    descriptor = os.open(artifact, os.O_RDONLY)
    stream = _read_chunks(descriptor, 0, 8)
    assert next(stream) == b"artifact"
    stream.close()
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_artifact_snapshot_is_immutable_after_source_overwrite(tmp_path) -> None:
    source = tmp_path / "artifact"
    source.write_bytes(b"original")
    descriptor = os.open(source, os.O_RDONLY)
    snapshot = _sealed_snapshot(
        descriptor, 8, 1024, hashlib.sha256(b"original").hexdigest()
    )
    source.write_bytes(b"replaced")
    try:
        assert snapshot.read() == b"original"
    finally:
        snapshot.close()


def test_snapshot_allocation_failure_closes_source_descriptor(
    tmp_path, monkeypatch
) -> None:
    source = tmp_path / "artifact"
    source.write_bytes(b"original")
    descriptor = os.open(source, os.O_RDONLY)
    monkeypatch.setattr(
        "vonk_control.agent_api.tempfile.TemporaryFile",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("full")),
    )
    with pytest.raises(OSError, match="full"):
        _sealed_snapshot(descriptor, 8, 1024, hashlib.sha256(b"original").hexdigest())
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_protected_agent_routes_gate_untrusted_invalid_bodies_before_parsing(
    agent_system,
) -> None:
    client, _, _, _ = agent_system
    for path in (
        "/agent/v1/claim",
        "/agent/v1/heartbeat",
        "/agent/v1/result",
        "/agent/v1/renew",
    ):
        assert (
            client.post(
                path, content=b"{not-json", headers={"content-type": "application/json"}
            ).status_code
            == 401
        )


def test_revoked_identity_is_gated_before_invalid_json_is_parsed(agent_system) -> None:
    client, services, _, clock = agent_system
    with services.sessions.begin() as session:
        session.get(AgentCertificate, "serial-a").revoked_at = clock.now  # type: ignore[union-attr]
    assert (
        client.post(
            "/agent/v1/result",
            headers={
                **agent_headers(NODE_A, "serial-a"),
                "content-type": "application/json",
            },
            content=b"{not-json",
        ).status_code
        == 401
    )


def test_node_revocation_has_typed_4xx_and_uncertain_remote_statuses(
    agent_system,
) -> None:
    client, services, codec, _ = agent_system
    headers = admin_headers(codec)
    assert (
        client.post(
            "/api/v1/agents/nodes/not-canonical/revoke", headers=headers
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/v1/agents/nodes/{'spk_' + '1' * 32}/revoke", headers=headers
        ).status_code
        == 404
    )

    authority = services.enrollment._authority
    authority.fail_revoke = True
    response = client.post(f"/api/v1/agents/nodes/{NODE_A}/revoke", headers=headers)
    assert response.status_code == 503
    with services.sessions() as session:
        assert session.get(AgentNode, NODE_A).state == "retired"  # type: ignore[union-attr]
        assert session.get(AgentCertificate, "serial-a").revoked_at is not None  # type: ignore[union-attr]


def test_enrollment_overflow_burns_valid_grant_before_rejection(agent_system) -> None:
    client, _, codec, _ = agent_system
    grant = client.post(
        "/api/v1/agents/enrollments/grants",
        headers=admin_headers(codec),
        json={"node_id": NODE_A, "ttl_seconds": 60},
    ).json()
    key = ed25519.Ed25519PrivateKey.generate()
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, NODE_A)])
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(
                        f"spiffe://vonk-forge.local/node/{NODE_A}"
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
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    body = {
        "grant_token": grant["token"],
        "csr": csr.decode(),
        "evidence": {
            "node_id": NODE_A,
            "csr_public_key_fingerprint": hashlib.sha256(public).hexdigest(),
            "host_key_fingerprint": "x" * 513,
            "hardware_fingerprint": "hardware",
            "agent_digest": "a" * 64,
            "boot_id": "boot",
        },
    }
    assert client.post("/agent/v1/enroll", json=body).status_code == 403
    assert client.post("/agent/v1/enroll", json=body).status_code == 403


def test_enrollment_unknown_top_level_field_burns_valid_grant(agent_system) -> None:
    client, _, codec, _ = agent_system
    grant = client.post(
        "/api/v1/agents/enrollments/grants",
        headers=admin_headers(codec),
        json={"node_id": NODE_A, "ttl_seconds": 60},
    ).json()
    key = ed25519.Ed25519PrivateKey.generate()
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, NODE_A)])
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.UniformResourceIdentifier(
                        f"spiffe://vonk-forge.local/node/{NODE_A}"
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
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    body = {
        "grant_token": grant["token"],
        "csr": csr.decode(),
        "evidence": {
            "node_id": NODE_A,
            "csr_public_key_fingerprint": hashlib.sha256(public).hexdigest(),
            "host_key_fingerprint": "host",
            "hardware_fingerprint": "hardware",
            "agent_digest": "a" * 64,
            "boot_id": "boot",
        },
        "unknown": "denied",
    }
    assert client.post("/agent/v1/enroll", json=body).status_code == 403
    assert client.post("/agent/v1/enroll", json=body).status_code == 403


def test_enrollment_listing_paginates_stably_and_can_filter_issuing(
    agent_system,
) -> None:
    client, services, codec, clock = agent_system
    with services.sessions.begin() as session:
        for index in range(101):
            grant_id = str(uuid.uuid4())
            session.add(
                AgentEnrollmentGrant(
                    id=grant_id,
                    node_id=NODE_A,
                    token_digest=hashlib.sha256(str(index).encode()).hexdigest(),
                    created_by="admin",
                    created_at=clock.now,
                    expires_at=clock.now + timedelta(seconds=60),
                )
            )
            session.add(
                AgentEnrollment(
                    id=str(uuid.uuid4()),
                    grant_id=grant_id,
                    node_id=NODE_A,
                    state="issuing" if index == 0 else "rejected",
                    csr_pem="csr",
                    csr_public_key_pem="pem",
                    csr_public_key_fingerprint="a" * 64,
                    host_key_fingerprint="host",
                    hardware_fingerprint="hardware",
                    agent_digest="a" * 64,
                    boot_id="boot",
                    created_at=clock.now,
                )
            )
    first = client.get(
        "/api/v1/agents/enrollments?limit=100", headers=admin_headers(codec)
    ).json()
    assert len(first["enrollments"]) == 100
    assert first["next_cursor"]
    second = client.get(
        f"/api/v1/agents/enrollments?limit=100&cursor={first['next_cursor']}",
        headers=admin_headers(codec),
    ).json()
    assert len(second["enrollments"]) == 1
    issuing = client.get(
        "/api/v1/agents/enrollments?state=issuing", headers=admin_headers(codec)
    ).json()
    assert [item["state"] for item in issuing["enrollments"]] == ["issuing"]
