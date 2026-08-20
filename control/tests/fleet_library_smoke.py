from __future__ import annotations

import hashlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parents[2]
CONTROL_SRC = ROOT / "control" / "src"
if str(CONTROL_SRC) not in sys.path:
    sys.path.insert(0, str(CONTROL_SRC))

from vonk_control.agent_api import AgentApiServices
from vonk_control.agent_jobs import AgentJobService
from vonk_control.api import create_app
from vonk_control.audit import SqlAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.enrollment import EnrollmentService
from vonk_control.enrollment_bootstrap import EnrollmentBootstrapConfig
from vonk_control.fleet_projection import FleetProjection
from vonk_control.library_projection import LibraryProjection
from vonk_control.metrics import MetricsRegistry
from vonk_control.models import Base
from vonk_control.pki import CertificateAuthority, IssuedCertificate
from vonk_control.presence import AgentPresenceService, ManagementAddressPolicy
from vonk_control.source_bundles import SourceBundleStore

NODE_ID = "spk_" + "c" * 32


class _Authority(CertificateAuthority):
    def issue_node(self, node_id: str, public_key_pem: bytes, now: datetime) -> IssuedCertificate:
        return IssuedCertificate(node_id, b"certificate", b"chain", "serial-c", "fp-c", now, now + timedelta(days=1))

    def renew_node(self, node_id: str, public_key_pem: bytes, now: datetime, *, request_id: str) -> IssuedCertificate:
        return self.issue_node(node_id, public_key_pem, now)

    def revocation_bundle(self, now: datetime) -> bytes:
        return b""

    def revoke_node(self, serial: str, now: datetime) -> None:
        return None


class _AuthorityStore:
    def head(self) -> str:
        return "a" * 64


def _csr() -> bytes:
    key = ed25519.Ed25519PrivateKey.generate()
    return (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, NODE_ID)]))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.UniformResourceIdentifier(f"spiffe://vonk-forge.local/node/{NODE_ID}")]
            ),
            critical=False,
        )
        .sign(key, algorithm=None)
        .public_bytes(serialization.Encoding.PEM)
    )


def run_fresh_fleet_library_smoke() -> dict[str, object]:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    authority = _Authority()
    enrollment = EnrollmentService(sessions, authority, clock=lambda: now)
    presence = AgentPresenceService(
        sessions, ManagementAddressPolicy.parse("10.0.0.0/24"), clock=lambda: now
    )
    operations = AgentJobService(sessions, clock=lambda: now)
    operations.set_contact_consumer(presence.observe_in_session)
    root = Path("/tmp/vonk-fleet-library-smoke")
    root.mkdir(parents=True, exist_ok=True)
    services = AgentApiServices(
        enrollment=enrollment,
        operations=operations,
        sessions=sessions,
        clock=lambda: now,
        presence=presence,
        artifact_root=root / "artifacts",
        source_bundles=SourceBundleStore(root / "source-bundles"),
        workload_tuf_metadata_root=root / "workload-tuf-metadata",
        workload_tuf_target_root=root / "workload-tuf-targets",
        fabric_policy=ManagementAddressPolicy.parse("192.168.100.0/24"),
        bootstrap=EnrollmentBootstrapConfig(
            controller_endpoint="https://agents.example.test:8443",
            enrollment_endpoint="https://enroll.example.test:8443",
            ca_fingerprint="a" * 64,
        ),
    )
    for path in (
        services.artifact_root,
        services.workload_tuf_metadata_root,
        services.workload_tuf_target_root,
    ):
        path.mkdir(parents=True, exist_ok=True)
    projection = FleetProjection(_AuthorityStore(), sessions, clock=lambda: now)
    codec = TokenCodec(b"k" * 32)
    library_projection = LibraryProjection(
        sessions,
        cursors=codec.cursor_codec(),
        clock=lambda: now,
    )
    audits = SqlAuditStore(sessions, clock=lambda: now)
    app = create_app(
        jobs=operations,
        tokens=codec,
        audits=audits,
        fleet=lambda: projection.read().model_dump(mode="json"),
        fleet_projection=projection,
        library_projection=library_projection,
        now=lambda: int(now.timestamp()),
        agent=services,
        trusted_agent_proxy_auth=b"p" * 32,
        metrics=MetricsRegistry(),
    )
    client = TestClient(app)
    token = codec.issue(Actor("admin", "administrator"), ttl_seconds=600, now=int(now.timestamp()))
    headers = {"Authorization": f"Bearer {token}"}
    initial = client.get("/api/v1/fleet", headers=headers)
    assert initial.status_code == 200, initial.text

    grant = client.post(
        "/api/v1/agents/enrollments/grants",
        headers=headers,
        json={"ttl_seconds": 60},
    )
    assert grant.status_code == 201, grant.text
    csr = _csr()
    public = x509.load_pem_x509_csr(csr).public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    pending = client.post(
        "/agent/v1/enroll",
        json={
            "grant_token": grant.json()["token"],
            "csr": csr.decode(),
            "evidence": {
                "node_id": NODE_ID,
                "csr_public_key_fingerprint": hashlib.sha256(public).hexdigest(),
                "host_key_fingerprint": "host-c",
                "hardware_fingerprint": "hardware-c",
                "agent_digest": "c" * 64,
                "boot_id": "boot-c",
            },
        },
    )
    assert pending.status_code == 202, pending.text
    approval = client.post(
        f"/api/v1/agents/enrollments/{pending.json()['id']}/approve", headers=headers
    )
    assert approval.status_code == 200, approval.text
    active = client.get("/api/v1/fleet", headers=headers)
    assert active.status_code == 200, active.text
    node_ids = [node["id"] for node in active.json()["nodes"]]
    assert node_ids == [NODE_ID]

    revoked = client.post(f"/api/v1/agents/nodes/{NODE_ID}/revoke", headers=headers)
    assert revoked.status_code == 204, revoked.text
    after_revoke = client.get("/api/v1/fleet", headers=headers)
    assert after_revoke.status_code == 200
    audit = client.get("/api/v1/audit", headers=headers)
    history = client.get("/api/v1/identity-history", headers=headers)
    library = client.get("/api/v1/library", headers=headers)
    assert library.status_code == 200, library.text
    return {
        "initial_fleet_nodes": initial.json()["nodes"],
        "active_fleet_nodes": node_ids,
        "active_node_occurrences": node_ids.count(NODE_ID),
        "revoked_fleet_nodes": [node["id"] for node in after_revoke.json()["nodes"]],
        "audit_actions": [
            event["action"]
            for event in reversed(audit.json()["events"])
            if event["action"]
            in {
                "agent.enrollment.grant.create",
                "agent.enrollment.approve",
                "agent.node.revoke",
            }
        ],
        "identity_history_revoked": any(
            item["node_id"] == NODE_ID and item["revoked_at"] is not None
            for item in history.json()["identities"]
        ),
        "library_recipe_count": len(library.json().get("recipes", library.json().get("unlinked_recipes", []))),
    }
