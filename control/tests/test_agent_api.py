from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi import HTTPException
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import CompiledExecutionPlan as AgentCompiledExecutionPlan
from vonk_agent_protocol import (
    ExecuteContainerRuntimeRequestOperation,
    HostHelperGrantClaims,
    HostHelperSignature,
    InstallVonkDebOperation,
    SignedHostHelperGrant,
    SignedPackageHelperGrant,
    SignedPackageObjectReceipt,
    canonical_message,
)
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
from vonk_control.enrollment_bootstrap import EnrollmentBootstrapConfig
from vonk_control.host_helper_authority import HostHelperGrantIssuer
from vonk_control.metrics import MetricsRegistry, OperationalMetricsCollector
from vonk_control.models import (
    AgentCertificate,
    AgentCertificateRotation,
    AgentEnrollment,
    AgentEnrollmentGrant,
    AgentNode,
    AgentNodeProfile,
    AgentOperationAttempt,
    AgentPresence,
    Base,
    CatalogDocument,
    CatalogDocumentRevision,
    ClusterMapping,
    ClusterMappingNode,
    InstallationNode,
    Job,
    NodeInventorySnapshot,
    NodeTelemetrySample,
    Observation,
    RecipeBuild,
    RecipeInstallation,
    RecipeRouteAuthority,
    RecipeRun,
    RecipeSourceBundle,
    RoutePublication,
    RunNode,
    RuntimeImageAuthorization,
    RuntimeImageReceipt,
)
from vonk_control.pki import CertificateAuthority, IssuedCertificate
from vonk_control.presence import AgentPresenceService, ManagementAddressPolicy
from vonk_control.route_runtime import RECIPE_ROUTE_AUTHORITY_ID
from vonk_control.source_bundles import SourceBundleStore, generate_source_bundle
from vonk_control.workload_helper_authority import (
    WorkloadHelperGrantIssuer,
    WorkloadObjectReceiptIssuer,
)
from vonk_forge_contracts import RecipeDefinition, content_sha256

NODE_A = "spk_" + "a" * 32
NODE_B = "spk_" + "b" * 32
NODE_C = "spk_" + "c" * 32
CAPABILITIES = sorted(
    [
        "agent.runtime.rust.v1",
        "runtime.vonk.v1",
        "agent.upgrade.v1",
        "artifact.distribution.v1",
        "recipe.build.v1",
        "recipe.image.import.v1",
        "recipe.install",
        "recipe.start",
        "recipe.job.run.v1",
        "recipe.stop",
        "recipe.uninstall",
        "recipe.model-uninstall.v1",
    ]
)
STOP_PAYLOAD = {
    "schema_version": 1,
    "run_id": "00000000-0000-4000-8000-000000000001",
    "plan_digest": "a" * 64,
}


def image_import_payload(digest: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "recipe.image.import.v1",
        "build_id": "00000000-0000-4000-8000-000000000010",
        "mapping_id": "00000000-0000-4000-8000-000000000011",
        "mapping_generation": 1,
        "source_node_id": NODE_A,
        "image_digest": "sha256:" + "b" * 64,
        "oci_layout_sha256": digest,
        "image_bytes": 8,
    }


PACKAGED_RUNTIME_IDENTITY = {
    "architecture": "linux-amd64",
    "binary_digest": "c" * 64,
    "build_digest": "sha256:" + "b" * 64,
    "semantic_version": "1.2.3",
    "self_test_passed": True,
    "observation_receipt_public_key": "d" * 64,
}


def _canonical_recipe_fixture(
    slug: str, *, source: str = "published"
) -> tuple[dict[str, object], str]:
    example = (
        "recipe-source-build.json"
        if source == "controller-build"
        else "recipe-image.json"
    )
    raw = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", example)
        .read_text(encoding="utf-8")
    )
    raw["identity"]["slug"] = slug
    recipe = RecipeDefinition.model_validate(raw)
    document = recipe.model_dump(mode="json")
    return document, content_sha256(recipe)


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
    subject = x509.Name(
        [x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "controller-ca")]
    )
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
            "e" * 64,
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


class CurrentAgentClient(TestClient):
    """Supply the current Rust claim envelope for terse authenticated tests."""

    def post(self, url, *args, headers=None, json=None, **kwargs):
        if (
            url == "/agent/v1/claim"
            and headers is not None
            and headers.get("x-vonk-agent-verified") == "1"
        ):
            body = {
                "capabilities": CAPABILITIES,
                "lease_seconds": 60,
                "node_id": headers["x-vonk-agent-node"],
                "protocol_version": 3,
                "runtime_identity": PACKAGED_RUNTIME_IDENTITY,
                "wait_seconds": 0,
            }
            if isinstance(json, dict):
                body.update(json)
            json = body
        return super().post(url, *args, headers=headers, json=json, **kwargs)


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
    controller_ca_pem, controller_ca_fingerprint = _controller_ca()
    services = AgentApiServices(
        enrollment=EnrollmentService(sessions, Authority(), clock=clock),
        operations=operations,
        sessions=sessions,
        clock=clock,
        presence=presence,
        artifact_root=tmp_path / "artifacts",
        source_bundles=SourceBundleStore(tmp_path / "source-bundles"),
        workload_tuf_metadata_root=tmp_path / "workload-tuf-metadata",
        workload_tuf_target_root=tmp_path / "workload-tuf-targets",
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
    services.workload_tuf_metadata_root.mkdir()
    services.workload_tuf_target_root.mkdir()
    codec = TokenCodec(b"k" * 32)
    audits = MemoryAuditStore()
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        audits=audits,
        now=lambda: 0,
        agent=services,
        trusted_agent_proxy_auth=b"p" * 32,
    )
    app.state.test_audits = audits
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
    sequence: int = 1,
    observed_at: datetime | None = None,
    boot_id: str = "00000000-0000-4000-8000-000000000001",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "samples": [
            {
                "boot_id": boot_id,
                "sequence": sequence,
                "observed_at": (observed_at or clock.now).isoformat(),
                "cpu_utilization_percent": 12.5,
                "load_average_1m": 1.25,
                "memory_total_bytes": 128_000_000_000,
                "memory_available_bytes": 64_000_000_000,
                "disk_total_bytes": 1_000_000_000_000,
                "disk_free_bytes": 750_000_000_000,
                "gpu_utilization_percent": 25.0,
                "gpu_memory_total_bytes": 128_000_000_000,
                "gpu_memory_free_bytes": 63_000_000_000,
                "temperature_c": 41.5,
                "power_watts": 17.25,
                "network_receive_bytes_per_second": 1024.5,
                "network_transmit_bytes_per_second": 512.25,
                "gap_samples": 0,
                "details": {
                    "accelerator_name": "NVIDIA GB10",
                    "accelerator_performance_state": "P0",
                },
                "metrics": {
                    "schema_version": 2,
                    "series": [],
                    "capabilities": [],
                    "runtimes": [],
                    "workloads": [],
                    "provenance": {
                        "collector": "test",
                        "collector_version": "1",
                    },
                },
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
            b'{"schema_version":1,"samples":[],"padding":"'
            + b"x" * (MAX_TELEMETRY_REPORT_BYTES // 2),
            b"x" * (MAX_TELEMETRY_REPORT_BYTES // 2),
            b'"}',
        ]
        reads = 0
        sent: list[dict[str, object]] = []

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

        async def send(message: dict[str, object]) -> None:
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
            "path": "/agent/v1/telemetry",
            "raw_path": b"/agent/v1/telemetry",
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
        await asyncio.wait_for(app(scope, receive, send), timeout=1)  # type: ignore[operator]
        start = next(
            message for message in sent if message["type"] == "http.response.start"
        )
        return int(start["status"]), reads

    return asyncio.run(request())


@pytest.mark.parametrize("series_count", [143, 512])
def test_large_valid_telemetry_preserves_all_metrics_through_api_and_storage(
    agent_system,
    series_count: int,
) -> None:
    from vonk_agent_protocol import TelemetryRequest
    from vonk_agent_protocol.telemetry import MAX_TELEMETRY_REPORT_BYTES

    client, services, _, clock = agent_system
    payload = telemetry_payload(clock)
    sampled = payload["samples"][0]
    series = [
        {
            "key": f"device.metric_{index}",
            "scope": "node",
            "process_name": "測" * 128,
            "value": "測" * 256,
            "unit": "state",
            "source": "native-collector",
            "measurement_kind": "measured",
            "observed_at": sampled["observed_at"],
            "freshness": "fresh",
            "freshness_threshold_seconds": 6.0,
            "support_status": "available",
            "aggregation": "last",
        }
        for index in range(series_count)
    ]
    sampled["metrics"] = {
        "schema_version": 2,
        "series": series,
        "capabilities": [],
        "runtimes": [],
        "workloads": [],
        "provenance": {
            "collector": "native-collector",
            "collector_version": "2",
            "host_uptime_seconds": 1,
            "source_observed_at": sampled["observed_at"],
        },
    }
    encoded = canonical_message(payload)
    assert 64 * 1024 < len(encoded) < MAX_TELEMETRY_REPORT_BYTES
    assert (
        len(TelemetryRequest.parse(payload).samples[0].metrics.series) == series_count
    )
    response = client.post(
        "/agent/v1/telemetry",
        headers=agent_headers(NODE_A, "serial-a"),
        content=encoded,
    )
    assert response.status_code == 204, response.text
    with services.sessions() as session:
        row = session.scalar(select(NodeTelemetrySample))
        assert row is not None
        assert [item["key"] for item in row.metrics["series"]] == [
            item["key"] for item in series
        ]
        assert all(item["value"] == "測" * 256 for item in row.metrics["series"])


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
            sequence=2,
            observed_at=clock.now,
        )["samples"][0]  # type: ignore[index]
    )

    response = client.post(
        "/agent/v1/telemetry",
        headers=agent_headers(NODE_A, "serial-a"),
        json=payload,
    )

    assert response.status_code == 204
    with services.sessions() as session:
        rows = session.scalars(
            select(NodeTelemetrySample).order_by(NodeTelemetrySample.sequence)
        ).all()
        assert [(row.node_id, row.sequence) for row in rows] == [
            (NODE_A, 1),
            (NODE_A, 2),
        ]
        assert rows[0].observed_at != rows[0].received_at
        assert rows[0].received_at == clock.now.replace(tzinfo=None)


def test_telemetry_body_cannot_choose_node_identity(agent_system) -> None:
    client, services, _, clock = agent_system
    payload = telemetry_payload(clock) | {"node_id": NODE_B}

    response = client.post(
        "/agent/v1/telemetry",
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
        "/agent/v1/telemetry",
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
        "/agent/v1/telemetry",
        headers=agent_headers(NODE_A, "serial-a"),
        json=telemetry_payload(clock, observed_at=clock.now + offset),
    )
    assert response.status_code == 422


def test_telemetry_rejects_more_than_sixteen_samples(agent_system) -> None:
    client, _, _, clock = agent_system
    samples = [
        telemetry_payload(
            clock,
            sequence=index,
            observed_at=clock.now - timedelta(seconds=16 - index),
        )["samples"][0]  # type: ignore[index]
        for index in range(17)
    ]
    response = client.post(
        "/agent/v1/telemetry",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"schema_version": 1, "samples": samples},
    )
    assert response.status_code == 422


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_telemetry_schema_version_is_exact_integer(
    agent_system, schema_version: object
) -> None:
    client, _, _, clock = agent_system
    payload = telemetry_payload(clock)
    payload["schema_version"] = schema_version
    response = client.post(
        "/agent/v1/telemetry",
        headers=agent_headers(NODE_A, "serial-a"),
        json=payload,
    )
    assert response.status_code == 422


def test_telemetry_observed_at_is_rfc3339_string(agent_system) -> None:
    client, _, _, clock = agent_system
    payload = telemetry_payload(clock)
    payload["samples"][0]["observed_at"] = int(clock.now.timestamp())  # type: ignore[index]
    response = client.post(
        "/agent/v1/telemetry",
        headers=agent_headers(NODE_A, "serial-a"),
        json=payload,
    )
    assert response.status_code == 422


def test_telemetry_requires_every_fixed_core_metric(agent_system) -> None:
    client, _, _, clock = agent_system
    payload = telemetry_payload(clock)
    del payload["samples"][0]["gpu_utilization_percent"]  # type: ignore[index]
    response = client.post(
        "/agent/v1/telemetry",
        headers=agent_headers(NODE_A, "serial-a"),
        json=payload,
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "document",
    [
        '{"schema_version":1,"schema_version":1,"samples":[]}',
        (
            '{"schema_version":1,"samples":[{'
            '"boot_id":"00000000-0000-4000-8000-000000000001",'
            '"sequence":1,"sequence":2,'
            '"observed_at":"2026-08-03T12:00:00+00:00"}]}'
        ),
    ],
)
def test_telemetry_rejects_duplicate_json_keys(agent_system, document: str) -> None:
    client, _, _, _ = agent_system
    response = client.post(
        "/agent/v1/telemetry",
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
        "artifact_store_read_only": False,
        "capabilities": ["runtime.vonk.v1", "fabric.connected.mbps.200000"],
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


def test_helper_json_routes_use_strict_wire_models_and_canonical_signed_outputs(
    agent_system,
) -> None:
    client, services, _, clock = agent_system
    request_id = "00000000-0000-4000-8000-000000000005"
    job_id = "00000000-0000-4000-8000-000000000002"
    operation_id = "00000000-0000-4000-8000-000000000003"
    fence = "00000000-0000-4000-8000-000000000004"

    host_issuer = HostHelperGrantIssuer(
        ed25519.Ed25519PrivateKey.generate(),
        clock=clock,
        request_id_factory=lambda: uuid.UUID(request_id),
    )
    package_issuer = WorkloadHelperGrantIssuer(
        ed25519.Ed25519PrivateKey.generate(),
        clock=clock,
        request_id_factory=lambda: uuid.UUID(request_id),
    )
    receipt_issuer = WorkloadObjectReceiptIssuer(ed25519.Ed25519PrivateKey.generate())

    class RecordingHostAuthority:
        def __init__(self) -> None:
            self.grant_calls: list[dict[str, object]] = []
            self.upgrade_calls: list[dict[str, object]] = []

        def issue_grant(self, **kwargs: object) -> object:
            self.grant_calls.append(kwargs)
            operation = ExecuteContainerRuntimeRequestOperation(
                type="execute-container-runtime-request",
                action=kwargs["action"].value,
                job_id=kwargs["job_id"],
                operation_id=kwargs["operation_id"],
                attempt=kwargs["attempt"],
                fence=kwargs["fence"],
                request_sha256=kwargs["request_sha256"],
            )
            return host_issuer.issue_grant(
                node_id=kwargs["node_id"],
                operation=operation,
                expires_in_seconds=kwargs["expires_in_seconds"],
            )

        def issue_agent_upgrade_grant(self, **kwargs: object) -> object:
            self.upgrade_calls.append(kwargs)
            operation = InstallVonkDebOperation(
                type="install-vonk-deb",
                package_sha256=kwargs["package_sha256"],
                package_signature=kwargs["package_signature"],
            )
            return host_issuer.issue_grant(
                node_id=kwargs["node_id"],
                operation=operation,
                expires_in_seconds=kwargs["expires_in_seconds"],
            )

    class RecordingPackageAuthority:
        def __init__(self) -> None:
            self.grant_calls: list[dict[str, object]] = []
            self.receipt_calls: list[dict[str, object]] = []
            self.receipt_output: object | None = None

        def issue_grant(self, **kwargs: object) -> object:
            self.grant_calls.append(kwargs)
            return package_issuer.issue_grant(
                request_id=kwargs["request_id"],
                node_id=kwargs["node_id"],
                job_id=kwargs["job_id"],
                operation_id=kwargs["operation_id"],
                attempt=kwargs["attempt"],
                fence=kwargs["fence"],
                release_digest=kwargs["release_digest"],
                generation=kwargs["generation"],
                operation=kwargs["operation"],
                request_digest=kwargs["request_digest"],
                expires_in_seconds=kwargs["expires_in_seconds"],
            )

        def issue_receipts(self, **kwargs: object) -> tuple[object, ...]:
            self.receipt_calls.append(kwargs)
            if self.receipt_output is not None:
                return self.receipt_output  # type: ignore[return-value]
            return tuple(
                receipt_issuer.issue_object_receipt(
                    object_digest=item["object_digest"], size=item["size"]
                )
                for item in kwargs["objects"]
            )

    host = RecordingHostAuthority()
    package = RecordingPackageAuthority()
    object.__setattr__(services, "host_runtime_authority", host)
    object.__setattr__(services, "workload_helper_authority", package)
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    assert (
        schemas["PackageHelperSignature"]["properties"]["algorithm"]["const"]
        == "ed25519"
    )
    uuid4_pattern = (
        r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    grant_claims = schemas["PackageHelperGrantClaims"]["properties"]
    assert set(schemas["PackageHelperGrantClaims"]["required"]) >= {
        "request_id",
        "job_id",
        "operation_id",
        "fence",
    }
    for field_name in ("request_id", "job_id", "operation_id", "fence"):
        assert grant_claims[field_name]["pattern"] == uuid4_pattern
    assert "relative_name" in schemas["PackageObjectReceiptClaims"]["required"]
    assert (
        schemas["PackageObjectReceiptClaims"]["properties"]["relative_name"]["pattern"]
        == r"^objects/sha256/[0-9a-f]{64}$"
    )
    assert (
        schemas["PackageHelperReceiptsResponse"]["properties"]["receipts"]["items"][
            "$ref"
        ]
        == "#/components/schemas/SignedPackageObjectReceipt"
    )
    assert (
        schemas["PackageHelperGrantResponse"]["properties"]["grant"]["$ref"]
        == "#/components/schemas/SignedPackageHelperGrant"
    )
    headers = agent_headers(NODE_A, "serial-a")
    common = {
        "node_id": NODE_A,
        "job_id": job_id,
        "operation_id": operation_id,
        "attempt": 1,
        "fence": fence,
        "expires_in_seconds": 30,
    }

    host_response = client.post(
        "/agent/v1/host-runtime/grant",
        headers=headers,
        json=common | {"action": "start", "request_sha256": "a" * 64},
    )
    assert host_response.status_code == 200
    assert isinstance(
        SignedHostHelperGrant.parse(host_response.json()["grant"]),
        SignedHostHelperGrant,
    )
    assert host.grant_calls[0]["job_id"] == job_id

    upgrade_response = client.post(
        "/agent/v1/agent-upgrade/grant",
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

    receipt_response = client.post(
        "/agent/v1/package-helper/receipts",
        headers=headers,
        json={
            key: value for key, value in common.items() if key != "expires_in_seconds"
        }
        | {
            "release_digest": "d" * 64,
            "objects": [{"object_digest": "e" * 64, "size": 17}],
        },
    )
    assert receipt_response.status_code == 200
    assert isinstance(
        SignedPackageObjectReceipt.parse(receipt_response.json()["receipts"][0]),
        SignedPackageObjectReceipt,
    )
    assert package.receipt_calls[0]["objects"] == [
        {"object_digest": "e" * 64, "size": 17}
    ]

    package.receipt_output = ({"claims": {"unexpected": True}},)
    malformed_receipt_response = client.post(
        "/agent/v1/package-helper/receipts",
        headers=headers,
        json={
            key: value for key, value in common.items() if key != "expires_in_seconds"
        }
        | {
            "release_digest": "d" * 64,
            "objects": [{"object_digest": "e" * 64, "size": 17}],
        },
    )
    assert malformed_receipt_response.status_code == 409
    package.receipt_output = None

    grant_body = common | {
        "request_id": request_id,
        "release_digest": "d" * 64,
        "generation": "gen-future-stack-001",
        "operation": "health",
        "request_digest": "f" * 64,
    }
    grant_response = client.post(
        "/agent/v1/package-helper/grant", headers=headers, json=grant_body
    )
    assert grant_response.status_code == 200
    assert isinstance(
        SignedPackageHelperGrant.parse(grant_response.json()["grant"]),
        SignedPackageHelperGrant,
    )
    assert package.grant_calls[0]["generation"] == "gen-future-stack-001"

    assert (
        client.post(
            "/agent/v1/package-helper/grant",
            headers=headers,
            json=grant_body | {"generation": 7},
        ).status_code
        == 422
    )
    assert len(package.grant_calls) == 1
    assert (
        client.post(
            "/agent/v1/package-helper/receipts",
            headers=headers,
            json={
                key: value
                for key, value in common.items()
                if key != "expires_in_seconds"
            }
            | {
                "release_digest": "d" * 64,
                "objects": [{"object_digest": "e" * 64, "size": 17, "extra": True}],
            },
        ).status_code
        == 422
    )
    assert len(package.receipt_calls) == 2
    assert (
        client.post(
            "/agent/v1/host-runtime/grant",
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
            "/agent/v1/agent-upgrade/grant",
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
    payload = {
        "schema_version": 2,
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
            json=payload | {"schema_version": 1},
        ).status_code
        == 422
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
    document, digest = _canonical_recipe_fixture("bundle-download")
    with services.sessions.begin() as session:
        session.add(
            CatalogDocument(
                id=recipe_id,
                kind="recipe",
                publisher="vonk-forge",
                slug="bundle-download",
                title=document["metadata"]["title"],
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
    with services.sessions.begin() as session:
        session.add(
            CatalogDocument(
                id=recipe_id,
                kind="recipe",
                publisher="vonk-forge",
                slug="image-upload",
                title=document["metadata"]["title"],
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
                policy_report={"passed": True},
                plan={},
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
    headers = agent_headers(NODE_A, "serial-a") | {
        "content-type": "application/x-tar",
        "x-vonk-image-digest": image_digest,
        "x-vonk-oci-layout-sha256": layout_digest,
    }

    rejected = client.put(
        f"/agent/v1/recipe-builds/{build_id}/image",
        headers=headers | {"content-type": "application/vnd.oci.image.layout.v1.tar"},
        content=payload,
    )
    assert rejected.status_code == 415

    response = client.put(
        f"/agent/v1/recipe-builds/{build_id}/image",
        headers=headers,
        content=payload,
    )

    assert response.status_code == 204
    from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

    storage = FilesystemRuntimeImageStorage(services.artifact_root)
    assert storage.verify_existing(layout_digest, len(payload)).read_bytes() == payload
    assert not (services.artifact_root / layout_digest).exists()
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
                "content-type": "application/x-tar",
                "x-vonk-image-digest": image_digest,
                "x-vonk-oci-layout-sha256": layout_digest,
            },
            content=payload,
        ).status_code
        == 404
    )


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
    with services.sessions.begin() as session:
        session.add(
            CatalogDocument(
                id=recipe_id,
                kind="recipe",
                publisher="vonk-forge",
                slug="nonblocking-image-upload",
                title=document["metadata"]["title"],
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
                policy_report={"passed": True},
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
        assert release.wait(timeout=2)
        real_fsync(descriptor)

    monkeypatch.setattr("vonk_control.agent_api.os.fsync", slow_fsync)
    headers = agent_headers(NODE_A, "serial-a") | {
        "content-type": "application/x-tar",
        "x-vonk-image-digest": "sha256:" + "d" * 64,
        "x-vonk-oci-layout-sha256": layout_digest,
    }

    def observe_responsiveness() -> bool:
        assert entered.wait(timeout=1)
        responsive = health_completed.wait(timeout=0.25)
        release.set()
        return responsive

    async def exercise() -> tuple[object, object, bool]:
        async with AsyncClient(
            transport=ASGITransport(app=client.app), base_url="http://testserver"
        ) as async_client:

            async def health_request():
                response = await async_client.get("/api/v1/healthz")
                health_completed.set()
                return response

            with ThreadPoolExecutor(max_workers=1) as pool:
                observer = pool.submit(observe_responsiveness)
                upload = asyncio.create_task(
                    async_client.put(
                        f"/agent/v1/recipe-builds/{build_id}/image",
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
                return upload_response, health_response, observer.result(timeout=1)

    upload_response, health_response, responsive = asyncio.run(exercise())

    assert responsive
    assert health_response.status_code == 200
    assert upload_response.status_code == 204


def admin_headers(codec: TokenCodec, role: str = "administrator") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {codec.issue(Actor(role, role), ttl_seconds=100, now=0)}"
    }


def enrollment_grant(services: AgentApiServices) -> str:
    return services.enrollment.create(NODE_C, "administrator", 60).token


def assert_grant_consumed(services: AgentApiServices, token: str) -> None:
    with pytest.raises(EnrollmentDenied, match="consumed"):
        services.enrollment.submit(token, b"", {})


def valid_enrollment_body(token: str) -> bytes:
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
                "observation_receipt_public_key": "d" * 64,
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
        authority_revision="a" * 64,
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
        jobs=Jobs(), tokens=TokenCodec(b"k" * 32), audits=MemoryAuditStore()
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
    with services.sessions.begin() as session:
        session.add(
            AgentNodeProfile(
                node_id=NODE_A,
                display_name=NODE_A,
                hostname="",
                lifecycle="ready",
                labels={},
            )
        )

    response = client.post(
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={
            "capabilities": CAPABILITIES,
            "hostname": "spark-3542",
            "lease_seconds": 30,
            "node_id": NODE_A,
            "protocol_version": 3,
            "runtime_identity": PACKAGED_RUNTIME_IDENTITY,
            "wait_seconds": 0,
        },
    )

    assert response.status_code == 204
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.last_seen_at.replace(tzinfo=UTC) == clock.now
        assert node.protocol_version == 3
        assert node.capabilities == CAPABILITIES
        assert node.semantic_version == "1.2.3"
        assert node.build_digest == "sha256:" + "b" * 64
        assert node.architecture == "linux-amd64"
        assert node.binary_digest == "c" * 64
        assert node.self_test_passed is True
        assert node.contact_certificate_serial == "serial-a"
        assert node.contact_observation_digest is not None
        profile = session.get(AgentNodeProfile, NODE_A)
        assert profile is not None
        assert profile.hostname == "spark-3542"
    metrics = MetricsRegistry()
    OperationalMetricsCollector(metrics, services.sessions, clock=clock).refresh()
    rendered = metrics.render()
    assert f'vonk_agent_last_seen_age_seconds{{node_id="{NODE_A}"}} 0' in rendered
    assert (
        f'vonk_agent_version_compatibility{{node_id="{NODE_A}",version_bucket="supported"}} 1'
        in rendered
    )


def test_authenticated_claim_accepts_model_uninstall_capability(agent_system) -> None:
    client, services, _, _ = agent_system
    response = client.post(
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={
            "capabilities": [*CAPABILITIES, "recipe.model-uninstall.v1"],
            "lease_seconds": 30,
            "node_id": NODE_A,
            "protocol_version": 3,
            "runtime_identity": PACKAGED_RUNTIME_IDENTITY,
            "wait_seconds": 0,
        },
    )

    assert response.status_code == 204
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert "recipe.model-uninstall.v1" in node.capabilities


@pytest.mark.parametrize(
    "hostname",
    ("", "-spark", "spark_3542", "spark 3542", "spark..lab", "a" * 256),
)
def test_authenticated_claim_rejects_invalid_reported_hostname(
    agent_system, hostname: str
) -> None:
    client, _services, _, _clock = agent_system

    response = client.post(
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"hostname": hostname},
    )

    assert response.status_code == 422


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
        "self_test_passed": True,
        "observation_receipt_public_key": "d" * 64,
        removed_field: removed_value,
    }

    response = client.post(
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"node_id": NODE_A, "runtime_identity": runtime_identity},
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
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={
            "node_id": NODE_A,
            "runtime_identity": {
                "architecture": architecture,
                "binary_digest": "c" * 64,
                "build_digest": "sha256:" + "b" * 64,
                "semantic_version": "1.2.3",
                "self_test_passed": True,
                "observation_receipt_public_key": "d" * 64,
            },
        },
    )

    assert response.status_code == 204


def test_authenticated_claim_requires_packaged_runtime_identity(agent_system) -> None:
    client, _services, _, _clock = agent_system

    response = client.request(
        "POST",
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"capabilities": CAPABILITIES, "node_id": NODE_A},
    )

    assert response.status_code == 422


def test_claim_rejects_failed_runtime_self_test(agent_system) -> None:
    client, _services, _, _clock = agent_system

    response = client.post(
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={
            "node_id": NODE_A,
            "runtime_identity": {
                "architecture": "linux-arm64",
                "binary_digest": "c" * 64,
                "build_digest": "sha256:" + "c" * 64,
                "semantic_version": "1.2.3",
                "self_test_passed": False,
            },
        },
    )

    assert response.status_code == 422


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
                "architecture": architecture,
                "binary_digest": "c" * 64,
                "build_digest": "sha256:" + "c" * 64,
                "semantic_version": "1.2.3",
                "self_test_passed": True,
                "observation_receipt_public_key": "d" * 64,
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
                "architecture": "linux-arm64",
                "binary_digest": "c" * 64,
                "build_digest": "sha256:" + "b" * 64,
                "semantic_version": "1.2.3",
                "self_test_passed": True,
                "observation_receipt_public_key": "d" * 64,
            },
        },
    )

    assert response.status_code in {401, 403}
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.architecture is None


def test_unknown_claim_capability_is_ignored_while_known_capabilities_negotiate(
    agent_system,
) -> None:
    client, services, _, _ = agent_system

    response = client.post(
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={
            "capabilities": CAPABILITIES + ["shell.exec"],
            "lease_seconds": 30,
            "node_id": NODE_A,
            "protocol_version": 3,
            "wait_seconds": 0,
        },
    )

    assert response.status_code == 204
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        assert node.capabilities == CAPABILITIES
        assert node.protocol_version == 3


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
        "capabilities": CAPABILITIES,
        "lease_seconds": 30,
        "node_id": NODE_A,
        "protocol_version": 3,
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
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json=payload,
    )

    assert response.status_code == 422


@pytest.mark.parametrize("field", ("lease_seconds", "wait_seconds"))
def test_claim_rejects_string_encoded_numeric_fields(
    agent_system,
    field: str,
) -> None:
    client, _services, _, _clock = agent_system
    response = client.post(
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={
            "capabilities": CAPABILITIES,
            "node_id": NODE_A,
            "protocol_version": 3,
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
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"protocol_version": 3},
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
        assert node.protocol_version == 3
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
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"protocol_version": 3},
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
    } | {"state": "succeeded", "result": {"stopped": True}}

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
        assert node.protocol_version == 3
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
        "result": {"status": "failed", "error_code": "stop_failed"},
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
            "protocol_version": 3,
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
        "result": {"stopped": True},
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
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.stop",
        "a" * 64,
        STOP_PAYLOAD,
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
        "result": {"stopped": True},
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
        "result": {"stopped": True},
    }
    assert (
        client.post(
            "/agent/v1/result", headers=agent_headers(NODE_A, "serial-a"), json=stale
        ).status_code
        == 409
    )


def test_enrollment_grant_is_admin_only_and_submission_immediately_issues_idempotently(
    agent_system,
) -> None:
    client, services, codec, _ = agent_system
    assert (
        client.post(
            "/api/v1/agents/enrollments/grants",
            headers=admin_headers(codec, "operator"),
            json={"ttl_seconds": 60},
        ).status_code
        == 403
    )
    grant = client.post(
        "/api/v1/agents/enrollments/grants",
        headers=admin_headers(codec),
        json={"ttl_seconds": 60},
    ).json()
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
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    body = {
        "grant_token": grant["token"],
        "csr": csr.decode(),
        "evidence": {
            "node_id": NODE_C,
            "csr_public_key_fingerprint": hashlib.sha256(public).hexdigest(),
            "host_key_fingerprint": "host",
            "hardware_fingerprint": "hardware",
            "agent_digest": "a" * 64,
            "boot_id": "boot",
            "observation_receipt_public_key": "d" * 64,
        },
    }
    first = client.post("/agent/v1/enroll", json=body)
    replay = client.post("/agent/v1/enroll", json=body)
    assert first.status_code == replay.status_code == 200
    assert first.content == replay.content == canonical_message(first.json())
    assert first.json()["node_id"] == NODE_C
    assert "certificate_pem" in first.json()
    with services.sessions() as session:
        assert session.get(AgentNode, NODE_C).observation_receipt_public_key == "d" * 64


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

    response = client.get("/agent/v1/bootstrap")

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

    current = client.get("/agent/v1/bootstrap")
    setup = client.get("/agent/v1/bootstrap?setup_schema=1")

    assert current.status_code == setup.status_code == 200
    assert current.json() == setup.json()
    assert setup.json()["host_helper_authority_public_key"] == "11" * 32
    assert setup.content == canonical_message(setup.json())
    assert "PRIVATE KEY" not in setup.text


def test_bootstrap_requires_the_host_helper_authority(
    agent_system,
) -> None:
    client, _, _, _ = agent_system

    response = client.get("/agent/v1/bootstrap")

    assert response.status_code == 503
    assert response.json() == {"detail": "host runtime authority is unavailable"}


def test_exact_recipe_run_observation_grant_api_is_strict_and_authenticated(
    agent_system,
) -> None:
    client, services, _, clock = agent_system

    class ExactObservationAuthority:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def issue_recipe_run_observation_grant(self, **values):
            self.calls.append(values)
            assert values["certificate_serial"] == "serial-a"
            assert values["expires_in_seconds"] == 10
            return "f" * 64, SignedHostHelperGrant(
                schema_version=1,
                claims=HostHelperGrantClaims(
                    schema_version=1,
                    authority="vonk.host-maintenance-helper",
                    request_id="70000000-0000-4000-8000-000000000007",
                    node_id=NODE_A,
                    issued_at=1_800_000_000,
                    expires_at=1_800_000_010,
                    operation=ExecuteContainerRuntimeRequestOperation(
                        type="execute-container-runtime-request",
                        action="run-inspect",
                        job_id=values["job_id"],
                        operation_id=values["operation_id"],
                        attempt=values["attempt"],
                        fence=values["fence"],
                        request_sha256=values["request_sha256"],
                        observation_identity_sha256="f" * 64,
                    ),
                ),
                signature=HostHelperSignature(
                    algorithm="ed25519", key_id="0" * 64, value="0" * 128
                ),
            )

    authority = ExactObservationAuthority()
    object.__setattr__(services, "host_runtime_authority", authority)
    run_id = "10000000-0000-4000-8000-000000000001"
    request = {
        "schema_version": 1,
        "node_id": NODE_A,
        "run_id": run_id,
        "installation_id": "20000000-0000-4000-8000-000000000002",
        "recipe_revision_id": "30000000-0000-4000-8000-000000000003",
        "recipe_content_sha256": "a" * 64,
        "mapping_id": "40000000-0000-4000-8000-000000000004",
        "mapping_generation": 2,
        "run_generation": 3,
        "image_digest": "b" * 64,
        "artifact_set_digest": "c" * 64,
        "model_identity": "publisher/model@revision",
        "rank": 1,
        "role": "worker",
        "world_size": 2,
        "local_address": "192.168.100.3",
        "master_address": "192.168.100.2",
        "master_port": 29500,
        "port": 8888,
        "runtime_arguments_sha256": "d" * 64,
        "job_id": run_id,
        "operation_id": "50000000-0000-4000-8000-000000000005",
        "attempt": 3,
        "fence": "60000000-0000-4000-8000-000000000006",
        "request_sha256": "e" * 64,
        "expires_in_seconds": 10,
    }

    accepted = client.post(
        "/agent/v1/recipe-runs/observation-grants",
        headers=agent_headers(NODE_A, "serial-a"),
        json=request,
    )
    wrong_node = client.post(
        "/agent/v1/recipe-runs/observation-grants",
        headers=agent_headers(NODE_A, "serial-a"),
        json={**request, "node_id": NODE_B},
    )
    unknown_field = client.post(
        "/agent/v1/recipe-runs/observation-grants",
        headers=agent_headers(NODE_A, "serial-a"),
        json={**request, "command": "docker inspect"},
    )
    maximum_model_identity = "p/" + "m" * 951 + "@" + "r" * 70
    maximum_identity = client.post(
        "/agent/v1/recipe-runs/observation-grants",
        headers=agent_headers(NODE_A, "serial-a"),
        json={**request, "model_identity": maximum_model_identity},
    )
    oversized_identity = client.post(
        "/agent/v1/recipe-runs/observation-grants",
        headers=agent_headers(NODE_A, "serial-a"),
        json={**request, "model_identity": maximum_model_identity + "x"},
    )

    assert accepted.status_code == 200
    assert accepted.json() == {
        "schema_version": 1,
        "observation_identity_sha256": "f" * 64,
        "grant": {
            "schema_version": 1,
            "claims": {
                "schema_version": 1,
                "authority": "vonk.host-maintenance-helper",
                "request_id": "70000000-0000-4000-8000-000000000007",
                "node_id": NODE_A,
                "issued_at": 1_800_000_000,
                "expires_at": 1_800_000_010,
                "operation": {
                    "type": "execute-container-runtime-request",
                    "action": "run-inspect",
                    "job_id": run_id,
                    "operation_id": request["operation_id"],
                    "attempt": request["attempt"],
                    "fence": request["fence"],
                    "request_sha256": request["request_sha256"],
                    "observation_identity_sha256": "f" * 64,
                },
            },
            "signature": {
                "algorithm": "ed25519",
                "key_id": "0" * 64,
                "value": "0" * 128,
            },
        },
    }
    assert wrong_node.status_code == 409
    assert unknown_field.status_code == 422
    assert maximum_identity.status_code == 200
    assert oversized_identity.status_code == 422
    assert len(maximum_model_identity) == 1024
    assert len(authority.calls) == 2

    identity_fields = {
        key: value
        for key, value in request.items()
        if key
        not in {
            "job_id",
            "operation_id",
            "attempt",
            "fence",
            "request_sha256",
            "expires_in_seconds",
        }
    }
    naive_item_time = client.post(
        "/agent/v1/recipe-runs/observations",
        headers=agent_headers(NODE_A, "serial-a"),
        json={
            "schema_version": 2,
            "observed_at": clock.now.isoformat(),
            "runs": [
                {
                    **identity_fields,
                    "observed_at": clock.now.replace(tzinfo=None).isoformat(),
                    "observation_identity_sha256": "f" * 64,
                    "endpoint_ready": None,
                    "grant": {"schema_version": 1},
                    "helper_receipt": {"schema_version": 1},
                }
            ],
        },
    )
    assert naive_item_time.status_code == 422
    assert "timezone-aware" in naive_item_time.text

    starting_run_id = "70000000-0000-4000-8000-000000000007"
    with services.sessions.begin() as session:
        session.add(
            RecipeRun(
                id=starting_run_id,
                installation_id="80000000-0000-4000-8000-000000000008",
                mapping_id="90000000-0000-4000-8000-000000000009",
                mapping_generation=1,
                run_generation=1,
                alias="starting-exact",
                plan_digest="1" * 64,
                plan={"schema_version": 1, "observation_schema_version": 2},
                state="starting",
                route_state="withdrawn",
                actor="admin",
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
        session.add(
            RunNode(
                run_id=starting_run_id,
                node_id=NODE_A,
                rank=0,
                role="entrypoint",
                state="starting",
                port=8888,
                reserved_memory_bytes=1,
                updated_at=clock.now,
            )
        )
    starting_request = {
        **request,
        "run_id": starting_run_id,
        "job_id": starting_run_id,
        "run_generation": 1,
        "attempt": 1,
    }
    too_early = client.post(
        "/agent/v1/recipe-runs/observation-grants",
        headers=agent_headers(NODE_A, "serial-a"),
        json=starting_request,
    )
    assert too_early.status_code == 425
    assert too_early.json() == {"detail": "recipe run observation is not ready"}
    assert len(authority.calls) == 2


def test_obsolete_enrollment_decision_routes_are_not_exposed(agent_system) -> None:
    client, _, codec, _ = agent_system
    headers = admin_headers(codec)

    assert (
        client.post(
            "/api/v1/agents/enrollments/unknown/approve", headers=headers
        ).status_code
        == 404
    )
    assert client.post("/agent/v1/bootstrap").status_code == 405
    assert client.get("/agent/v1/enroll").status_code == 405
    assert (
        client.post(
            "/api/v1/agents/enrollments/unknown/reject",
            headers=headers,
            json={"reason": "obsolete"},
        ).status_code
        == 404
    )


def test_enrollment_grant_ttl_accepts_nine_hundred_and_rejects_above_contract(
    agent_system,
) -> None:
    client, _, codec, _ = agent_system

    accepted = client.post(
        "/api/v1/agents/enrollments/grants",
        headers=admin_headers(codec),
        json={"ttl_seconds": 900},
    )
    rejected = client.post(
        "/api/v1/agents/enrollments/grants",
        headers=admin_headers(codec),
        json={"ttl_seconds": 901},
    )

    assert accepted.status_code == 201
    assert rejected.status_code == 422


def test_enrollment_grant_returns_configured_origins_and_controller_ca_fingerprint(
    agent_system,
) -> None:
    client, services, codec, _ = agent_system
    assert services.bootstrap is not None

    grant = client.post(
        "/api/v1/agents/enrollments/grants",
        headers=admin_headers(codec),
        json={"ttl_seconds": 60},
    )

    assert grant.status_code == 201
    body = grant.json()
    assert {
        key: body[key]
        for key in (
            "controller_endpoint",
            "enrollment_endpoint",
            "ca_fingerprint",
            "controller_address",
            "service_hostnames",
            "installer_url",
        )
    } == {
        "controller_endpoint": "https://agents.example.test:8443",
        "enrollment_endpoint": "https://enroll.example.test:8443",
        "ca_fingerprint": services.bootstrap.ca_fingerprint,
        "controller_address": "192.168.1.231",
        "service_hostnames": [
            "control.example.test",
            "enroll.example.test",
            "agents.example.test",
            "registry.example.test",
        ],
        "installer_url": "https://install.vonkforge.ai/spark",
    }


def test_reenrollment_grant_is_explicit_and_bound_to_the_selected_node(
    agent_system,
) -> None:
    client, services, codec, _ = agent_system
    response = client.post(
        "/api/v1/agents/enrollments/grants",
        headers=admin_headers(codec),
        json={"ttl_seconds": 60, "purpose": "re-enroll", "node_id": NODE_A},
    )

    assert response.status_code == 201
    assert response.json()["purpose"] == "re-enroll"
    with services.sessions() as session:
        grant = session.get(AgentEnrollmentGrant, response.json()["id"])
        assert grant is not None
        assert grant.node_id == NODE_A
        assert grant.purpose == "re-enroll"

    invalid = client.post(
        "/api/v1/agents/enrollments/grants",
        headers=admin_headers(codec),
        json={"ttl_seconds": 60, "purpose": "new-node", "node_id": NODE_A},
    )
    assert invalid.status_code == 422


@pytest.mark.parametrize(
    ("new_receipt_key", "invalidated"),
    (("d" * 64, False), ("e" * 64, True)),
    ids=("same-key-preserves-evidence", "changed-key-invalidates-evidence"),
)
def test_reenrollment_submission_reconciles_observation_receipt_key_through_api(
    agent_system, new_receipt_key: str, invalidated: bool
) -> None:
    client, services, codec, _ = agent_system
    run_id = "70000000-0000-4000-8000-000000000070"
    with services.sessions.begin() as session:
        session.get(AgentNode, NODE_A).observation_receipt_public_key = "d" * 64
        session.add(
            RecipeRouteAuthority(
                authority_id=RECIPE_ROUTE_AUTHORITY_ID,
                created_at=services.clock(),
            )
        )
        session.add(
            RoutePublication(
                authority_id=RECIPE_ROUTE_AUTHORITY_ID,
                state="completed",
                generation=1,
                plan_digest="5" * 64,
            )
        )
        session.add(
            RecipeRun(
                id=run_id,
                installation_id="80000000-0000-4000-8000-000000000080",
                mapping_id="90000000-0000-4000-8000-000000000090",
                mapping_generation=1,
                run_generation=1,
                alias="receipt-rotation",
                plan_digest="1" * 64,
                plan={"schema_version": 1, "observation_schema_version": 2},
                state="running",
                route_state="published",
                actor="admin",
                created_at=services.clock(),
                updated_at=services.clock(),
            )
        )
        session.add(
            RunNode(
                run_id=run_id,
                node_id=NODE_A,
                rank=0,
                role="entrypoint",
                state="running",
                port=8888,
                reserved_memory_bytes=1,
                evidence_digest="2" * 64,
                observed_run_generation=1,
                observation_receipt_sha256="3" * 64,
                observation_endpoint_ready=True,
                updated_at=services.clock(),
            )
        )
    grant = client.post(
        "/api/v1/agents/enrollments/grants",
        headers=admin_headers(codec),
        json={"ttl_seconds": 60, "purpose": "re-enroll", "node_id": NODE_A},
    ).json()
    csr = _csr_for(NODE_A)

    response = client.post(
        "/agent/v1/enroll",
        json={
            "grant_token": grant["token"],
            "csr": csr.decode(),
            "evidence": {
                "node_id": NODE_A,
                "csr_public_key_fingerprint": _csr_fingerprint(csr),
                "host_key_fingerprint": "host-a",
                "hardware_fingerprint": "hardware-a",
                "agent_digest": "a" * 64,
                "boot_id": "boot-a",
                "observation_receipt_public_key": new_receipt_key,
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["node_id"] == NODE_A
    assert response.json()["generation"] == 2
    with services.sessions() as session:
        node = session.get(AgentNode, NODE_A)
        run = session.get(RecipeRun, run_id)
        run_node = session.query(RunNode).filter_by(run_id=run_id).one()
        publication = session.get(RoutePublication, RECIPE_ROUTE_AUTHORITY_ID)
        assert node.observation_receipt_public_key == new_receipt_key
        assert run.route_state == "published"
        if invalidated:
            assert publication.state == "withdrawal-pending"
            assert run_node.state == "failed"
            assert run_node.observed_run_generation is None
            assert run_node.observation_receipt_sha256 is None
            assert run_node.observation_endpoint_ready is None
        else:
            assert publication.state == "completed"
            assert run_node.state == "running"
            assert run_node.observed_run_generation == 1
            assert run_node.observation_receipt_sha256 == "3" * 64
            assert run_node.observation_endpoint_ready is True


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

    assert response.status_code == 200
    assert response.json()["node_id"] == NODE_C
    event = audits.for_request(response.headers["x-request-id"])
    assert event.action == "agent.enrollment.submit.approved"
    assert event.targets[1] == NODE_C
    assert body["grant_token"] not in repr(event)


def test_uncertain_enrollment_provider_write_returns_503_without_reissuing(
    agent_system, monkeypatch
) -> None:
    client, services, _, _ = agent_system
    calls = 0

    def fail_issue(*_args: object, **_kwargs: object) -> IssuedCertificate:
        nonlocal calls
        calls += 1
        raise RuntimeError("provider response lost")

    monkeypatch.setattr(services.enrollment._authority, "issue_node", fail_issue)
    monkeypatch.setattr(services.enrollment, "_issuance_replay_wait_seconds", 0)
    body = json.loads(valid_enrollment_body(enrollment_grant(services)))

    first = client.post("/agent/v1/enroll", json=body)
    replay = client.post("/agent/v1/enroll", json=body)

    assert first.status_code == replay.status_code == 503
    assert calls == 1
    event = client.app.state.test_audits.for_request(first.headers["x-request-id"])
    assert event.action == "agent.enrollment.submit.uncertain"
    assert body["grant_token"] not in repr(event)


@pytest.mark.parametrize("source", ["published", "controller-build"])
def test_agent_runtime_spec_binds_canonical_plan_and_image_receipt(
    agent_system, source: str
) -> None:
    client, services, _, clock = agent_system
    document, digest = _canonical_recipe_fixture(f"agent-spec-{source}", source=source)
    recipe_id = str(uuid.uuid4())
    revision_id = str(uuid.uuid4())
    mapping_id = str(uuid.uuid4())
    installation_id = str(uuid.uuid4())
    build_id = str(uuid.uuid4()) if source == "controller-build" else None
    payload = _compiled_plan_fixture(digest, source=source, build_id=build_id)
    image = payload["runtime_image"]
    image_digest = image["image_digest"]
    with services.sessions.begin() as session:
        session.add(
            CatalogDocument(
                id=recipe_id,
                kind="recipe",
                publisher="vonk-forge",
                slug=document["identity"]["slug"],
                title=document["metadata"]["title"],
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
                slug=document["identity"]["slug"],
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
            ClusterMapping(
                id=mapping_id,
                recipe_revision_id=revision_id,
                topology_name=document["topology"]["name"],
                generation=1,
                node_count=1,
                state="ready",
                parameters={},
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
        if build_id is not None:
            session.add(
                RecipeBuild(
                    id=build_id,
                    recipe_revision_id=revision_id,
                    builder_node_id=NODE_A,
                    source_bundle_sha256="a" * 64,
                    build_input_sha256="f" * 64,
                    state="succeeded",
                    policy_report={"passed": True},
                    plan={"schema_version": 2},
                    image_digest=image_digest,
                    oci_layout_sha256=image["oci_layout_sha256"],
                    image_bytes=image["image_bytes"],
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
                plan={"compiled_execution_plans": {NODE_A: payload}},
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

    endpoint = f"/agent/v1/recipe-installations/{installation_id}/spec"
    response = client.get(endpoint, headers=agent_headers(NODE_A, "serial-a"))
    assert response.status_code == 409
    assert (
        response.json()["detail"] == "recipe specification execution receipts are stale"
    )

    receipt_id = str(uuid.uuid4())
    with services.sessions.begin() as session:
        session.add(
            RuntimeImageReceipt(
                id=receipt_id,
                recipe_revision_id=revision_id,
                source=source,
                original_content_digest=digest,
                effective_execution_key=payload["identity"]["execution_sha256"],
                registry_manifest_digest=image["registry_manifest_digest"],
                platform_manifest_digest=image["platform_manifest_digest"],
                local_image_config_id=image["local_image_config_id"],
                oci_archive_sha256=image["oci_layout_sha256"],
                image_bytes=image["image_bytes"],
                architecture=image["architecture"],
                runtime_interface=image["runtime_interface"],
                runtime_interface_label=image["runtime_interface_label"],
                build_id=build_id,
                verified_at=clock.now,
                state="verified",
            )
        )
        session.add(
            RuntimeImageAuthorization(
                recipe_revision_id=revision_id,
                receipt_id=receipt_id,
                source=source,
                original_content_digest=digest,
                effective_execution_key=payload["identity"]["execution_sha256"],
                registry_manifest_digest=image["registry_manifest_digest"],
                platform_manifest_digest=image["platform_manifest_digest"],
                local_image_config_id=image["local_image_config_id"],
                oci_archive_sha256=image["oci_layout_sha256"],
                image_bytes=image["image_bytes"],
                build_id=build_id,
                authorized_at=clock.now,
                state="authorized",
            )
        )

    resolved = client.get(endpoint, headers=agent_headers(NODE_A, "serial-a"))
    assert resolved.status_code == 200
    assert resolved.json() == payload
    assert resolved.json()["schema_version"] == 2
    assert resolved.content == canonical_message(
        AgentCompiledExecutionPlan.model_validate(payload)
    )
    openapi = client.get("/openapi.json").json()
    spec_route = openapi["paths"][
        "/agent/v1/recipe-installations/{installation_id}/spec"
    ]["get"]
    schema_ref = spec_route["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    assert schema_ref.startswith("#/components/schemas/")
    resolved_schema = openapi["components"]["schemas"][schema_ref.rsplit("/", 1)[-1]]
    canonical_schema = AgentCompiledExecutionPlan.model_json_schema()
    assert resolved_schema["type"] == canonical_schema["type"] == "object"
    assert (
        resolved_schema["additionalProperties"]
        == canonical_schema["additionalProperties"]
        == False
    )
    assert resolved_schema["required"] == canonical_schema["required"]
    assert set(resolved_schema["properties"]) == set(canonical_schema["properties"])

    tampered = copy.deepcopy(payload)
    tampered["runtime_image"]["local_image_config_id"] = "sha256:" + "0" * 64
    with services.sessions.begin() as session:
        installation = session.get(RecipeInstallation, installation_id)
        assert installation is not None
        installation.plan = {"compiled_execution_plans": {NODE_A: tampered}}
    rejected = client.get(endpoint, headers=agent_headers(NODE_A, "serial-a"))
    assert rejected.status_code == 409
    assert (
        rejected.json()["detail"] == "recipe specification execution receipts are stale"
    )

    with services.sessions.begin() as session:
        installation = session.get(RecipeInstallation, installation_id)
        assert installation is not None
        installation.plan = {"compiled_execution_plans": {NODE_A: payload}}
        if build_id is not None:
            build = session.get(RecipeBuild, build_id)
            assert build is not None
            build.image_bytes = 999
        else:
            installation.recipe_build_id = str(uuid.uuid4())
    rejected_build_authority = client.get(
        endpoint, headers=agent_headers(NODE_A, "serial-a")
    )
    assert rejected_build_authority.status_code == 409
    assert (
        rejected_build_authority.json()["detail"]
        == "recipe specification execution receipts are stale"
    )
    assert (
        client.get(
            f"/agent/v1/recipe-installations/{uuid.uuid4()}/spec",
            headers=agent_headers(NODE_A, "serial-a"),
        ).status_code
        == 404
    )
    assert (
        client.get(endpoint, headers=agent_headers(NODE_B, "serial-b")).status_code
        == 404
    )
    assert client.get(endpoint).status_code == 401


def test_exact_enrollment_replay_returns_certificate_and_mismatch_is_denied(
    agent_system,
) -> None:
    client, services, _, _ = agent_system
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
            "observation_receipt_public_key": "d" * 64,
        },
    }
    issued = client.post("/agent/v1/enroll", json=body)
    pickup = client.post("/agent/v1/enroll", json=body)
    mismatch = client.post(
        "/agent/v1/enroll",
        json={**body, "evidence": {**body["evidence"], "boot_id": "different"}},
    )

    assert issued.status_code == pickup.status_code == 200
    assert issued.content == pickup.content
    assert pickup.content == canonical_message(pickup.json())
    assert pickup.json()["generation"] == 1
    assert "certificate_pem" in pickup.json()
    assert mismatch.status_code == 403
    assert "certificate" not in mismatch.text.lower()


def test_human_enrollment_mutations_audit_only_grant_and_revocation(
    agent_system,
) -> None:
    client, _services, codec, _clock = agent_system
    headers = admin_headers(codec)
    audits = client.app.state.test_audits

    grant_response = client.post(
        "/api/v1/agents/enrollments/grants",
        headers=headers,
        json={"ttl_seconds": 60},
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
                "observation_receipt_public_key": "d" * 64,
            },
        },
    )
    revocation = client.post(
        f"/api/v1/agents/nodes/{NODE_A}/revoke",
        headers=headers,
    )

    assert [
        grant_response.status_code,
        pending.status_code,
        revocation.status_code,
    ] == [201, 200, 204]
    expected = {
        grant_response.headers["x-request-id"]: (
            "agent.enrollment.grant.create",
            (),
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
        assert event.authority_revision is None
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
    assert [response.status_code for response in failures] == [422, 404, 404, 404]
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
        "/agent/v1/result", headers=agent_headers(NODE_A, "serial-a"), json=result
    )

    assert response.status_code == 204
    with services.sessions() as session:
        attempt = (
            session.query(AgentOperationAttempt).filter_by(fence=claim["fence"]).one()
        )
        parent_job = session.get(Job, claim["job_id"])
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
        assert evidence.tick()
        content, _, bundle = evidence.read(claim["operation_id"], claim["attempt"])
        assert "should-never-persist" not in content.decode()
        assert "/proc: permission denied" in bundle.diagnostics.stderr.text


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
    } | {"state": "failed", "result": {"unexpected": "unstructured failure"}}

    response = client.post(
        "/agent/v1/result", headers=agent_headers(NODE_A, "serial-a"), json=result
    )

    assert response.status_code == 422
    with services.sessions() as session:
        attempt = (
            session.query(AgentOperationAttempt).filter_by(fence=claim["fence"]).one()
        )
        assert attempt.state == "running"
        assert attempt.result is None


def test_recipe_job_failure_uses_its_typed_exit_result_at_authenticated_ingress(agent_system):
    import json
    from pathlib import Path

    client, services, _, clock = agent_system
    vectors = Path(__file__).parents[2] / "agent_protocol/src/vonk_agent_protocol/vectors"
    request = json.loads((vectors / "recipe-job-run-claim-v1.json").read_text())["payload"]
    job_result = json.loads((vectors / "recipe-job-run-result-v1.json").read_text())["result"]
    services.operations.enqueue(
        parent(services.sessions, clock).id, NODE_A, "recipe.job.run.v1", "a" * 64, request,
    )
    claim_response = client.post("/agent/v1/claim", headers=agent_headers(NODE_A, "serial-a"))
    assert claim_response.status_code == 200
    claim = claim_response.json()
    result = {key: claim[key] for key in ("schema_version", "job_id", "operation_id", "attempt", "fence", "node_id", "deadline")}
    result.update(state="failed", result=dict(job_result, exit_code=1, reason="runtime failed"))
    response = client.post("/agent/v1/result", headers=agent_headers(NODE_A, "serial-a"), json=result)
    assert response.status_code == 204
    with services.sessions() as session:
        attempt = session.query(AgentOperationAttempt).filter_by(fence=claim["fence"]).one()
        assert attempt.state == "failed"
        assert attempt.result["exit_code"] == 1


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
            parent_job.id, NODE_A, "recipe.stop", "a" * 64, STOP_PAYLOAD
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
        agent=services,
        enrollment_rate_limiter=limiter,
    )
    assert (
        asgi_post(
            app, "/agent/v1/enroll", valid_enrollment_body(enrollment_grant(services))
        )[0]
        == 200
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

    assert status_code == 200
    assert json.loads(response)["node_id"] == NODE_C
    assert "certificate_pem" in json.loads(response)


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


def test_enrollment_rejects_malformed_observation_receipt_public_key(
    agent_system,
) -> None:
    client, services, _, _ = agent_system
    token = enrollment_grant(services)
    body = json.loads(valid_enrollment_body(token))
    body["evidence"]["observation_receipt_public_key"] = "not-lower-hex"

    response = client.post("/agent/v1/enroll", json=body)

    assert response.status_code == 403
    assert_grant_consumed(services, token)


def test_enrollment_rejects_receipt_less_evidence(agent_system) -> None:
    client, services, _, _ = agent_system
    token = enrollment_grant(services)
    body = json.loads(valid_enrollment_body(token))
    body["evidence"].pop("observation_receipt_public_key")

    response = client.post("/agent/v1/enroll", json=body)

    assert response.status_code == 403
    assert_grant_consumed(services, token)


def test_artifact_access_is_owned_content_addressed_and_range_bounded(
    agent_system,
) -> None:
    client, services, _, clock = agent_system
    digest = hashlib.sha256(b"artifact").hexdigest()
    from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

    storage = FilesystemRuntimeImageStorage(services.artifact_root)
    (storage.root / digest).write_bytes(b"artifact")
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.image.import.v1",
        "a" * 64,
        image_import_payload(digest),
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


def test_recipe_image_range_does_not_snapshot_the_complete_archive(
    agent_system, monkeypatch
) -> None:
    client, services, _, clock = agent_system
    payload = b"accepted recipe image archive"
    digest = hashlib.sha256(payload).hexdigest()
    from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

    storage = FilesystemRuntimeImageStorage(services.artifact_root)
    (storage.root / digest).write_bytes(payload)
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.image.import.v1",
        "a" * 64,
        {
            "schema_version": 1,
            "kind": "recipe.image.import.v1",
            "build_id": "00000000-0000-4000-8000-000000000010",
            "mapping_id": "00000000-0000-4000-8000-000000000011",
            "mapping_generation": 1,
            "source_node_id": NODE_A,
            "image_digest": "sha256:" + "b" * 64,
            "oci_layout_sha256": digest,
            "image_bytes": len(payload),
        },
    )

    def fail_snapshot(*_args, **_kwargs):
        raise AssertionError("recipe image ranges must not snapshot the archive")

    monkeypatch.setattr("vonk_control.agent_api._sealed_snapshot", fail_snapshot)
    response = client.get(
        f"/agent/v1/artifacts/{digest}",
        headers={**agent_headers(NODE_A, "serial-a"), "Range": "bytes=1-3"},
    )

    assert (
        response.status_code,
        response.content,
        response.headers["content-range"],
    ) == (206, payload[1:4], f"bytes 1-3/{len(payload)}")


def test_artifact_symlink_is_never_served(agent_system, tmp_path) -> None:
    client, services, _, clock = agent_system
    digest = "a" * 64
    from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

    storage = FilesystemRuntimeImageStorage(services.artifact_root)
    (storage.root / digest).symlink_to(tmp_path / "outside")
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.image.import.v1",
        "a" * 64,
        image_import_payload(digest),
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
        "agent.upgrade.v1",
        "a" * 64,
        {
            "schema_version": 1,
            "architecture": "linux-arm64",
            "package_bytes": 8,
            "package_sha256": digest,
            "package_signature": "a" * 128,
            "package_url": "https://install.vonkforge.ai/releases/test/vonk-forge-agent.deb",
            "package_version": "1.2.3",
            "target_binary_digest": "b" * 64,
            "target_build_digest": "sha256:" + "c" * 64,
        },
    )
    assert (
        client.get(
            f"/agent/v1/artifacts/{digest}", headers=agent_headers(NODE_A, "serial-a")
        ).status_code
        == 404
    )


def test_retired_agent_update_tuf_routes_are_absent(agent_system) -> None:
    client, _, _, _ = agent_system
    headers = agent_headers(NODE_A, "serial-a")
    paths = client.get("/openapi.json").json()["paths"]

    assert not any(path.startswith("/agent/v1/tuf/") for path in paths)
    assert (
        client.get("/agent/v1/tuf/metadata/timestamp.json", headers=headers).status_code
        == 404
    )
    assert (
        client.get(
            f"/agent/v1/tuf/targets/platform/releases/1.2.3/{'a' * 64}.json",
            headers=headers,
        ).status_code
        == 404
    )


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


def test_invalid_ranges_do_not_leak_artifact_descriptors(agent_system) -> None:
    client, services, _, clock = agent_system
    digest = hashlib.sha256(b"artifact").hexdigest()
    from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

    storage = FilesystemRuntimeImageStorage(services.artifact_root)
    (storage.root / digest).write_bytes(b"artifact")
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.image.import.v1",
        "a" * 64,
        image_import_payload(digest),
    )
    fd_directory = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    before = len(os.listdir(fd_directory))
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
    assert len(os.listdir(fd_directory)) <= before + 1


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
        "/agent/v1/telemetry",
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
        json={"ttl_seconds": 60},
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
        json={"ttl_seconds": 60},
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
                    state="issuing" if index == 0 else "certificate_issued",
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
    assert all(
        set(item)
        == {
            "id",
            "node_id",
            "state",
            "csr_public_key_fingerprint",
            "host_key_fingerprint",
            "hardware_fingerprint",
            "agent_digest",
            "boot_id",
            "created_at",
            "certificate_serial",
            "certificate_fingerprint",
        }
        for item in first["enrollments"]
    )
    second = client.get(
        f"/api/v1/agents/enrollments?limit=100&cursor={first['next_cursor']}",
        headers=admin_headers(codec),
    ).json()
    assert len(second["enrollments"]) == 1
    issuing = client.get(
        "/api/v1/agents/enrollments?state=issuing", headers=admin_headers(codec)
    ).json()
    assert [item["state"] for item in issuing["enrollments"]] == ["issuing"]


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
        reference = paths[f"/agent/v1/{path}"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        document = components[reference.rsplit("/", 1)[-1]]
        assert document["title"] == model.__name__
        assert set(document["required"]) == set(model.model_json_schema()["required"])

    for path, model in (("claim", AgentClaim), ("heartbeat", AgentDirective)):
        reference = paths[f"/agent/v1/{path}"]["post"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        assert components[reference.rsplit("/", 1)[-1]]["title"] == model.__name__
    assert "204" in paths["/agent/v1/claim"]["post"]["responses"]

    # A compact serializer must not erase the outgoing progress structure.
    progress = OperationProgress.model_json_schema(mode="serialization")
    assert progress["additionalProperties"] is False
    assert {"phase", "completed_bytes", "checkpoint", "members"} <= set(
        progress["properties"]
    )


@pytest.mark.parametrize(
    "missing",
    (
        "capabilities",
        "lease_seconds",
        "node_id",
        "protocol_version",
        "runtime_identity",
        "wait_seconds",
        "observation_receipt_public_key",
    ),
)
def test_claim_requires_the_current_agent_document(agent_system, missing: str) -> None:
    client, _services, _sessions, _clock = agent_system
    body = {
        "capabilities": CAPABILITIES,
        "lease_seconds": 30,
        "node_id": NODE_A,
        "protocol_version": 3,
        "runtime_identity": dict(PACKAGED_RUNTIME_IDENTITY),
        "wait_seconds": 0,
    }
    if missing == "observation_receipt_public_key":
        del body["runtime_identity"][missing]
    else:
        del body[missing]
    # Use the raw request method: the convenience test client must not refill
    # deliberately missing fields and hide a production boundary regression.
    response = client.request(
        "POST",
        "/agent/v1/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json=body,
    )
    assert response.status_code == 422


def test_enrollment_openapi_exposes_the_runtime_request_contract(agent_system) -> None:
    from vonk_agent_protocol.enrollment import EnrollmentSubmitRequest

    client, _services, _sessions, _clock = agent_system
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/agent/v1/enroll"]["post"]
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
