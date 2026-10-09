from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import time
import uuid
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import TypedDict, Unpack

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID
from fastapi import HTTPException
from fastapi.testclient import TestClient
from httpx2 import ASGITransport, AsyncClient, Response
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.types import Message
from vonk_agent_protocol import CompiledExecutionPlan as AgentCompiledExecutionPlan
from vonk_agent_protocol import (
    ContainerRuntimeAction,
    ExecuteContainerRuntimeRequestOperation,
    InstallVonkDebOperation,
    PackageRollbackAuthority,
    RecipeStopPayload,
    RunState,
    SecurityRefusalReason,
    SignedHostHelperGrant,
    canonical_message,
)
from vonk_agent_protocol.host_helper import (
    HostRuntimeRequest,
    RecipeReconciliationIdentity,
    host_helper_grant_signing_bytes,
)
from vonk_control import agent_operation_states as aos
from vonk_control.agent_api import (
    AgentApiServices,
    EnrollmentRateLimiter,
    _bounded_enrollment_body,
)
from vonk_control.agent_jobs import AgentJobService
from vonk_control.api import create_app
from vonk_control.auth import Actor, AgentSource, TokenCodec
from vonk_control.ca_issuance_contract import CertificateIssuanceBinding
from vonk_control.enrollment import EnrollmentService
from vonk_control.enrollment_bootstrap import EnrollmentBootstrapConfig
from vonk_control.enrollment_contract import EnrollmentGrant, EnrollmentObservationReply
from vonk_control.host_helper_authority import (
    HostHelperGrantIssuer,
    HostRuntimeAuthorityService,
)
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    AgentPresence,
    Base,
    CatalogDocument,
    CatalogDocumentRevision,
    Job,
    NodeInventorySnapshot,
    NodeTelemetrySample,
    RecipeBuild,
    RecipeRun,
    RecipeSourceBundle,
    RunNode,
)
from vonk_control.pki import IssuedCertificate
from vonk_control.presence import AgentPresenceService, ManagementAddressPolicy
from vonk_control.source_bundles import SourceBundleStore, generate_source_bundle
from vonk_forge_contracts import RecipeDefinition, document_sha256

from .agent_fences import fenced_attempt, fenced_operation
from .ca_test_authority import FixtureCertificateAuthority
from .stored_documents_support import valid_policy_report

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
        return IssuedCertificate(
            node_id,
            b"certificate",
            b"chain",
            "1",
            "e" * 64,
            datetime.fromisoformat(request.not_before),
            datetime.fromisoformat(request.not_after),
            generation=request.generation,
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

    def observe_contact(session: Session, source: AgentSource) -> None:
        presence.observe_in_session(session, source)

    operations.set_contact_consumer(observe_contact)
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
                "registry.example.test",
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
                "boot_id": boot_id,
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


def chunked_asgi_telemetry(
    app: object,
    *,
    extra_headers: tuple[tuple[bytes, bytes], ...],
) -> tuple[int, int]:
    from vonk_agent_protocol.telemetry import MAX_TELEMETRY_REPORT_BYTES

    async def request() -> tuple[int, int]:
        chunks = [
            b'{"samples":[],"padding":"' + b"x" * (MAX_TELEMETRY_REPORT_BYTES // 2),
            b"x" * (MAX_TELEMETRY_REPORT_BYTES // 2),
            b'"}',
        ]
        reads = 0
        sent: list[Message] = []

        async def receive() -> dict[str, object]:
            nonlocal reads
            if reads >= len(chunks):
                return {"type": "http.disconnect"}
            body = chunks[reads]
            reads += 1
            return {
                "type": "http.request",
                "body": body,
                "more_body": reads < len(chunks),
            }

        async def send(message: Message) -> None:
            sent.append(message)

        forwarded = tuple(
            (key.encode("ascii"), value.encode("ascii"))
            for key, value in agent_headers(NODE_A, "serial-a").items()
        )
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/agent/telemetry",
            "raw_path": b"/agent/telemetry",
            "query_string": b"",
            "headers": (
                (b"content-type", b"application/json"),
                (b"host", b"testserver"),
                *forwarded,
                *extra_headers,
            ),
            "client": ("testclient", 1234),
            "server": ("testserver", 443),
            "root_path": "",
            "state": {},
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=10)  # type: ignore[operator]
        start = next(
            message for message in sent if message["type"] == "http.response.start"
        )
        return int(start["status"]), reads

    return asyncio.run(request())


def test_agent_posts_authenticated_telemetry_for_certificate_node(
    agent_system,
) -> None:
    client, services, _, clock = agent_system
    payload = telemetry_payload(
        clock,
        observed_at=clock.now - timedelta(seconds=1),
    )
    payload["samples"].append(  # type: ignore[union-attr]
        telemetry_payload(
            clock,
            observed_at=clock.now,
        )["samples"][0]  # type: ignore[index]
    )

    response = client.post(
        "/agent/telemetry",
        headers=agent_headers(NODE_A, "serial-a"),
        json=payload,
    )

    assert response.status_code == 204
    with services.sessions() as session:
        rows = session.scalars(
            select(NodeTelemetrySample).order_by(NodeTelemetrySample.observed_at)
        ).all()
        assert [row.node_id for row in rows] == [NODE_A, NODE_A]
        assert rows[0].observed_at != rows[0].received_at
        assert rows[0].received_at == clock.now.replace(tzinfo=None)


def test_telemetry_body_cannot_choose_node_identity(agent_system) -> None:
    client, services, _, clock = agent_system
    payload = telemetry_payload(clock) | {"node_id": NODE_B}

    response = client.post(
        "/agent/telemetry",
        headers=agent_headers(NODE_A, "serial-a"),
        json=payload,
    )

    assert response.status_code == 422
    with services.sessions() as session:
        assert session.scalar(select(NodeTelemetrySample)) is None


def test_telemetry_authentication_happens_before_json_parsing(agent_system) -> None:
    client, services, _, _ = agent_system
    assert services.bootstrap is not None
    response = client.post(
        "/agent/telemetry",
        headers={"content-type": "application/json"},
        content=b'{"schema_version":1,"schema_version":2',
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "offset",
    [
        -timedelta(minutes=5, microseconds=1),
        timedelta(seconds=30, microseconds=1),
    ],
)
def test_telemetry_rejects_stale_or_future_samples(agent_system, offset) -> None:
    client, _, _, clock = agent_system
    response = client.post(
        "/agent/telemetry",
        headers=agent_headers(NODE_A, "serial-a"),
        json=telemetry_payload(clock, observed_at=clock.now + offset),
    )
    assert response.status_code == 422


def test_telemetry_rejects_more_than_sixteen_samples(agent_system) -> None:
    client, _, _, clock = agent_system
    samples = [
        telemetry_payload(
            clock,
            observed_at=clock.now - timedelta(seconds=16 - index),
        )["samples"][0]  # type: ignore[index]
        for index in range(17)
    ]
    response = client.post(
        "/agent/telemetry",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"samples": samples},
    )
    assert response.status_code == 422


def test_telemetry_observed_at_is_rfc3339_string(agent_system) -> None:
    client, _, _, clock = agent_system
    payload = telemetry_payload(clock)
    payload["samples"][0]["observed_at"] = int(clock.now.timestamp())  # type: ignore[index]
    response = client.post(
        "/agent/telemetry",
        headers=agent_headers(NODE_A, "serial-a"),
        json=payload,
    )
    assert response.status_code == 422


def test_telemetry_requires_every_fixed_core_metric(agent_system) -> None:
    client, _, _, clock = agent_system
    payload = telemetry_payload(clock)
    del payload["samples"][0]["gpu_utilization_percent"]  # type: ignore[index]
    response = client.post(
        "/agent/telemetry",
        headers=agent_headers(NODE_A, "serial-a"),
        json=payload,
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "document",
    [
        '{"samples":[],"samples":[]}',
        (
            '{"samples":[{'
            '"boot_id":"00000000-0000-4000-8000-000000000001",'
            '"observed_at":"2026-08-03T12:00:00+00:00",'
            '"observed_at":"2026-08-03T12:00:01+00:00"}]}'
        ),
    ],
)
def test_telemetry_rejects_duplicate_json_keys(agent_system, document: str) -> None:
    client, _, _, _ = agent_system
    response = client.post(
        "/agent/telemetry",
        headers={
            **agent_headers(NODE_A, "serial-a"),
            "content-type": "application/json",
        },
        content=document,
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "headers",
    [
        ((b"transfer-encoding", b"chunked"),),
        ((b"content-length", b"64"),),
    ],
)
def test_telemetry_streams_only_to_endpoint_body_limit(
    agent_system,
    headers: tuple[tuple[bytes, bytes], ...],
) -> None:
    client, services, _, _ = agent_system
    assert services.bootstrap is not None
    status_code, reads = chunked_asgi_telemetry(client.app, extra_headers=headers)
    assert status_code == 413
    assert reads == 2


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
        "memory_pool": "separate",
        "artifact_store_read_only": False,
        "capabilities": [
            "runtime.vonk.v1",
            "recipe.image.pull.v1",
            "fabric.connected.mbps.200000",
        ],
        "fabric_address": "192.168.100.2",
        "fabric_bandwidth_mbps": 200000,
        "nvidia_driver_version": "580.65.06",
        "container_runtime_version": "28.3.3",
        "network_interfaces": [
            {"name": "enP7s7", "kind": "wired", "carrier": False},
            {"name": "wlP9s9", "kind": "wifi", "carrier": True},
        ],
        "nas_route_interface": "wlP9s9",
    }

    response = client.post(
        "/agent/inventory",
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
        assert row.nas_route_interface == "wlP9s9"
        assert row.network_interfaces == [
            {
                "name": "enP7s7",
                "kind": "wired",
                "link_speed_mbps": None,
                "carrier": False,
            },
            {
                "name": "wlP9s9",
                "kind": "wifi",
                "link_speed_mbps": None,
                "carrier": True,
            },
        ]
        assert row.capabilities == sorted(payload["capabilities"])

    # Optional evidence never costs the mandatory capacity report: a NAS route
    # that names no reported NIC, or a fabric address outside the fabric policy,
    # is dropped and the rest of the report is recorded.
    later = (clock.now + timedelta(seconds=1)).isoformat()
    inconsistent = payload | {"nas_route_interface": "wlan9", "observed_at": later}
    assert (
        client.post(
            "/agent/inventory",
            headers=agent_headers(NODE_A, "serial-a"),
            json=inconsistent,
        ).status_code
        == 204
    )
    with services.sessions() as session:
        row = session.scalar(
            select(NodeInventorySnapshot)
            .where(NodeInventorySnapshot.node_id == NODE_A)
            .order_by(NodeInventorySnapshot.observed_at.desc())
        )
        assert row is not None
        assert row.nas_route_interface is None
        assert row.disk_free_bytes == 700
        assert [item["name"] for item in row.network_interfaces] == [
            "enP7s7",
            "wlP9s9",
        ]

    denied = payload | {"fabric_address": "10.0.0.42"}
    assert (
        client.post(
            "/agent/inventory",
            headers=agent_headers(NODE_B, "serial-b"),
            json=denied,
        ).status_code
        == 204
    )
    with services.sessions() as session:
        row = session.scalar(
            select(NodeInventorySnapshot).where(NodeInventorySnapshot.node_id == NODE_B)
        )
        assert row is not None
        assert row.fabric_address is None and row.fabric_bandwidth_mbps is None
        assert row.gpu_memory_total_bytes == 1000

    # The mandatory core stays strict.
    broken_core = payload | {"disk_free_bytes": 5000, "observed_at": later}
    assert (
        client.post(
            "/agent/inventory",
            headers=agent_headers(NODE_A, "serial-a"),
            json=broken_core,
        ).status_code
        == 422
    )


def test_agent_4xx_logs_one_line_with_request_id_and_field(agent_system, caplog):
    client, _, _, clock = agent_system
    caplog.set_level("INFO")

    response = client.post(
        "/agent/inventory",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"schema_version": 1, "observed_at": clock.now.isoformat()},
    )

    assert response.status_code == 422
    lines = [
        r.getMessage()
        for r in caplog.records
        if "agent.request_rejected" in r.getMessage()
    ]
    assert len(lines) == 1
    assert "gpu_count" in lines[0] and "request_id" in lines[0]
    assert clock.now.isoformat() not in lines[0]


@pytest.mark.usefixtures("damaged_json_rows")
def test_reconciliation_identity_survives_agent_api_and_signed_grant(agent_system):
    client, services, _, clock = agent_system
    identity = RecipeReconciliationIdentity(
        installation_id="70000000-0000-4000-8000-000000000007",
        plan_digest="b" * 64,
    )
    job_id = str(uuid.uuid4())
    operation_id = str(uuid.uuid4())
    request = HostRuntimeRequest(
        action="installation-cleanup",
        fence=str(uuid.uuid4()),
        arguments=[],
        installation_id=identity.installation_id,
        reconciliation_identity=identity,
    )
    with services.sessions.begin() as session:
        session.add(
            Job(
                id=job_id,
                request_id=str(uuid.uuid4()),
                kind="recipe.reconcile",
                state="running",
                actor="admin",
                authority_revision="e" * 64,
                targets=[NODE_A],
                payload_digest="f" * 64,
                payload={"workload_intent_ordinal": 1},
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
        session.add(
            AgentOperation(
                id=operation_id,
                parent_job_id=job_id,
                node_id=NODE_A,
                kind="recipe.reconcile",
                payload=json.loads(canonical_message(identity)),
                payload_digest=hashlib.sha256(canonical_message(identity)).hexdigest(),
                authority_revision="e" * 64,
                workload_intent_ordinal=1,
                state="running",
                current_attempt=1,
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
        session.add(
            AgentOperationAttempt(
                id=str(uuid.uuid4()),
                operation_id=operation_id,
                attempt=1,
                fence=request.fence,
                lease_deadline=clock.now + timedelta(minutes=1),
                agent_certificate_serial="serial-a",
                state="running",
            )
        )
    signer = HostHelperGrantIssuer(ed25519.Ed25519PrivateKey.generate(), clock=clock)
    authority = HostRuntimeAuthorityService(services.sessions, signer, clock=clock)
    object.__setattr__(services, "host_runtime_authority", authority)
    body = {
        "fence": request.fence,
        "action": request.action,
        "installation_id": request.installation_id,
        "reconciliation_identity": json.loads(canonical_message(identity)),
        "request_sha256": hashlib.sha256(canonical_message(request)).hexdigest(),
        "expires_in_seconds": 30,
    }
    response = client.post(
        "/agent/host-runtime/grant",
        headers=agent_headers(NODE_A, "serial-a"),
        json=body,
    )
    assert response.status_code == 200, response.text
    grant = SignedHostHelperGrant.parse(response.json()["grant"])
    assert isinstance(grant.claims.operation, ExecuteContainerRuntimeRequestOperation)
    assert grant.claims.operation.reconciliation_identity == identity
    signer.public_key.verify(
        bytes.fromhex(grant.signature.value),
        host_helper_grant_signing_bytes(grant.claims),
    )
    assert (
        client.post(
            "/agent/host-runtime/grant",
            headers=agent_headers(NODE_B, "serial-b"),
            json=body,
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/agent/host-runtime/grant",
            headers=agent_headers(NODE_A, "serial-a"),
            json=body | {"reconciliation_identity": None},
        ).status_code
        == 409
    )


class _HostGrantKwargs(TypedDict):
    node_id: str
    fence: str
    action: ContainerRuntimeAction
    request_sha256: str
    start_plan_sha256: str | None
    stop_plan_sha256: str | None
    run_generation: int | None
    runtime_run_id: str | None
    runtime_target_id: str | None
    runtime_installation_id: str | None
    certificate_serial: str
    installation_id: str | None
    reconciliation_identity: RecipeReconciliationIdentity | None
    expires_in_seconds: int


class _HostUpgradeGrantKwargs(TypedDict):
    node_id: str
    fence: str
    package_sha256: str
    package_signature: str
    certificate_serial: str
    expires_in_seconds: int


def test_helper_json_routes_use_strict_wire_models_and_canonical_signed_outputs(
    agent_system,
) -> None:
    client, services, _, clock = agent_system
    request_id = "00000000-0000-4000-8000-000000000005"
    fence = "00000000-0000-4000-8000-000000000004"

    host_issuer = HostHelperGrantIssuer(
        ed25519.Ed25519PrivateKey.generate(),
        clock=clock,
        request_id_factory=lambda: uuid.UUID(request_id),
    )

    class RecordingHostAuthority:
        def __init__(self) -> None:
            self.grant_calls: list[Mapping[str, object]] = []
            self.upgrade_calls: list[Mapping[str, object]] = []

        def issue_grant(self, **kwargs: Unpack[_HostGrantKwargs]) -> object:
            self.grant_calls.append(kwargs)
            operation = ExecuteContainerRuntimeRequestOperation(
                type="execute-container-runtime-request",
                action=kwargs["action"].value,
                fence=kwargs["fence"],
                request_sha256=kwargs["request_sha256"],
                start_plan_sha256=kwargs["start_plan_sha256"],
                stop_plan_sha256=kwargs["stop_plan_sha256"],
                run_generation=kwargs["run_generation"],
                runtime_run_id=kwargs["runtime_run_id"],
                runtime_target_id=kwargs["runtime_target_id"],
                runtime_installation_id=kwargs["runtime_installation_id"],
            )
            return host_issuer.issue_grant(
                node_id=kwargs["node_id"],
                operation=operation,
                expires_in_seconds=kwargs["expires_in_seconds"],
            )

        def issue_agent_upgrade_grant(
            self, **kwargs: Unpack[_HostUpgradeGrantKwargs]
        ) -> object:
            self.upgrade_calls.append(kwargs)
            from .package_upgrade_fixtures import rollback_authority

            operation = InstallVonkDebOperation(
                type="install-vonk-deb",
                package_sha256=kwargs["package_sha256"],
                package_signature=kwargs["package_signature"],
                rollback=PackageRollbackAuthority.model_validate(rollback_authority()),
            )
            return host_issuer.issue_grant(
                node_id=kwargs["node_id"],
                operation=operation,
                expires_in_seconds=kwargs["expires_in_seconds"],
            )

    host = RecordingHostAuthority()
    object.__setattr__(services, "host_runtime_authority", host)
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    runtime_request = schemas["HostRuntimeGrantRequest"]
    runtime_properties = runtime_request["properties"]
    assert {
        "start_plan_sha256",
        "stop_plan_sha256",
        "run_generation",
        "runtime_run_id",
        "runtime_target_id",
        "runtime_installation_id",
    } <= runtime_properties.keys()
    assert not (
        {
            "start_plan_sha256",
            "stop_plan_sha256",
            "run_generation",
            "runtime_run_id",
            "runtime_target_id",
            "runtime_installation_id",
        }
        & set(runtime_request.get("required", ()))
    )
    headers = agent_headers(NODE_A, "serial-a")
    common = {
        "fence": fence,
        "expires_in_seconds": 30,
    }

    host_response = client.post(
        "/agent/host-runtime/grant",
        headers=headers,
        json=common
        | {
            "action": "start",
            "request_sha256": "a" * 64,
            "start_plan_sha256": "b" * 64,
            "run_generation": 1,
            "runtime_run_id": "10000000-0000-4000-8000-000000000001",
            "runtime_target_id": "10000000-0000-4000-8000-000000000001",
            "runtime_installation_id": "20000000-0000-4000-8000-000000000002",
        },
    )
    assert host_response.status_code == 200
    assert isinstance(
        SignedHostHelperGrant.parse(host_response.json()["grant"]),
        SignedHostHelperGrant,
    )
    assert host.grant_calls[0]["node_id"] == NODE_A
    assert host.grant_calls[0]["fence"] == fence
    assert host.grant_calls[0]["runtime_target_id"] == (
        "10000000-0000-4000-8000-000000000001"
    )

    upgrade_response = client.post(
        "/agent/agent-upgrade/grant",
        headers=headers,
        json=common
        | {
            "package_sha256": "b" * 64,
            "package_signature": "c" * 128,
        },
    )
    assert upgrade_response.status_code == 200
    assert isinstance(
        SignedHostHelperGrant.parse(upgrade_response.json()["grant"]),
        SignedHostHelperGrant,
    )

    assert (
        client.post(
            "/agent/host-runtime/grant",
            headers=headers,
            json=common
            | {
                "job_id": "00000000-0000-5000-8000-000000000002",
                "action": "start",
                "request_sha256": "a" * 64,
            },
        ).status_code
        == 422
    )
    assert len(host.grant_calls) == 1
    assert (
        client.post(
            "/agent/agent-upgrade/grant",
            headers=headers,
            json=common
            | {
                "attempt": "1",
                "package_sha256": "b" * 64,
                "package_signature": "c" * 128,
            },
        ).status_code
        == 422
    )
    assert len(host.upgrade_calls) == 1


def test_agent_posts_authenticated_complete_recipe_run_observation_snapshot(
    agent_system,
) -> None:
    client, _services, _, clock = agent_system
    payload = {"observed_at": clock.now.isoformat(), "runs": []}

    assert (
        client.post(
            "/agent/recipe-runs/observations",
            headers=agent_headers(NODE_A, "serial-a"),
            json=payload,
        ).status_code
        == 204
    )
    assert (
        client.post("/agent/recipe-runs/observations", json=payload).status_code == 401
    )
    assert (
        client.post(
            "/agent/recipe-runs/observations",
            headers=agent_headers(NODE_A, "serial-a"),
            json=payload | {"observed_at": clock.now.replace(tzinfo=None).isoformat()},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/agent/recipe-runs/observations",
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
    document, digest = _canonical_recipe_fixture("bundle-download")
    recipe = RecipeDefinition.model_validate(document)
    with services.sessions.begin() as session:
        session.add(
            CatalogDocument(
                id=recipe_id,
                kind="recipe",
                publisher="vonk-forge",
                slug="bundle-download",
                title=recipe.metadata.title,
                created_by="administrator",
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
        session.add(
            CatalogDocumentRevision(
                id=revision_id,
                document_id=recipe_id,
                kind="recipe",
                publisher="vonk-forge",
                slug="bundle-download",
                revision_number=1,
                schema_version=2,
                state="active",
                document=document,
                content_digest=digest,
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
                manifest=bundle.manifest.model_dump(mode="json"),
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
                policy_report=valid_policy_report(),
                plan={},
                created_at=clock.now,
                updated_at=clock.now,
            )
        )

    response = client.get(
        f"/agent/source-bundles/{bundle.sha256}",
        headers=agent_headers(NODE_A, "serial-a"),
    )
    assert response.status_code == 200
    assert response.content == bundle.archive
    assert response.headers["etag"] == f'"sha256:{bundle.sha256}"'
    assert (
        client.get(
            f"/agent/source-bundles/{bundle.sha256}",
            headers=agent_headers(NODE_B, "serial-b"),
        ).status_code
        == 404
    )
    assert client.get(f"/agent/source-bundles/{bundle.sha256}").status_code == 401


def test_builder_uploads_digest_verified_docker_archive_without_a_registry(
    agent_system,
) -> None:
    client, services, _, clock = agent_system
    recipe_id = str(uuid.uuid4())
    revision_id = str(uuid.uuid4())
    build_id = str(uuid.uuid4())
    payload = b"exact docker archive"
    layout_digest = hashlib.sha256(payload).hexdigest()
    image_digest = "sha256:" + "d" * 64
    document, digest = _canonical_recipe_fixture("image-upload")
    recipe = RecipeDefinition.model_validate(document)
    with services.sessions.begin() as session:
        session.add(
            CatalogDocument(
                id=recipe_id,
                kind="recipe",
                publisher="vonk-forge",
                slug="image-upload",
                title=recipe.metadata.title,
                created_by="administrator",
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
        session.add(
            CatalogDocumentRevision(
                id=revision_id,
                document_id=recipe_id,
                kind="recipe",
                publisher="vonk-forge",
                slug="image-upload",
                revision_number=1,
                schema_version=2,
                state="active",
                document=document,
                content_digest=digest,
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
                policy_report=valid_policy_report(),
                plan={},
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
    headers = agent_headers(NODE_A, "serial-a") | {
        "content-type": "application/x-tar",
        "x-vonk-image-digest": image_digest,
        "x-vonk-oci-layout-sha256": layout_digest,
        "x-vonk-image-bytes": str(len(payload)),
    }

    rejected = client.put(
        f"/agent/recipe-builds/{build_id}/image",
        headers=headers | {"content-type": "application/vnd.oci.image.layout.v1.tar"},
        content=payload,
    )
    assert rejected.status_code == 415

    route = f"/agent/recipe-builds/{build_id}/image"
    interrupted = client.put(
        route,
        headers=headers | {"content-length": str(len(payload))},
        content=payload[:7],
    )
    assert interrupted.status_code == 422
    cursor = client.head(route, headers=headers)
    assert cursor.status_code == 200
    assert cursor.headers["x-vonk-upload-offset"] == "7"
    assert cursor.headers["x-vonk-upload-complete"] == "false"
    wrong_identity = client.head(
        route, headers=headers | {"x-vonk-image-digest": "sha256:" + "e" * 64}
    )
    assert wrong_identity.headers["x-vonk-upload-offset"] == "0"
    assert client.put(route, headers=headers, content=payload).status_code == 409
    response = client.put(
        route, headers=headers | {"x-vonk-upload-offset": "7"}, content=payload[7:]
    )
    complete = client.head(route, headers=headers)
    assert complete.headers["x-vonk-upload-offset"] == str(len(payload))
    assert complete.headers["x-vonk-upload-complete"] == "true"

    assert response.status_code == 204
    # The upload waits beside the layered store until the worker converts it.
    upload = services.artifact_root / "image-cache" / layout_digest
    assert upload.read_bytes() == payload
    assert not (services.artifact_root / layout_digest).exists()
    with services.sessions() as session:
        build = session.get(RecipeBuild, build_id)
        assert build.image_digest == image_digest
        assert build.oci_layout_sha256 == layout_digest
        assert build.image_bytes == len(payload)
    assert (
        client.put(
            f"/agent/recipe-builds/{build_id}/image",
            headers=agent_headers(NODE_B, "serial-b")
            | {
                "content-type": "application/x-tar",
                "x-vonk-image-digest": image_digest,
                "x-vonk-oci-layout-sha256": layout_digest,
                "x-vonk-image-bytes": str(len(payload)),
            },
            content=payload,
        ).status_code
        == 404
    )


def test_recipe_image_upload_lock_preserves_interrupted_bytes(tmp_path) -> None:
    import os

    from vonk_control.agent_api import _prepare_recipe_image_upload

    descriptor, partial = _prepare_recipe_image_upload(tmp_path, "a" * 64)
    try:
        os.write(descriptor, b"partial archive")
        with pytest.raises(HTTPException) as rejected:
            _prepare_recipe_image_upload(tmp_path, "a" * 64)
        assert rejected.value.status_code == 409
        assert partial.read_bytes() == b"partial archive"
    finally:
        os.close(descriptor)
    resumed, resumed_path = _prepare_recipe_image_upload(tmp_path, "a" * 64)
    try:
        assert resumed_path == partial
        assert os.fstat(resumed).st_size == len(b"partial archive")
    finally:
        os.close(resumed)


def test_recipe_image_fsync_does_not_block_concurrent_agent_requests(
    agent_system, monkeypatch
) -> None:
    client, services, _, clock = agent_system
    recipe_id = str(uuid.uuid4())
    revision_id = str(uuid.uuid4())
    build_id = str(uuid.uuid4())
    payload = b"exact docker archive"
    layout_digest = hashlib.sha256(payload).hexdigest()
    document, digest = _canonical_recipe_fixture("nonblocking-image-upload")
    recipe = RecipeDefinition.model_validate(document)
    with services.sessions.begin() as session:
        session.add(
            CatalogDocument(
                id=recipe_id,
                kind="recipe",
                publisher="vonk-forge",
                slug="nonblocking-image-upload",
                title=recipe.metadata.title,
                created_by="administrator",
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
        session.add(
            CatalogDocumentRevision(
                id=revision_id,
                document_id=recipe_id,
                kind="recipe",
                publisher="vonk-forge",
                slug="nonblocking-image-upload",
                revision_number=1,
                schema_version=2,
                state="active",
                document=document,
                content_digest=digest,
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
                policy_report=valid_policy_report(),
                plan={},
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
    entered = Event()
    release = Event()
    health_completed = Event()
    real_fsync = os.fsync

    def slow_fsync(descriptor: int) -> None:
        entered.set()
        assert release.wait(timeout=60)
        real_fsync(descriptor)

    monkeypatch.setattr("vonk_control.agent_api.common.os.fsync", slow_fsync)
    headers = agent_headers(NODE_A, "serial-a") | {
        "content-type": "application/x-tar",
        "x-vonk-image-digest": "sha256:" + "d" * 64,
        "x-vonk-oci-layout-sha256": layout_digest,
        "x-vonk-image-bytes": str(len(payload)),
    }

    def observe_responsiveness() -> bool:
        assert entered.wait(timeout=30)
        responsive = health_completed.wait(timeout=5)
        release.set()
        return responsive

    async def exercise() -> tuple[Response, Response, bool]:
        async with AsyncClient(
            transport=ASGITransport(app=client.app), base_url="http://testserver"
        ) as async_client:

            async def health_request():
                response = await async_client.get("/api/healthz")
                health_completed.set()
                return response

            with ThreadPoolExecutor(max_workers=1) as pool:
                observer = pool.submit(observe_responsiveness)
                upload = asyncio.create_task(
                    async_client.put(
                        f"/agent/recipe-builds/{build_id}/image",
                        headers=headers,
                        content=payload,
                    )
                )
                health = asyncio.create_task(health_request())
                try:
                    upload_response, health_response = await asyncio.gather(
                        upload, health
                    )
                finally:
                    release.set()
                return upload_response, health_response, observer.result(timeout=60)

    upload_response, health_response, responsive = asyncio.run(exercise())

    assert responsive
    assert health_response.status_code == 200
    assert upload_response.status_code == 204


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


def assert_grant_available(services: AgentApiServices, token: str) -> None:
    from vonk_control.enrollment import _decode_token, _digest
    from vonk_control.models import AgentEnrollmentGrant

    assert services.enrollment is not None
    with services.enrollment._sessions() as session:
        grant = session.scalar(
            select(AgentEnrollmentGrant).where(
                AgentEnrollmentGrant.token_digest == _digest(_decode_token(token))
            )
        )
        assert grant is not None and grant.consumed_at is None


def assert_grant_unconsumed(services: AgentApiServices, token: str) -> None:
    from vonk_control.enrollment import _decode_token, _digest
    from vonk_control.models import AgentEnrollmentGrant

    with services.sessions() as session:
        grant = session.scalar(
            select(AgentEnrollmentGrant).where(
                AgentEnrollmentGrant.token_digest == _digest(_decode_token(token))
            )
        )
        assert grant is not None
        assert grant.consumed_at is None


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
        await asyncio.wait_for(app(scope, receive, send), timeout=5)
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


def test_spoofed_agent_header_is_rejected() -> None:
    app = create_app(
        jobs=Jobs(),
        tokens=TokenCodec(b"k" * 32),
    )

    response = TestClient(app).post(
        "/agent/claim", headers={"x-vonk-agent-node": NODE_A}
    )

    assert response.status_code == 401


def test_unauthenticated_agent_gate_returns_without_reading_request_body() -> None:
    app = create_app(
        jobs=Jobs(),
        tokens=TokenCodec(b"k" * 32),
    )
    sent: list[Message] = []
    body_reads = 0

    async def receive() -> dict[str, object]:
        nonlocal body_reads
        body_reads += 1
        return {"type": "http.request", "body": b"untrusted", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/agent/claim",
        "raw_path": b"/agent/claim",
        "query_string": b"",
        "headers": (),
        "client": ("untrusted", 1234),
        "server": ("testserver", 80),
        "root_path": "",
        "state": {},
    }

    asyncio.run(asyncio.wait_for(app(scope, receive, send), timeout=10))

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
    )

    response = TestClient(app).post(
        "/agent/claim", headers={"Authorization": "Bearer invalid"}
    )

    assert response.status_code == 401


def test_untrusted_proxy_and_malformed_forwarded_identity_are_rejected(
    agent_system,
) -> None:
    client, _, _, _ = agent_system
    assert client.post("/agent/claim").status_code == 401
    assert (
        client.post(
            "/agent/claim",
            headers={
                **agent_headers(NODE_A, "serial-a"),
                "x-vonk-agent-verified": "false",
            },
        ).status_code
        == 401
    )

    app = create_app(jobs=Jobs(), tokens=TokenCodec(b"k" * 32))
    assert (
        TestClient(app)
        .post("/agent/claim", headers=agent_headers(NODE_A, "serial-a"))
        .status_code
        == 401
    )


def test_claim_requires_a_trusted_policy_bounded_source(agent_system) -> None:
    client, services, _, _ = agent_system
    missing = agent_headers(NODE_A, "serial-a")
    missing.pop("x-vonk-agent-source")

    assert client.post("/agent/claim", headers=missing).status_code == 401
    outside = client.post(
        "/agent/claim",
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

    response = client.post("/agent/claim", headers=agent_headers(NODE_A, "serial-a"))

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

    response = client.post("/agent/claim", headers=agent_headers(NODE_A, "serial-a"))

    assert response.status_code == 204
    with services.sessions() as session:
        assert session.get(AgentPresence, NODE_A) is not None


@pytest.mark.parametrize(
    "hostname",
    ("", "-spark", "spark_3542", "spark 3542", "spark..lab", "a" * 256),
)
def test_authenticated_claim_drops_an_invalid_reported_hostname(
    agent_system, hostname: str
) -> None:
    """The hostname is a hint: a malformed one never costs the Spark its claim."""

    client, services, _, _clock = agent_system

    response = client.post(
        "/agent/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"hostname": hostname},
    )

    assert response.status_code == 204
    with services.sessions() as session:
        assert session.get(AgentPresence, NODE_A) is not None


@pytest.mark.parametrize(
    ("removed_field", "removed_value"),
    (
        ("active_slot", "B"),
        ("agent_sha256", "c" * 64),
        ("platform_version", "1.2.3"),
        ("supervisor_generation", 7),
        ("supervisor_ready_generation", 7),
        ("activation_deadline", "2026-08-20T12:00:00Z"),
    ),
)
def test_claim_rejects_retired_supervisor_identity_fields(
    agent_system, removed_field: str, removed_value: object
) -> None:
    client, services, _, _clock = agent_system
    runtime_identity: dict[str, object] = {
        "architecture": "linux-arm64",
        "binary_digest": "c" * 64,
        "build_digest": "sha256:" + "c" * 64,
        "semantic_version": "1.2.3",
        removed_field: removed_value,
    }

    response = client.post(
        "/agent/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"runtime_identity": runtime_identity},
    )

    assert response.status_code == 422
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.semantic_version is None


@pytest.mark.parametrize("architecture", ("linux-amd64", "linux-arm64"))
def test_claim_accepts_independently_valid_packaged_build_and_binary_digests(
    agent_system, architecture: str
) -> None:
    client, _services, _, _clock = agent_system

    response = client.post(
        "/agent/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={
            "runtime_identity": {
                "architecture": architecture,
                "binary_digest": "c" * 64,
                "build_digest": "sha256:" + "b" * 64,
                "semantic_version": "1.2.3",
            },
        },
    )

    assert response.status_code == 204


def test_authenticated_claim_requires_packaged_runtime_identity(agent_system) -> None:
    client, _services, _, _clock = agent_system

    response = client.request(
        "POST",
        "/agent/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"protocol_version": 4},
    )

    assert response.status_code == 422


@pytest.mark.parametrize("architecture", ("linux-riscv64", True, 7))
def test_claim_api_rejects_noncanonical_runtime_architecture(
    agent_system, architecture: object
) -> None:
    client, services, _, _ = agent_system

    response = client.post(
        "/agent/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={
            "runtime_identity": {
                "architecture": architecture,
                "binary_digest": "c" * 64,
                "build_digest": "sha256:" + "c" * 64,
                "semantic_version": "1.2.3",
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
        "/agent/claim",
        json={
            "runtime_identity": {
                "architecture": "linux-arm64",
                "binary_digest": "c" * 64,
                "build_digest": "sha256:" + "b" * 64,
                "semantic_version": "1.2.3",
            },
        },
    )

    assert response.status_code in {401, 403}
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.architecture is None


@pytest.mark.parametrize(
    ("field", "nested"),
    (("future_claim_field", False), ("future_attestation", True)),
)
def test_claim_rejects_unknown_structural_fields(
    agent_system,
    field: str,
    nested: bool,
) -> None:
    client, _services, _, _clock = agent_system
    payload = {
        "protocol_version": 4,
        "runtime_identity": PACKAGED_RUNTIME_IDENTITY,
    }
    if nested:
        payload["runtime_identity"] = {
            **PACKAGED_RUNTIME_IDENTITY,
            field: {"format": "v2"},
        }
    else:
        payload[field] = {"version": 4}

    response = client.post(
        "/agent/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json=payload,
    )

    assert response.status_code == 422


@pytest.mark.parametrize("field", ("protocol_version", "wait_seconds"))
@pytest.mark.usefixtures("damaged_json_rows")
def test_claim_rejects_string_encoded_numeric_fields(
    agent_system,
    field: str,
) -> None:
    client, _services, _, _clock = agent_system
    response = client.post(
        "/agent/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={
            "protocol_version": 4,
            "runtime_identity": PACKAGED_RUNTIME_IDENTITY,
            field: "30",
        },
    )

    assert response.status_code == 422


def test_authenticated_heartbeat_preserves_claim_advertised_protocol_after_exact_fence_validation(
    agent_system,
    monkeypatch,
) -> None:
    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.stop",
        "a" * 64,
        STOP_PAYLOAD,
    )
    claim = client.post(
        "/agent/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"protocol_version": 4},
    ).json()
    with services.sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.last_seen_at = None
    clock.now += timedelta(seconds=5)
    progress = {key: claim[key] for key in ("fence",)} | {
        "progress": {"phase": "checking"}
    }

    def reject_post_commit(_source) -> None:
        raise AssertionError("presence must be written inside the queue transaction")

    monkeypatch.setattr(services.presence, "observe", reject_post_commit)

    response = client.post(
        "/agent/heartbeat",
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
        assert node.protocol_version == 4
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
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.stop",
        "a" * 64,
        STOP_PAYLOAD,
    )
    claim = client.post(
        "/agent/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"protocol_version": 4},
    ).json()
    with services.sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        node.last_seen_at = None
    clock.now += timedelta(seconds=5)
    result = {key: claim[key] for key in ("fence",)} | {
        "state": "succeeded",
        "result": {},
    }

    def reject_post_commit(_source) -> None:
        raise AssertionError("presence must be written inside the queue transaction")

    monkeypatch.setattr(services.presence, "observe", reject_post_commit)

    response = client.post(
        "/agent/result",
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
        assert node.protocol_version == 4
        presence = session.get(AgentPresence, NODE_A)
        assert presence is not None
        assert presence.management_address == "10.0.0.44"
        assert presence.observed_at.replace(tzinfo=UTC) == clock.now


def test_failed_stop_result_never_writes_health_observation(agent_system) -> None:
    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.stop",
        "a" * 64,
        STOP_PAYLOAD,
    )
    claim = client.post(
        "/agent/claim", headers=agent_headers(NODE_A, "serial-a")
    ).json()
    result = {key: claim[key] for key in ("fence",)} | {
        "state": "failed",
        "result": {"status": "failed", "error_code": "stop_failed"},
    }

    assert (
        client.post(
            "/agent/result",
            headers=agent_headers(NODE_A, "serial-a"),
            json=result,
        ).status_code
        == 204
    )


def test_untrusted_and_stale_requests_do_not_record_agent_contact(agent_system) -> None:
    client, services, _, clock = agent_system
    untrusted = client.post(
        "/agent/claim",
        json={
            "protocol_version": 4,
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
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.stop",
        "a" * 64,
        STOP_PAYLOAD,
    )
    client.post("/agent/claim", headers=agent_headers(NODE_A, "serial-a")).json()
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
        "fence": str(uuid.uuid4()),
        "state": "succeeded",
        "result": {},
    }

    rejected = client.post(
        "/agent/result",
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


@pytest.mark.parametrize("version", (True, 3, 5))
def test_unsupported_protocol_version_is_rejected_without_recording_contact(
    agent_system, version: object
) -> None:
    client, services, _, _ = agent_system

    response = client.post(
        "/agent/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={
            "protocol_version": version,
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
            "/agent/claim", headers=agent_headers(NODE_A, "serial-a")
        ).status_code
        == 401
    )


def test_fence_and_cross_node_result_updates_are_denied(agent_system) -> None:
    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.stop",
        "a" * 64,
        STOP_PAYLOAD,
    )
    claim = client.post(
        "/agent/claim", headers=agent_headers(NODE_A, "serial-a")
    ).json()
    result = {key: claim[key] for key in ("fence",)}
    foreign = {**result, "state": "succeeded", "result": {}}
    assert (
        client.post(
            "/agent/result", headers=agent_headers(NODE_B, "serial-b"), json=foreign
        ).status_code
        == 422
    )
    stale = {
        **result,
        "fence": str(uuid.uuid4()),
        "state": "succeeded",
        "result": {},
    }
    assert (
        client.post(
            "/agent/result", headers=agent_headers(NODE_A, "serial-a"), json=stale
        ).status_code
        == 409
    )


def test_expired_exact_result_is_retained_as_diagnostic_without_completing_job(
    agent_system,
) -> None:
    client, services, _, clock = agent_system
    job = parent(services.sessions, clock)
    services.operations.enqueue(job.id, NODE_A, "recipe.stop", "a" * 64, STOP_PAYLOAD)
    claim = client.post(
        "/agent/claim", headers=agent_headers(NODE_A, "serial-a")
    ).json()
    clock.now += timedelta(seconds=61)
    result = {key: claim[key] for key in ("fence",)} | {
        "state": "succeeded",
        "result": {},
    }

    response = client.post(
        "/agent/result", headers=agent_headers(NODE_A, "serial-a"), json=result
    )
    assert response.status_code == 202
    with services.sessions() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim["fence"]
            )
        )
        assert aos.attempt_lapsed(attempt)
        assert attempt.result == {}
        assert session.get(Job, job.id).state != "succeeded"


def test_public_enrollment_bootstrap_is_canonical_bounded_and_contains_only_public_trust(
    agent_system,
) -> None:
    client, services, _, _ = agent_system
    assert services.bootstrap is not None
    object.__setattr__(
        services,
        "host_runtime_authority",
        SimpleNamespace(public_key_document={"public_key": "11" * 32}),
    )

    response = client.get("/agent/bootstrap")

    assert response.status_code == 200
    assert response.content == canonical_message(response.json())
    assert response.json() == {
        "ca_fingerprint": services.bootstrap.ca_fingerprint,
        "ca_pem": services.bootstrap.ca_pem,
        "host_helper_authority_public_key": "11" * 32,
        "controller_endpoint": "https://agents.example.test:8443",
        "enrollment_endpoint": "https://enroll.example.test:8443",
        "controller_address": "192.168.1.231",
        "service_hostnames": [
            "control.example.test",
            "enroll.example.test",
            "agents.example.test",
            "registry.example.test",
        ],
    }
    assert len(response.content) < 64 * 1024
    assert "PRIVATE KEY" not in response.text


def test_bootstrap_has_one_current_response_even_with_an_obsolete_query(
    agent_system,
) -> None:
    client, services, _, _ = agent_system

    object.__setattr__(
        services,
        "host_runtime_authority",
        SimpleNamespace(public_key_document={"public_key": "11" * 32}),
    )

    current = client.get("/agent/bootstrap")
    setup = client.get("/agent/bootstrap?setup_schema=1")

    assert current.status_code == setup.status_code == 200
    assert current.json() == setup.json()
    assert setup.json()["host_helper_authority_public_key"] == "11" * 32
    assert setup.content == canonical_message(setup.json())
    assert "PRIVATE KEY" not in setup.text


def test_bootstrap_requires_the_host_helper_authority(
    agent_system,
) -> None:
    client, _, _, _ = agent_system

    response = client.get("/agent/bootstrap")

    assert response.status_code == 503
    assert response.json() == {"detail": "host runtime authority is unavailable"}


@pytest.mark.usefixtures("damaged_json_rows")
def test_recipe_run_disposition_names_every_run_the_controller_does_not_want(
    agent_system,
) -> None:
    # Only a run the Controller can still stop is wanted. A run it never
    # owned, and a run it knows but failed or stopped, are both named unowned
    # so the agent retires its local claim instead of keeping it forever.
    client, services, _, clock = agent_system
    known_run_id = "70000000-0000-4000-8000-000000000071"
    with services.sessions.begin() as session:
        session.add(
            RecipeRun(
                id=known_run_id,
                installation_id="80000000-0000-4000-8000-000000000008",
                mapping_id="90000000-0000-4000-8000-000000000009",
                mapping_generation=1,
                run_generation=1,
                alias="known-exact",
                plan_digest="1" * 64,
                plan={"kind": "old"},
                state="failed",
                route_state="withdrawn",
                actor="admin",
                created_at=clock.now,
                updated_at=clock.now,
            )
        )

    def disposition(run_id: str, headers: dict[str, str] | None = None):
        return client.get(
            f"/agent/recipe-runs/{run_id}/disposition",
            headers=agent_headers(NODE_A, "serial-a") if headers is None else headers,
        )

    def in_state(state: str, generation: int = 1):
        with services.sessions.begin() as session:
            run = session.get(RecipeRun, known_run_id)
            assert run is not None
            run.state = state
            run.run_generation = generation
        return disposition(known_run_id)

    never_owned = disposition("e85c4710-e437-4d12-8191-499596aa2a4c")
    assert never_owned.status_code == 204
    assert never_owned.headers["x-vonk-recipe-run-disposition"] == "unowned"
    for state in ("failed", "stopped"):
        not_wanted = in_state(state)
        assert not_wanted.status_code == 204
        assert not_wanted.headers["x-vonk-recipe-run-disposition"] == "unowned"
        assert "x-vonk-recipe-run-generation" not in not_wanted.headers
    for state in ("planned", "starting", "stopping"):
        wanted = in_state(state)
        assert "x-vonk-recipe-run-disposition" not in wanted.headers
        assert "x-vonk-recipe-run-generation" not in wanted.headers
    # A lost run's generation lets the agent report its process gone, which
    # releases the claims of a cancelled start.
    lost = in_state("lost", generation=2)
    assert "x-vonk-recipe-run-disposition" not in lost.headers
    assert lost.headers["x-vonk-recipe-run-generation"] == "2"
    running = in_state("running", generation=4)
    assert "x-vonk-recipe-run-disposition" not in running.headers
    assert running.headers["x-vonk-recipe-run-generation"] == "4"
    assert disposition("E85C4710-not-a-run").status_code == 422
    assert disposition("e85c4710-e437-4d12-8191-499596aa2a4c", {}).status_code == 401


@pytest.mark.usefixtures("damaged_json_rows")
def test_recipe_run_observation_report_applies_process_state_per_run(
    agent_system,
) -> None:
    client, services, _, clock = agent_system
    run_id = "70000000-0000-4000-8000-000000000007"
    with services.sessions.begin() as session:
        session.add(
            RecipeRun(
                id=run_id,
                installation_id="80000000-0000-4000-8000-000000000008",
                mapping_id="90000000-0000-4000-8000-000000000009",
                mapping_generation=1,
                run_generation=2,
                alias="observed",
                plan_digest="1" * 64,
                plan={"schema_version": 1, "observation_schema_version": 2},
                state="running",
                route_state="withdrawn",
                actor="admin",
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
        session.add(
            RunNode(
                run_id=run_id,
                node_id=NODE_A,
                rank=0,
                role="worker",
                state="running",
                port=8888,
                reserved_memory_bytes=1,
                updated_at=clock.now - timedelta(seconds=1),
            )
        )

    def report(*runs: dict[str, object], observed_at: datetime = clock.now):
        return client.post(
            "/agent/recipe-runs/observations",
            headers=agent_headers(NODE_A, "serial-a"),
            json={"observed_at": observed_at.isoformat(), "runs": list(runs)},
        )

    from vonk_agent_protocol.failure_evidence import (
        FailureDiagnostics,
        FailureLogTail,
        FailureProperty,
    )

    empty = FailureLogTail(text="", truncated=False, dropped_bytes=0, dropped_lines=0)
    diagnostics = FailureDiagnostics(
        collected_at=clock.now.isoformat(),
        phase="workload.host_memory_guard",
        category="capacity",
        stdout=empty,
        stderr=empty,
        versions=[],
        sandbox=[],
        storage=[],
        collector_errors=[],
        preflight=[
            FailureProperty(name="reason", value="workload.host_memory_exhausted"),
            FailureProperty(name="exit_cause", value="host_memory_exhausted"),
            FailureProperty(name="mem_available_bytes", value="1"),
        ],
    )
    run: dict[str, object] = {
        "run_id": run_id,
        "run_generation": 2,
        "process_running": False,
        "endpoint_ready": None,
        "failure_diagnostics": diagnostics.model_dump(mode="json", exclude_none=True),
    }
    stale_generation = report(run | {"run_generation": 1})
    assert stale_generation.status_code == 422
    assert "generation is stale" in stale_generation.text
    # A report that crossed the Controller's own change (the run left this
    # Spark, or the rank changed after the report was taken) is expected:
    # it is dropped without error and never overwrites the rank.
    assert report(run | {"run_id": str(uuid.uuid4())}).status_code == 204
    assert report(run, observed_at=clock.now - timedelta(seconds=2)).status_code == 204
    with services.sessions() as session:
        node = session.scalar(select(RunNode).where(RunNode.run_id == run_id))
        assert node is not None and node.observation_observed_at is None

    assert report(run | {"run_id": str(uuid.uuid4())}, run).status_code == 204
    with services.sessions() as session:
        node = session.scalar(select(RunNode).where(RunNode.run_id == run_id))
        assert node is not None
        assert node.state == "failed"
        assert node.observed_run_generation == 2
        assert node.observation_process_running is False
        assert node.observation_observed_at is not None
        # Wrong implementation: background inspection drops the safety cause,
        # leaving a hardware OOM indistinguishable from an ordinary exit.
        restored = FailureDiagnostics.model_validate_json(
            json.dumps(node.observation_failure_diagnostics)
        )
        assert restored == diagnostics

    # New live evidence clears the old failure; it cannot poison a recovered run.
    recovered_at = clock.now + timedelta(seconds=1)
    assert (
        report(
            run | {"process_running": True, "failure_diagnostics": None},
            observed_at=recovered_at,
        ).status_code
        == 204
    )
    with services.sessions() as session:
        node = session.scalar(select(RunNode).where(RunNode.run_id == run_id))
        assert node is not None
        assert node.state == RunState.RUNNING
        assert node.observation_failure_diagnostics is None
        assert node.observation_process_running is True
        assert node.observation_observed_at is not None
        assert node.observation_observed_at.replace(tzinfo=UTC) == recovered_at

    # A delayed replay of the failure cannot undo the newer live observation.
    assert report(run).status_code == 204
    with services.sessions() as session:
        node = session.scalar(select(RunNode).where(RunNode.run_id == run_id))
        assert node is not None
        assert node.state == RunState.RUNNING
        assert node.observation_failure_diagnostics is None
        assert node.observation_process_running is True
        assert node.observation_observed_at is not None
        assert node.observation_observed_at.replace(tzinfo=UTC) == recovered_at


def test_rust_agent_enrollment_shape_remains_controller_compatible(
    agent_system,
) -> None:
    client, services, _, _ = agent_system
    fixture = json.loads(
        (
            Path(__file__).parents[2]
            / "agent_protocol/fixtures/enrollment-request.json"
        ).read_text()
    )
    body = json.loads(valid_enrollment_body(enrollment_grant(services)))

    assert set(body) == set(fixture) == {"csr", "evidence", "grant_token"}
    assert set(body["evidence"]) == set(fixture["evidence"])
    response = client.post("/agent/enroll", json=body)

    assert response.status_code == 200
    assert response.json()["node_id"] == NODE_C


def test_uncertain_enrollment_provider_write_returns_503_without_reissuing(
    agent_system, monkeypatch
) -> None:
    client, services, _, _ = agent_system
    calls = 0

    def fail_issue(
        _node_id: str,
        _csr: bytes,
        _now: datetime,
        *,
        request: CertificateIssuanceBinding,
    ) -> IssuedCertificate:
        nonlocal calls
        services.enrollment._authority._begin(request)
        calls += 1
        raise RuntimeError("provider response lost")

    monkeypatch.setattr(services.enrollment._authority, "issue_node", fail_issue)
    body = json.loads(valid_enrollment_body(enrollment_grant(services)))

    first = client.post("/agent/enroll", json=body)
    replay = client.post("/agent/enroll", json=body)

    assert first.status_code == replay.status_code == 503
    for response in (first, replay):
        EnrollmentObservationReply.model_validate_json(response.content)
        assert 0 < int(response.headers["Retry-After"]) <= 300
        assert response.headers["Cache-Control"] == "no-store"
    assert calls == 1


def test_exact_enrollment_replay_returns_certificate_and_mismatch_is_denied(
    agent_system,
) -> None:
    client, services, _, _ = agent_system
    grant = services.enrollment.create(NODE_C, "administrator", 60)
    assert isinstance(grant, EnrollmentGrant)
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
    issued = client.post("/agent/enroll", json=body)
    pickup = client.post("/agent/enroll", json=body)
    mismatch = client.post(
        "/agent/enroll",
        json={**body, "evidence": {**body["evidence"], "boot_id": "different"}},
    )

    assert issued.status_code == pickup.status_code == 200
    assert issued.content == pickup.content
    assert pickup.content == canonical_message(pickup.json())
    assert pickup.json()["generation"] == 1
    assert "certificate_pem" in pickup.json()
    assert mismatch.status_code == 200
    assert mismatch.content == issued.content


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
    monkeypatch,
) -> None:
    from vonk_agent_protocol.enrollment import IssuedCertificateResponse
    from vonk_control.models import AgentCertificateRotation

    client, services, _, clock = agent_system
    with services.sessions.begin() as session:
        source = session.scalar(
            select(AgentCertificate).where(AgentCertificate.serial == "serial-a")
        )
        assert source is not None
        source.serial = "101"
        source.fingerprint = "fingerprint-101"
    request = _csr_for(NODE_A)
    claim = services.enrollment._claim_rotation(
        NODE_A, "101", request, _csr_fingerprint(request), clock.now
    )
    assert not isinstance(claim, IssuedCertificate)
    assert claim.provider_request is not None
    authority = services.enrollment._authority
    authority._begin(claim.provider_request)
    attempts = []

    observe = authority.observe_node

    def observed(*args, request, **kwargs):
        attempts.append(request)
        return observe(*args, request=request, **kwargs)

    def forbidden_issue(*_args, **_kwargs):
        pytest.fail("a pending observation cannot become another issuance call")

    monkeypatch.setattr(authority, "observe_node", observed)
    monkeypatch.setattr(authority, "renew_node", forbidden_issue)

    response = client.post(
        "/agent/renew",
        headers=agent_headers(NODE_A, "101"),
        json={"node_id": NODE_A, "csr": request.decode()},
    )

    assert attempts == [claim.provider_request] * 4
    assert response.status_code == 503
    assert response.content == canonical_message(response.json())
    EnrollmentObservationReply.model_validate_json(response.content)
    assert 0 < int(response.headers["Retry-After"]) <= 300

    with services.sessions() as session:
        assert session.get(AgentCertificateRotation, NODE_A) is not None
        assert session.get(AgentCertificate, "101").revoked_at is None
    clock.now += timedelta(seconds=301)
    services.enrollment.reconcile_revocations()
    with services.sessions() as session:
        assert session.get(AgentCertificateRotation, NODE_A) is None
    monkeypatch.undo()
    fresh = client.post(
        "/agent/renew",
        headers=agent_headers(NODE_A, "101"),
        json={"node_id": NODE_A, "csr": _csr_for(NODE_A).decode()},
    )
    assert fresh.status_code == 200
    IssuedCertificateResponse.model_validate_json(fresh.content)


def test_staged_certificate_can_only_activate_and_activation_is_idempotent_after_response_loss(
    agent_system,
) -> None:
    client, services, _, _ = agent_system
    with services.enrollment._sessions.begin() as session:
        source = session.get(AgentCertificate, "serial-a")
        assert source is not None
        source.serial = "101"
        source.fingerprint = "fingerprint-101"
    csr = _csr_for(NODE_A)
    first = client.post(
        "/agent/renew",
        headers=agent_headers(NODE_A, "101"),
        json={"node_id": NODE_A, "csr": csr.decode()},
    )
    replay = client.post(
        "/agent/renew",
        headers=agent_headers(NODE_A, "101"),
        json={"node_id": NODE_A, "csr": csr.decode()},
    )
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    issued = first.json()
    staged_headers = agent_headers(NODE_A, issued["serial"])
    staged_headers["x-vonk-agent-fingerprint"] = issued["fingerprint"]

    assert client.post("/agent/claim", headers=staged_headers).status_code == 401
    assert (
        client.post(
            "/agent/heartbeat", headers=staged_headers, json={"invalid": True}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/agent/result", headers=staged_headers, json={"invalid": True}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/agent/renew",
            headers=staged_headers,
            json={"node_id": NODE_A, "csr": _csr_for(NODE_A).decode()},
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/agent/artifacts/" + "a" * 64,
            headers=staged_headers,
        ).status_code
        == 401
    )

    activation = {"node_id": NODE_A, "generation": issued["generation"]}
    assert (
        client.post(
            "/agent/renew/activate", headers=staged_headers, json=activation
        ).status_code
        == 204
    )
    assert (
        client.post(
            "/agent/renew/activate", headers=staged_headers, json=activation
        ).status_code
        == 204
    )
    assert (
        client.post("/agent/claim", headers=agent_headers(NODE_A, "101")).status_code
        == 401
    )
    assert client.post("/agent/claim", headers=staged_headers).status_code == 204
    with services.sessions() as session:
        old = session.get(AgentCertificate, "101")
        new = session.get(AgentCertificate, issued["serial"])
        assert old is not None and old.state == "revoked" and old.revoked_at is not None
        assert new is not None and new.state == "active" and new.revoked_at is None


@pytest.mark.parametrize("with_diagnostics", [False, True])
def test_failed_result_preserves_canonical_evidence_and_maps_parent_reason(
    agent_system,
    with_diagnostics,
) -> None:
    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.stop",
        "a" * 64,
        STOP_PAYLOAD,
    )
    claimed = client.post("/agent/claim", headers=agent_headers(NODE_A, "serial-a"))
    assert claimed.status_code == 200
    claim = claimed.json()
    result = {key: claim[key] for key in ("fence",)} | {
        "state": "failed",
        "result": {"status": "failed", "error_code": "stop_failed"},
    }
    if with_diagnostics:
        import json
        from pathlib import Path

        fixture = (
            Path(__file__).parents[2]
            / "agent_protocol/src/vonk_agent_protocol/vectors/failure-diagnostics-v1.json"
        )
        diagnostics = json.loads(fixture.read_text())
        diagnostics["stderr"]["text"] = (
            "Authorization: Bearer should-never-persist\n/proc: permission denied"
        )
        result["result"]["diagnostics"] = diagnostics

    response = client.post(
        "/agent/result", headers=agent_headers(NODE_A, "serial-a"), json=result
    )

    assert response.status_code == 204
    with services.sessions() as session:
        attempt = (
            session.query(AgentOperationAttempt).filter_by(fence=claim["fence"]).one()
        )
        parent_job = session.get(
            Job, fenced_operation(services.sessions, claim["fence"]).parent_job_id
        )
        assert attempt.result["status"] == "failed"
        assert attempt.result["error_code"] == "stop_failed"
        if with_diagnostics:
            from vonk_agent_protocol import FailureDiagnostics

            typed = FailureDiagnostics.model_validate(attempt.result["diagnostics"])
            assert "/proc: permission denied" in typed.stderr.text
            assert "should-never-persist" not in typed.model_dump_json()
        assert parent_job is not None and parent_job.status_reason == "stop_failed"
    if with_diagnostics:
        from vonk_control.failure_evidence import FailureEvidenceService

        evidence = FailureEvidenceService(services.sessions, clock=clock)
        fenced = fenced_attempt(services.sessions, claim["fence"])
        bundle = evidence.read(fenced.operation_id, fenced.attempt)
        assert "should-never-persist" not in bundle.model_dump_json()
        assert "/proc: permission denied" in bundle.diagnostics.stderr.text


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    (
        # The accepted boundary is the longest stable code the shared rule
        # allows; a shorter conventional code is the same path.
        ({"status": "failed", "error_code": "a" * 64}, 204),
        ({"status": "failed", "error_code": "a" * 65}, 422),
        ({"status": "failed", "error_code": "stop.failed"}, 422),
        ({"status": "failed", "error_code": "Stop_failed"}, 422),
        # A failed envelope that omits its failed status cannot identify the
        # failure and is refused by the same contextual rule.
        ({"error_code": "stop_failed"}, 422),
    ),
)
def test_failed_result_error_code_obeys_the_shared_contract_rule(
    agent_system, failure, expected_status
) -> None:
    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.stop",
        "a" * 64,
        STOP_PAYLOAD,
    )
    claimed = client.post("/agent/claim", headers=agent_headers(NODE_A, "serial-a"))
    assert claimed.status_code == 200
    claim = claimed.json()
    result = {key: claim[key] for key in ("fence",)} | {
        "state": "failed",
        "result": failure,
    }

    response = client.post(
        "/agent/result", headers=agent_headers(NODE_A, "serial-a"), json=result
    )

    assert response.status_code == expected_status


def test_failed_result_rejection_names_the_failing_field_and_rule(
    agent_system,
) -> None:
    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.stop",
        "a" * 64,
        STOP_PAYLOAD,
    )
    claim = client.post(
        "/agent/claim", headers=agent_headers(NODE_A, "serial-a")
    ).json()
    envelope = {key: claim[key] for key in ("fence",)} | {"state": "failed"}

    malformed = client.post(
        "/agent/result",
        headers=agent_headers(NODE_A, "serial-a"),
        json=envelope | {"result": {"status": "failed", "error_code": "stop.failed"}},
    )

    assert malformed.status_code == 422
    assert any(
        issue["type"] == "string_pattern_mismatch" and issue["loc"][-1] == "error_code"
        for issue in malformed.json()["issues"]
    )

    unnamed = client.post(
        "/agent/result",
        headers=agent_headers(NODE_A, "serial-a"),
        json=envelope | {"result": {"error_code": "stop_failed"}},
    )

    assert unnamed.status_code == 422
    assert any(
        "stable error_code" in issue["msg"] for issue in unnamed.json()["issues"]
    )


# The failure envelope spark-3542's agent retained for the artifact
# distribution whose report the Controller refused.  ``failure_kind`` is a
# declared string enum and the diagnostics list sits on its declared bound.
INCIDENT_DISTRIBUTION_FAILURE = {
    "status": "failed",
    "error_code": "artifact_distribution_failed",
    "failure_kind": "temporary-dependency",
    "reason": "Controller distribution could not be verified and retained",
    "diagnostics": {
        "schema_version": 1,
        "category": "runtime",
        "collected_at": "2026-09-16T23:29:02.483132899+00:00",
        "phase": "artifact.distribution.v1",
        "stdout": {
            "text": "",
            "truncated": False,
            "dropped_bytes": 0,
            "dropped_lines": 0,
        },
        "stderr": {
            "text": "",
            "truncated": False,
            "dropped_bytes": 0,
            "dropped_lines": 0,
        },
        "collector_errors": [],
        "preflight": [],
        "versions": [
            {"name": "agent", "value": "0.1.1"},
            {"name": "kernel", "value": "6.17.0-1031-nvidia"},
            {"name": "podman", "value": "podman version 4.9.3"},
        ],
        "storage": [
            {"name": "data-root", "value": "/var/lib/vonk-forge-agent"},
            {"name": "free-bytes", "value": "2483534802944"},
        ],
        "sandbox": [
            {"name": "Uid", "value": "128\t128\t128\t128"},
            {"name": "Gid", "value": "127\t127\t127\t127"},
            {"name": "CapEff", "value": "0000000000000000"},
            {"name": "NoNewPrivs", "value": "0"},
            {"name": "Seccomp", "value": "2"},
            {"name": "User", "value": "vonk-agent"},
            {
                "name": "ReadWritePaths",
                "value": "/var/lib/vonk-forge-agent /var/lib/vonk-forge/incoming"
                " /run/vonk-forge-agent",
            },
            {
                "name": "ReadOnlyPaths",
                "value": "/etc/vonk-forge-agent /usr/lib/vonk-forge",
            },
            {"name": "PrivateTmp", "value": "yes"},
            {"name": "ProtectSystem", "value": "strict"},
            {"name": "NoNewPrivileges", "value": "no"},
            {"name": "RestrictNamespaces", "value": "no"},
        ],
    },
}


def test_declared_failure_kind_survives_agent_result_ingress(agent_system) -> None:
    """A truthful ``failure_kind`` must not be refused as an invalid request.

    ``failure_kind`` is a string enum in the published wire schema, and the
    agent sends the member value.  A body model that resolves an enum only from
    an enum instance rejects that value, so every replay of a genuine failure
    was answered 422 -- which is what left spark-3542's agent looping until
    systemd exhausted its restart limit -- and the retained evidence was never
    consumed.  Replay the retained envelope through the real ingress and require
    both acceptance and the preserved receipt.
    """

    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "artifact.distribution.v1",
        "a" * 64,
        {"plan_digest": "a" * 64},
    )
    claim = client.post(
        "/agent/claim", headers=agent_headers(NODE_A, "serial-a")
    ).json()
    envelope = {key: claim[key] for key in ("fence",)} | {
        "state": "failed",
        "result": INCIDENT_DISTRIBUTION_FAILURE,
    }

    response = client.post(
        "/agent/result", headers=agent_headers(NODE_A, "serial-a"), json=envelope
    )

    assert response.status_code == 204, response.text
    with services.sessions() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.fence == claim["fence"]
            )
        )
        assert attempt is not None
        assert attempt.state == "failed"
        assert attempt.result["failure_kind"] == "temporary-dependency"
        assert attempt.result["error_code"] == "artifact_distribution_failed"
        # The bound diagnostics survive sanitization rather than being dropped.
        assert attempt.result["diagnostics"]["phase"] == "artifact.distribution.v1"
        assert len(attempt.result["diagnostics"]["sandbox"]) == 12


def test_boundary_failures_record_a_correlated_operator_reason(agent_system) -> None:
    """A refused result or heartbeat must leave a bounded, correlated reason.

    The claim path already explained its refusals, but a refused heartbeat or
    result persisted nothing, so an operator saw an operation that stopped
    progressing with no cause at all.
    """

    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.stop",
        "a" * 64,
        STOP_PAYLOAD,
    )
    claim = client.post(
        "/agent/claim", headers=agent_headers(NODE_A, "serial-a")
    ).json()
    envelope = {key: claim[key] for key in ("fence",)}

    rejected = client.post(
        "/agent/result",
        headers=agent_headers(NODE_A, "serial-a"),
        json=envelope | {"state": "succeeded", "result": {"installed_bytes": 0}},
    )

    assert rejected.status_code == 422
    with services.sessions() as session:
        fenced = fenced_operation(services.sessions, claim["fence"])
        operation = session.get(AgentOperation, fenced.id)
        job = session.get(Job, fenced.parent_job_id)
        assert operation is not None and operation.status_reason is not None
        assert operation.status_reason.startswith("result refused: invalid-result")
        assert "attempt=1" in operation.status_reason
        assert job is not None and job.status_reason == operation.status_reason

    clock.now += timedelta(seconds=61)
    heartbeat = client.post(
        "/agent/heartbeat",
        headers=agent_headers(NODE_A, "serial-a"),
        json=envelope | {"progress": {"phase": "stopping"}},
    )

    assert heartbeat.status_code == 409
    with services.sessions() as session:
        operation = session.get(
            AgentOperation, fenced_operation(services.sessions, claim["fence"]).id
        )
        assert operation is not None and operation.status_reason is not None
        assert operation.status_reason.startswith("heartbeat refused: stale-attempt")
        assert "attempt=1" in operation.status_reason


def test_a_concluded_operation_never_acquires_a_boundary_refusal_note(
    agent_system,
) -> None:
    """A refusal must never be attached to an outcome that already concluded.

    Wrong implementation: ``_write_refusal_note`` keyed only on the submission's
    correlating attempt and fence, so a late heartbeat for an operation that had
    already succeeded wrote ``heartbeat refused: stale-attempt`` onto the
    succeeded operation and its succeeded parent job.  The job then reported a
    refusal while its owning state said ``succeeded`` -- a success that reads to
    an operator as a refusal of work that already completed.
    """

    client, services, _, clock = agent_system
    job = parent(services.sessions, clock)
    operation = services.operations.enqueue(
        job.id, NODE_A, "recipe.stop", "a" * 64, STOP_PAYLOAD
    )
    claim = client.post(
        "/agent/claim", headers=agent_headers(NODE_A, "serial-a")
    ).json()
    envelope = {key: claim[key] for key in ("fence",)}
    completed = client.post(
        "/agent/result",
        headers=agent_headers(NODE_A, "serial-a"),
        json=envelope | {"state": "succeeded", "result": {}},
    )
    assert completed.status_code == 204, completed.text
    with services.sessions() as session:
        concluded = session.get(AgentOperation, operation.id)
        succeeded = session.get(Job, job.id)
        assert concluded is not None and concluded.state == "succeeded"
        assert succeeded is not None and succeeded.state == "succeeded"

    stale = client.post(
        "/agent/heartbeat",
        headers=agent_headers(NODE_A, "serial-a"),
        json=envelope | {"progress": {"phase": "stopping"}},
    )
    assert stale.status_code == 409, stale.text
    with services.sessions() as session:
        concluded = session.get(AgentOperation, operation.id)
        succeeded = session.get(Job, job.id)
        assert concluded is not None and concluded.state == "succeeded"
        assert succeeded is not None and succeeded.state == "succeeded"
        # The parent job is the owner surface the operator reads; it must not
        # carry a refusal for work that already reached its outcome.
        assert succeeded.status_reason is None
        assert concluded.status_reason is None


def test_invalid_failed_result_is_not_reported_as_an_acknowledged_stale_attempt(
    agent_system,
) -> None:
    client, services, _, clock = agent_system
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.stop",
        "a" * 64,
        STOP_PAYLOAD,
    )
    claim = client.post(
        "/agent/claim", headers=agent_headers(NODE_A, "serial-a")
    ).json()
    result = {key: claim[key] for key in ("fence",)} | {
        "state": "failed",
        "result": {"unexpected": "unstructured failure"},
    }

    response = client.post(
        "/agent/result", headers=agent_headers(NODE_A, "serial-a"), json=result
    )

    assert response.status_code == 422
    with services.sessions() as session:
        attempt = (
            session.query(AgentOperationAttempt).filter_by(fence=claim["fence"]).one()
        )
        assert attempt.state == "running"
        assert attempt.result is None


def test_recipe_job_failure_uses_its_typed_exit_result_at_authenticated_ingress(
    agent_system,
):
    import json
    from pathlib import Path

    client, services, _, clock = agent_system
    vectors = (
        Path(__file__).parents[2] / "agent_protocol/src/vonk_agent_protocol/vectors"
    )
    request = json.loads((vectors / "recipe-job-run-claim-v1.json").read_text())[
        "payload"
    ]
    job_result = json.loads((vectors / "recipe-job-run-result-v1.json").read_text())[
        "result"
    ]
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.job.run.v1",
        "a" * 64,
        request,
    )
    claim_response = client.post(
        "/agent/claim", headers=agent_headers(NODE_A, "serial-a")
    )
    assert claim_response.status_code == 200
    claim = claim_response.json()
    result = {key: claim[key] for key in ("fence",)}
    result.update(
        state="failed", result=dict(job_result, exit_code=1, reason="runtime failed")
    )
    response = client.post(
        "/agent/result", headers=agent_headers(NODE_A, "serial-a"), json=result
    )
    assert response.status_code == 204
    with services.sessions() as session:
        attempt = (
            session.query(AgentOperationAttempt).filter_by(fence=claim["fence"]).one()
        )
        assert attempt.state == "failed"
        assert attempt.result["exit_code"] == 1


def test_agent_validation_errors_are_canonical_json(agent_system) -> None:
    client, _, _, _ = agent_system

    response = client.post(
        "/agent/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"wait_seconds": -1},
    )

    assert response.status_code == 422
    assert response.content == canonical_message(response.json())


def test_claim_endpoint_long_poll_wakes_when_work_is_enqueued(agent_system) -> None:
    client, services, _, clock = agent_system
    parent_job = parent(services.sessions, clock)
    with ThreadPoolExecutor(max_workers=1) as pool:
        # A long wait with generous bounds: shared runners can stall a thread
        # for seconds, so correctness must not depend on wall-clock timing.
        waiting = pool.submit(
            client.post,
            "/agent/claim",
            headers=agent_headers(NODE_A, "serial-a"),
            json={"wait_seconds": 30},
        )
        # Synchronize on the observable event instead of sleeping: the claim
        # is parked on the availability condition once it has a waiter.
        deadline = time.monotonic() + 20
        while not services.operations._available._waiters:
            assert not waiting.done(), "claim returned before work existed"
            assert time.monotonic() < deadline, "claim never began waiting"
            time.sleep(0.005)
        operation = services.operations.enqueue(
            parent_job.id, NODE_A, "recipe.stop", "a" * 64, STOP_PAYLOAD
        )
        response = waiting.result(timeout=20)

    assert response.status_code == 200
    assert (
        fenced_operation(services.sessions, response.json()["fence"]).id == operation.id
    )


def test_enrollment_rate_limit_rejects_before_reading_request_body(
    agent_system,
) -> None:
    _, services, codec, _ = agent_system
    limiter = EnrollmentRateLimiter(maximum=1, window_seconds=60, clock=lambda: 0.0)
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        agent=services,
        enrollment_rate_limiter=limiter,
    )
    assert (
        asgi_post(
            app, "/agent/enroll", valid_enrollment_body(enrollment_grant(services))
        )[0]
        == 200
    )
    sent: list[Message] = []
    reads = 0

    async def receive() -> dict[str, object]:
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"never-read", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/agent/enroll",
        "raw_path": b"/agent/enroll",
        "query_string": b"",
        "headers": ((b"content-type", b"application/json"),),
        "client": ("testclient", 1234),
        "server": ("testserver", 80),
        "root_path": "",
        "state": {},
    }
    asyncio.run(asyncio.wait_for(app(scope, receive, send), timeout=10))

    assert (
        next(message for message in sent if message["type"] == "http.response.start")[
            "status"
        ]
        == 429
    )
    assert reads == 0


@pytest.mark.parametrize(
    "damage",
    [
        "json",
        "non-object",
        "ambiguous",
        "content-type",
        "evidence",
        "unicode-csr",
        "oversized-csr",
        "utf8",
    ],
)
def test_malformed_enrollment_preserves_grant_then_corrected_request_succeeds(
    agent_system, damage
):
    client, services, _, _ = agent_system
    token = enrollment_grant(services)
    valid = valid_enrollment_body(token)
    body = json.loads(valid)
    content_type = "application/json"
    if damage == "json":
        raw = f'{{"grant_token":"{token}",]'.encode()
    elif damage == "non-object":
        raw = f'[{{"grant_token":"{token}"}}]'.encode()
    elif damage == "ambiguous":
        raw = f'{{"grant_token":"{token}","grant_token":"{token}"}}'.encode()
    elif damage == "content-type":
        raw = valid
        content_type = "text/plain"
    elif damage == "utf8":
        raw = (
            f'{{"grant_token":"{token}","csr":"'.encode()
            + bytes([255])
            + bytes([34, 125])
        )
    else:
        if damage == "evidence":
            body["evidence"] = []
        elif damage == "unicode-csr":
            body["csr"] = "é"
        else:
            body["csr"] = "x" * (16 * 1024 + 1)
        raw = json.dumps(body).encode()
    status, _ = asgi_post(client.app, "/agent/enroll", raw, content_type=content_type)
    assert 400 <= status < 500
    assert_grant_available(services, token)
    status, response = asgi_post(client.app, "/agent/enroll", valid)
    assert status == 200
    assert json.loads(response)["node_id"] == NODE_C


def test_duplicate_enrollment_grants_preserve_unicode_escaped_token_values(
    agent_system,
) -> None:
    client, services, _, _ = agent_system
    first = enrollment_grant(services)
    second = enrollment_grant(services)
    escaped_second = "".join(f"\\u{ord(character):04x}" for character in second)
    raw = (
        f'{{"grant_token":"{first}","gr\\u0061nt_token":"{escaped_second}"}}'
    ).encode("ascii")

    status_code, _ = asgi_post(client.app, "/agent/enroll", raw)

    assert status_code == 422
    assert_grant_unconsumed(services, first)
    assert_grant_unconsumed(services, second)


def test_normal_enrollment_object_still_succeeds(agent_system) -> None:
    client, services, _, _ = agent_system
    token = enrollment_grant(services)

    status_code, response = asgi_post(
        client.app,
        "/agent/enroll",
        valid_enrollment_body(token),
    )

    assert status_code == 200
    assert json.loads(response)["node_id"] == NODE_C
    assert "certificate_pem" in json.loads(response)


def test_oversized_enrollment_preserves_split_discovery_prefix(agent_system) -> None:
    _, services, _, _ = agent_system
    token = enrollment_grant(services)
    first = b" " * 1000 + b'{"grant_to'
    second = b'ken":"' + token.encode("ascii") + b'","padding":"' + b"x" * (64 * 1024)
    request = ChunkedEnrollmentRequest(first, second, b"must-not-be-received")

    with pytest.raises(Exception):  # noqa: B017 -- ending witness; bounded body and corrected grant below
        asyncio.run(_bounded_enrollment_body(request, services))  # type: ignore[arg-type]

    assert request.received == 2
    assert_corrected_enrollment_succeeds(services, token)


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

    with pytest.raises(Exception):  # noqa: B017 -- ending witness; bounded body and corrected grant below
        asyncio.run(_bounded_enrollment_body(request, services))  # type: ignore[arg-type]

    assert request.received == 1
    assert huge.largest_slice <= 2048
    assert_corrected_enrollment_succeeds(services, token)


@pytest.mark.parametrize(
    "raw",
    (b"[1]", b"[]", b'"scalar"', b"0", b"true", b"false", b"null"),
    ids=("array", "empty-array", "string", "number", "true", "false", "null"),
)
@pytest.mark.usefixtures("damaged_json_rows")
def test_enrollment_rejects_non_object_json_without_server_error(
    agent_system, raw: bytes
) -> None:
    client, _, _, _ = agent_system

    status_code, _ = asgi_post(client.app, "/agent/enroll", raw)

    assert status_code == 422


def test_non_object_enrollment_preserves_identifiable_nested_grant(
    agent_system,
) -> None:
    client, services, _, _ = agent_system
    token = enrollment_grant(services)
    raw = f'[{{"grant_token":"{token}"}}]'.encode("ascii")

    status_code, _ = asgi_post(client.app, "/agent/enroll", raw)

    assert status_code == 422
    assert_corrected_enrollment_succeeds(services, token)


def test_invalid_evidence_preserves_every_discovered_grant(
    agent_system,
) -> None:
    client, services, _, _ = agent_system
    effective = enrollment_grant(services)
    nested = enrollment_grant(services)
    body = json.loads(valid_enrollment_body(effective))
    body["evidence"]["extra"] = {"grant_token": nested}

    status_code, _ = asgi_post(
        client.app,
        "/agent/enroll",
        json.dumps(body).encode("utf-8"),
    )

    assert status_code == 422
    assert_grant_unconsumed(services, effective)
    assert_grant_unconsumed(services, nested)


@pytest.mark.parametrize(
    ("prefix", "suffix"),
    (
        (b'{"grant_token":"', b'",]'),
        (b'{"grant_token":"', b'","invalid-utf8":"\xff"}'),
        (b"[" * 1500 + b'{"grant_token":"', b'"}' + b"]" * 1500),
    ),
    ids=("malformed-json", "invalid-utf8", "deep-nesting"),
)
def test_invalid_enrollment_json_preserves_identifiable_grant(
    agent_system,
    prefix: bytes,
    suffix: bytes,
) -> None:
    client, services, _, _ = agent_system
    token = enrollment_grant(services)

    status_code, _ = asgi_post(
        client.app,
        "/agent/enroll",
        prefix + token.encode("ascii") + suffix,
    )

    assert status_code == 422
    assert_corrected_enrollment_succeeds(services, token)


def test_wrong_enrollment_content_type_preserves_identifiable_grant(
    agent_system,
) -> None:
    client, services, _, _ = agent_system
    token = enrollment_grant(services)

    status_code, _ = asgi_post(
        client.app,
        "/agent/enroll",
        f'{{"grant_token":"{token}"}}'.encode("ascii"),
        content_type="text/plain",
    )

    assert status_code == 415
    assert_corrected_enrollment_succeeds(services, token)


def test_enrollment_evidence_has_a_fixed_bounded_schema(agent_system) -> None:
    client, _, _, _ = agent_system
    response = client.post(
        "/agent/enroll",
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
    assert response.status_code == 422


def test_artifact_access_is_owned_content_addressed_and_range_bounded(
    agent_system,
) -> None:
    client, services, _, clock = agent_system
    digest = hashlib.sha256(b"artifact").hexdigest()
    (services.artifact_root / digest).write_bytes(b"artifact")
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "agent.upgrade.v1",
        "a" * 64,
        upgrade_payload(digest, len(b"artifact")),
    )
    response = client.get(
        f"/agent/artifacts/{digest}",
        headers={**agent_headers(NODE_A, "serial-a"), "Range": "bytes=1-3"},
    )
    # The Controller only authorizes: the edge reads the named file and
    # answers the range, so this response carries no bytes and no range.
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["x-vonk-file"] == (
        (services.artifact_root / digest).relative_to("/").as_posix()
    )
    assert response.headers["etag"] == f'"sha256:{digest}"'
    assert (
        client.get(
            f"/agent/artifacts/{digest}", headers=agent_headers(NODE_B, "serial-b")
        ).status_code
        == 404
    )
    assert (
        client.get(
            "/agent/artifacts/../secret", headers=agent_headers(NODE_A, "serial-a")
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/agent/artifacts/{digest}",
            headers={**agent_headers(NODE_A, "serial-a"), "Range": "bytes=0-99999999"},
        ).status_code
        == 416
    )


def test_artifact_symlink_is_never_served(agent_system, tmp_path) -> None:
    client, services, _, clock = agent_system
    digest = "a" * 64
    (tmp_path / "outside").write_bytes(b"artifact")
    (services.artifact_root / digest).symlink_to(tmp_path / "outside")
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "agent.upgrade.v1",
        "a" * 64,
        upgrade_payload(digest, len(b"artifact")),
    )
    assert (
        client.get(
            f"/agent/artifacts/{digest}", headers=agent_headers(NODE_A, "serial-a")
        ).status_code
        == 404
    )


def test_artifact_is_named_to_the_edge_without_hashing_or_reading_the_file(
    agent_system,
) -> None:
    from .package_upgrade_fixtures import source_transport

    client, services, _, clock = agent_system
    payload = b"stored package bytes"
    # Same size, different content than the name says: authorized as stored,
    # which only holds if the route does not re-hash the object.
    digest = hashlib.sha256(b"a different payload").hexdigest()
    (services.artifact_root / digest).write_bytes(payload)
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "agent.upgrade.v1",
        "a" * 64,
        {
            "schema_version": 1,
            "architecture": "linux-arm64",
            "package_bytes": len(payload),
            "package_sha256": digest,
            "package_signature": "a" * 128,
            "package_url": "https://install.vonkforge.ai/releases/test/vonk-forge-agent.deb",
            "package_version": "1.2.3",
            "target_binary_digest": "b" * 64,
            "target_build_digest": "sha256:" + "c" * 64,
            **source_transport(),
        },
    )

    response = client.get(
        f"/agent/artifacts/{digest}",
        headers={**agent_headers(NODE_A, "serial-a"), "Range": "bytes=1-3"},
    )

    assert response.status_code == 200
    assert response.headers["x-vonk-file"].endswith("/" + digest)


def test_retired_agent_update_tuf_routes_are_absent(agent_system) -> None:
    client, _, _, _ = agent_system
    headers = agent_headers(NODE_A, "serial-a")
    paths = client.get("/openapi.json").json()["paths"]

    assert not any(path.startswith("/agent/tuf/") for path in paths)
    assert (
        client.get("/agent/tuf/metadata/timestamp.json", headers=headers).status_code
        == 404
    )
    assert (
        client.get(
            f"/agent/tuf/targets/platform/releases/1.2.3/{'a' * 64}.json",
            headers=headers,
        ).status_code
        == 404
    )


def test_protected_agent_routes_gate_untrusted_invalid_bodies_before_parsing(
    agent_system,
) -> None:
    client, _, _, _ = agent_system
    for path in (
        "/agent/claim",
        "/agent/heartbeat",
        "/agent/result",
        "/agent/renew",
        "/agent/telemetry",
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
            "/agent/result",
            headers={
                **agent_headers(NODE_A, "serial-a"),
                "content-type": "application/json",
            },
            content=b"{not-json",
        ).status_code
        == 401
    )


def test_job_wire_routes_publish_the_canonical_model_graph(agent_system) -> None:
    from vonk_agent_protocol import (
        AgentClaim,
        AgentDirective,
        AgentProgress,
        AgentResult,
    )
    from vonk_agent_protocol.wire_model import OperationProgress

    client, _services, _sessions, _clock = agent_system
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    components = schema["components"]["schemas"]

    for path, model in (("heartbeat", AgentProgress), ("result", AgentResult)):
        reference = paths[f"/agent/{path}"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        document = components[reference.rsplit("/", 1)[-1]]
        assert document["title"] == model.__name__
        assert set(document["required"]) == set(model.model_json_schema()["required"])

    for path, model in (("claim", AgentClaim), ("heartbeat", AgentDirective)):
        reference = paths[f"/agent/{path}"]["post"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        assert components[reference.rsplit("/", 1)[-1]]["title"] == model.__name__
    assert "204" in paths["/agent/claim"]["post"]["responses"]

    # A compact serializer must not erase the outgoing progress structure.
    progress = OperationProgress.model_json_schema(mode="serialization")
    assert progress["additionalProperties"] is False
    assert {"phase", "completed_bytes", "checkpoint", "members"} <= set(
        progress["properties"]
    )


@pytest.mark.parametrize(
    "missing",
    (
        "protocol_version",
        "runtime_identity",
        "wait_seconds",
    ),
)
def test_claim_requires_the_current_agent_document(agent_system, missing: str) -> None:
    client, _services, _sessions, _clock = agent_system
    body = {
        "protocol_version": 4,
        "runtime_identity": dict(PACKAGED_RUNTIME_IDENTITY),
        "wait_seconds": 0,
    }
    del body[missing]
    # Use the raw request method: the convenience test client must not refill
    # deliberately missing fields and hide a production boundary regression.
    response = client.request(
        "POST",
        "/agent/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json=body,
    )
    assert response.status_code == 422


def test_enrollment_openapi_exposes_the_runtime_request_contract(agent_system) -> None:
    from vonk_agent_protocol.enrollment import EnrollmentSubmitRequest

    client, _services, _sessions, _clock = agent_system
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/agent/enroll"]["post"]
    request = operation["requestBody"]
    assert request["required"] is True
    assert request["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/EnrollmentSubmitRequest"
    }
    expected = EnrollmentSubmitRequest.model_json_schema(
        ref_template="#/components/schemas/{model}"
    )
    nested = expected.pop("$defs")
    components = schema["components"]["schemas"]
    assert components["EnrollmentSubmitRequest"] == expected
    for name, document in nested.items():
        assert components[name] == document


def test_renewal_recovery_openapi_exposes_the_canonical_runtime_contract(
    agent_system,
) -> None:
    from vonk_agent_protocol.enrollment import IssuedCertificateResponse, RenewRequest

    client, _services, _sessions, _clock = agent_system
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/agent/renew/recover"]["post"]
    assert operation["requestBody"] == {
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/RenewRequest"}
            }
        },
        "required": True,
    }
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/IssuedCertificateResponse"
    }
    components = schema["components"]["schemas"]
    assert components["RenewRequest"] == RenewRequest.model_json_schema(
        ref_template="#/components/schemas/{model}"
    )
    assert components["IssuedCertificateResponse"] == (
        IssuedCertificateResponse.model_json_schema(
            ref_template="#/components/schemas/{model}"
        )
    )


@pytest.mark.parametrize("lose_response", [False, True])
def test_cli_enrollment_file_and_recovery_use_the_actual_authority(
    agent_system, tmp_path, capsys, lose_response
):
    from email.message import Message as EmailMessage
    from urllib.error import URLError

    from vonk_control.operator_projection_api import build_fleet_operator_services

    from cluster_profiles import cli
    from cluster_profiles.control_client import ControlClient

    from .test_enrollment import csr, evidence

    _agent_api, services, codec, _clock = agent_system
    api = TestClient(
        create_app(
            jobs=Jobs(),
            tokens=codec,
            now=lambda: 0,
            fleet_services=build_fleet_operator_services(
                agent_services=services, upgrades=None
            ),
        )
    )
    identity = str(uuid.uuid4())
    actor = Actor("admin", "administrator")
    token_file = tmp_path / "cli-token"
    token_file.touch(mode=0o600)
    token_file.write_text(codec.issue(actor, ttl_seconds=3600, now=0))
    issued_secrets = []
    posts = []

    class OpenedResponse(io.BytesIO):
        def __init__(self, response):
            super().__init__(response.content)
            self.status = response.status_code
            self.headers = EmailMessage()
            for key, value in response.headers.items():
                self.headers[key] = value

        def __exit__(self, *args: object) -> None:
            self.close()

    def opener(request, *, timeout):
        response = api.request(
            request.get_method(),
            request.full_url,
            headers=dict(request.header_items()),
            content=request.data,
        )
        if request.get_method() == "POST":
            posts.append(request.full_url)
        if (
            request.full_url.endswith("/api/fleet/enroll")
            and response.status_code == 201
        ):
            issued_secrets.append(response.json()["grant"]["token"])
            if lose_response:
                raise URLError("response lost after acceptance")
        return OpenedResponse(response)

    client = ControlClient("https://forge.example.test", token_file, opener=opener)
    destination = tmp_path / "grant.json"
    code = cli.main(
        ("fleet", "enroll", "New Spark", "--output", str(destination), "--json"),
        control_client=client,
        request_id_factory=lambda: identity,
    )
    assert code == (2 if lose_response else 0), capsys.readouterr().out
    captured = capsys.readouterr()
    assert len(issued_secrets) == 1
    assert issued_secrets[0] not in captured.out + captured.err
    assert len(posts) == 1
    status_path = f"/api/fleet/enrollments/{identity}"
    assert client.request("GET", status_path)["state"] == "pending"
    assert api.get(status_path).status_code == 401
    for subject, role, expected in (
        ("viewer", "viewer", 403),
        ("other-admin", "administrator", 404),
    ):
        headers = {
            "Authorization": "Bearer "
            + codec.issue(Actor(subject, role), ttl_seconds=3600, now=0)
        }
        assert api.get(status_path, headers=headers).status_code == expected
        assert (
            api.post(status_path + "/revoke", headers=headers).status_code == expected
        )
    if lose_response:
        receipt = json.loads(destination.read_text())
        assert receipt["id"] == identity
        assert receipt["grant_status"]["state"] == "pending"
        assert (
            cli.main(
                ("fleet", "enrollment", "revoke", identity, "--yes", "--json"),
                control_client=client,
            )
            == 0
        )
        assert client.request("GET", status_path)["state"] == "revoked"
    else:
        grant = json.loads(destination.read_text())
        request = csr()
        assert services.enrollment is not None
        services.enrollment.submit(grant["token"], request, evidence(request))
        assert client.request("GET", status_path)["state"] == "consumed"
    assert destination.stat().st_mode & 0o777 == 0o600


def test_reenrollment_refuses_an_unprivileged_actor_before_node_lookup(agent_system):
    api, _services, codec, _clock = agent_system
    headers = {
        "Authorization": "Bearer "
        + codec.issue(Actor("operator", "operator"), ttl_seconds=3600, now=0)
    }
    response = api.post(
        f"/api/fleet/{NODE_A}/re-enroll",
        headers=headers,
        json={"request_key": str(uuid.uuid4())},
    )
    assert response.status_code == 403


def test_enrollment_reader_miss_ends_and_fresh_same_node_is_admitted(
    agent_system, monkeypatch
):
    from vonk_control.step_ca import StepCAUnavailable

    client, services, _, _ = agent_system
    original = services.enrollment._authority.issue_node

    def unavailable(*args, **kwargs):
        raise StepCAUnavailable("reader unavailable")

    monkeypatch.setattr(services.enrollment._authority, "issue_node", unavailable)
    body = json.loads(valid_enrollment_body(enrollment_grant(services)))
    response = client.post("/agent/enroll", json=body)
    assert response.status_code == 503
    EnrollmentObservationReply.model_validate_json(response.content)
    monkeypatch.setattr(services.enrollment._authority, "issue_node", original)
    fresh = json.loads(valid_enrollment_body(enrollment_grant(services)))
    repaired = client.post("/agent/enroll", json=fresh)
    assert repaired.status_code == 200
    assert repaired.json()["node_id"] == NODE_C


@pytest.mark.parametrize("failure", [None, "grace", "revoked", "removed", "wrong-key"])
def test_expired_renewal_endpoint_requires_enrolled_key_without_mtls(tmp_path, failure):
    from .test_enrollment import RecoveryAuthority, csr, enroll, expired_proof

    client, services, _codec, clock = make_agent_system(
        tmp_path, authority=RecoveryAuthority()
    )
    enrollment = services.enrollment
    assert enrollment is not None
    key = ed25519.Ed25519PrivateKey.generate()
    issued = enroll(enrollment, node_id=NODE_C, request=csr(NODE_C, key=key))
    clock.now = issued.not_after + timedelta(days=1)
    if failure == "grace":
        clock.now = issued.not_after + timedelta(days=30, seconds=1)
    if failure in {"revoked", "removed"}:
        with services.sessions.begin() as session:
            if failure == "removed":
                session.execute(
                    delete(AgentCertificate).where(AgentCertificate.node_id == NODE_C)
                )
                session.execute(delete(AgentNode).where(AgentNode.node_id == NODE_C))
            else:
                certificate = session.get(AgentCertificate, issued.serial)
                assert certificate is not None
                certificate.revoked_at = clock.now
    signer = ed25519.Ed25519PrivateKey.generate() if failure == "wrong-key" else key
    proof = expired_proof(signer, NODE_C, issued.serial, csr(NODE_C), clock.now)
    response = client.post(
        "/agent/renew/expired",
        content=proof.model_dump_json(),
        headers={"content-type": "application/json"},
    )
    if failure is None:
        assert response.status_code == 200
        assert (
            client.post(
                "/agent/renew/expired",
                content=proof.model_dump_json(),
                headers={"content-type": "application/json"},
            ).json()
            == response.json()
        )
    else:
        assert response.status_code == 403
        expected = (
            SecurityRefusalReason.AGENT_EXPIRED_RENEWAL_GRACE_EXHAUSTED
            if failure == "grace"
            else SecurityRefusalReason.AGENT_EXPIRED_RENEWAL_REFUSED
        )
        assert response.json()["detail"]["reason_code"] == expected.value
    # The proof-only exemption must never admit expired identity to work.
    assert client.post("/agent/claim", json={}).status_code == 401
    fresh_grant = enrollment.create("spk_" + "e" * 32, "admin", 60)
    assert isinstance(fresh_grant, EnrollmentGrant)
    assert fresh_grant.node_id == "spk_" + "e" * 32


def test_gpu_collection_reason_reaches_fleet_and_fresh_report_recovers(agent_system):
    # Catches ingress accepting the reason but omitting it from stored telemetry.
    from vonk_agent_protocol.telemetry import (
        GpuUnavailableReason,
        TelemetryRequest,
        TelemetrySample,
    )
    from vonk_control.fleet_projection import telemetry_point
    from vonk_control.telemetry import TelemetryRepository

    client, services, _, clock = agent_system
    failed = TelemetrySample(
        boot_id="00000000-0000-4000-8000-000000000001",
        observed_at=clock.now,
        memory_total_bytes=128_000_000_000,
        memory_available_bytes=64_000_000_000,
        disk_total_bytes=None,
        disk_free_bytes=None,
        gpu_utilization_percent=None,
        gpu_memory_total_bytes=None,
        gpu_memory_free_bytes=None,
        gpu_unavailable_reason=GpuUnavailableReason.COMMAND_FAILED,
    )
    repository = TelemetryRepository(services.sessions, clock=clock)
    response = client.post(
        "/agent/telemetry",
        headers=agent_headers(NODE_A, "serial-a"),
        json=TelemetryRequest(samples=[failed]).model_dump(mode="json"),
    )
    assert response.status_code == 204
    point = telemetry_point(repository.latest((NODE_A,))[NODE_A])
    assert point.gpu_unavailable_reason is GpuUnavailableReason.COMMAND_FAILED
    clock.now += timedelta(seconds=1)
    recovered = failed.model_copy(
        update={
            "observed_at": clock.now,
            "gpu_unavailable_reason": None,
            "gpu_utilization_percent": 25.0,
            "gpu_temperature_c": 61,
        }
    )
    response = client.post(
        "/agent/telemetry",
        headers=agent_headers(NODE_A, "serial-a"),
        json=TelemetryRequest(samples=[recovered]).model_dump(mode="json"),
    )
    assert response.status_code == 204
    point = telemetry_point(repository.latest((NODE_A,))[NODE_A])
    assert point.gpu_unavailable_reason is None
    assert point.gpu_utilization_percent == 25.0
    assert point.gpu_temperature_c == 61
    assert point.memory_total_bytes == 128_000_000_000
    assert point.gpu_memory_total_bytes is None


def test_enrollment_observation_does_not_block_unrelated_requests(
    agent_system, monkeypatch
):
    """Catches synchronous CA/SQL observation running on the async route's loop."""
    client, services, _, _ = agent_system
    original = services.enrollment._authority.issue_node
    entered = Event()
    release = Event()
    health_completed = Event()
    body = json.loads(valid_enrollment_body(enrollment_grant(services)))

    def slow_issue(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(services.enrollment._authority, "issue_node", slow_issue)

    def observe():
        try:
            assert entered.wait(timeout=3)
            return health_completed.wait(timeout=2)
        finally:
            release.set()

    async def exercise():
        async with AsyncClient(
            transport=ASGITransport(app=client.app), base_url="http://testserver"
        ) as async_client:

            async def health_request():
                assert await asyncio.to_thread(entered.wait, 3)
                response = await async_client.get("/api/healthz")
                health_completed.set()
                return response

            with ThreadPoolExecutor(max_workers=1) as pool:
                observer = pool.submit(observe)
                pairing = asyncio.create_task(
                    async_client.post("/agent/enroll", json=body)
                )
                health = asyncio.create_task(health_request())
                try:
                    paired, responsive = await asyncio.wait_for(
                        asyncio.gather(pairing, health),
                        timeout=10,
                    )
                finally:
                    release.set()
                return paired, responsive, observer.result(timeout=1)

    paired, responsive, progressed = asyncio.run(exercise())
    assert progressed
    assert paired.status_code == responsive.status_code == 200
    replay = client.post("/agent/enroll", json=body)
    assert replay.status_code == 200
    assert replay.json() == paired.json()
