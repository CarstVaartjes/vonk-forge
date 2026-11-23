"""Agent API: telemetry inventory."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Any, TypedDict, Unpack

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import select
from starlette.types import Message

from vonk_agent_protocol.contracts import AgentOperation as OperationKind
from vonk_agent_protocol.contracts import canonical_message
from vonk_agent_protocol.host_helper import (
    ContainerRuntimeAction,
    ExecuteContainerRuntimeRequestOperation,
    HostRuntimeRequest,
    InstallVonkDebOperation,
    RecipeReconciliationIdentity,
    SignedHostHelperGrant,
    host_helper_grant_signing_bytes,
)
from vonk_agent_protocol.inventory import (
    GpuHealth,
    HostSettings,
    InventoryRequest,
    InventoryVerdict,
    NetworkInterface,
)
from vonk_agent_protocol.package_upgrade import PackageRollbackAuthority
from vonk_control.host_helper_authority import (
    HostHelperGrantIssuer,
    HostRuntimeAuthorityService,
)
from vonk_control.models.execution import InstallationNode
from vonk_control.models.jobs import Job
from vonk_control.models.operations import AgentOperation, AgentOperationAttempt
from vonk_control.models.telemetry import NodeInventorySnapshot, NodeTelemetrySample
from vonk_control.telemetry.samples import TelemetryRepository

from .agent_api_support import (
    NODE_A,
    NODE_B,
    PACKAGED_RUNTIME_IDENTITY,
    STOP_PAYLOAD,
    agent_headers,
    parent,
    telemetry_payload,
)
from .agent_fences import fenced_operation
from .test_catalog_revision_collection import Catalog


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


@pytest.mark.parametrize("with_link", [False, True])
def test_agent_posts_authenticated_runtime_and_fabric_inventory(
    agent_system, with_link: bool
) -> None:
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
        "gpu_health": GpuHealth.NEEDS_RESET,
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
        "host_settings": {
            "display_manager_active": True,
            "graphical_default_target": False,
        },
        "unused_model_bytes": 123,
    }

    # Catch dropping new optional NIC evidence on ingress or persistence, while
    # accepting older reports that omit it. Compare contracts, never a copied
    # list of fields whose declared optional defaults may grow.
    if with_link:
        vector = InventoryRequest.model_validate_json(
            files("vonk_agent_protocol")
            .joinpath("vectors/inventory-wifi-link-v1.json")
            .read_bytes()
        )
        assert vector.network_interfaces is not None
        payload["network_interfaces"] = [
            NetworkInterface(name="enP7s7", kind="wired", carrier=False).model_dump(
                mode="json", exclude_none=True
            ),
            *[
                item.model_dump(mode="json", exclude_none=True)
                for item in vector.network_interfaces
            ],
        ]
    expected = InventoryRequest.model_validate_json(canonical_message(payload))

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
        assert (
            HostSettings.model_validate_json(canonical_message(row.host_settings))
            == expected.host_settings
        )
        assert row.gpu_health == expected.gpu_health
        assert row.unused_model_bytes == 123
        assert row.network_interfaces is not None
        assert [
            NetworkInterface.model_validate_json(canonical_message(item))
            for item in row.network_interfaces
        ] == expected.network_interfaces
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
        assert row.fabric_address is None
        assert row.fabric_bandwidth_mbps is None
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


def test_an_inventory_naming_installations_gets_the_controllers_verdict(
    agent_system,
) -> None:
    """Catches a Controller that judges nothing, or answers an older agent."""

    client, _, _, clock = agent_system
    forgotten = "5f0c1e2d-3b4a-4c5d-8e6f-7a8b9c0d1e2f"
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
        "capabilities": [],
        "nvidia_driver_version": "580.65.06",
        "container_runtime_version": "28.3.3",
    }

    older = client.post(
        "/agent/inventory", headers=agent_headers(NODE_A, "serial-a"), json=payload
    )
    judged = client.post(
        "/agent/inventory",
        headers=agent_headers(NODE_A, "serial-a"),
        json=payload
        | {
            "installations": [forgotten],
            "observed_at": (clock.now + timedelta(seconds=1)).isoformat(),
        },
    )

    assert older.status_code == 204
    assert older.content == b""
    assert judged.status_code == 200
    verdict = InventoryVerdict.model_validate_json(judged.content)
    assert verdict.untracked_installations == [forgotten]
    assert verdict.store_retention is not None
    assert verdict.store_retention.required_free_bytes == 0
    # An agent that named no runs gets no run verdict field (an older
    # agent reads the verdict strictly); one that names them gets it.
    runs = client.post(
        "/agent/inventory",
        headers=agent_headers(NODE_A, "serial-a"),
        json=payload
        | {
            "runs": [forgotten],
            "observed_at": (clock.now + timedelta(seconds=2)).isoformat(),
        },
    )
    assert runs.status_code == 200
    verdict = InventoryVerdict.model_validate_json(runs.content)
    assert verdict.untracked_installations == []
    assert verdict.untracked_runs == [forgotten]


def test_an_inventory_measuring_installations_refreshes_their_recorded_size(
    agent_system,
) -> None:
    """Catches a size the inventory measured but the record never takes."""

    client, services, _, clock = agent_system
    world = Catalog(services.sessions.kw["bind"])
    installation, _ = world.workload(world.revision("glm", 1), run="stopped")
    with services.sessions.begin() as session:
        rank = session.scalar(
            select(InstallationNode).where(
                InstallationNode.installation_id == installation
            )
        )
        assert rank is not None
        rank.node_id = NODE_A
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
        "capabilities": [],
        "nvidia_driver_version": "580.65.06",
        "container_runtime_version": "28.3.3",
        "installation_bytes": [{"installation_id": installation, "bytes": 4242}],
    }

    response = client.post(
        "/agent/inventory", headers=agent_headers(NODE_A, "serial-a"), json=payload
    )

    assert response.status_code == 204, response.text
    with services.sessions() as session:
        assert (
            session.scalar(
                select(InstallationNode.installed_bytes).where(
                    InstallationNode.installation_id == installation
                )
            )
            == 4242
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
    assert "gpu_count" in lines[0]
    assert "request_id" in lines[0]
    assert clock.now.isoformat() not in lines[0]


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize("peer_miss", [None, "once", "episode", "missing"])
def test_reconciliation_identity_survives_agent_api_and_signed_grant(
    agent_system, monkeypatch, peer_miss
):
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
    original_issue = authority.issue_grant
    attempts = []

    def observe_issue(**kwargs):
        attempts.append(kwargs["fence"])
        if peer_miss == "episode" or (peer_miss == "once" and len(attempts) == 1):
            raise KeyError("authority receipt temporarily unreadable")
        return original_issue(**kwargs)

    monkeypatch.setattr(authority, "issue_grant", observe_issue)
    removed = None
    if peer_miss == "missing":
        with services.sessions.begin() as session:
            removed = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.fence == request.fence
                )
            )
            session.delete(removed)
    if peer_miss in {"episode", "missing"}:
        ended = client.post(
            "/agent/host-runtime/grant",
            headers=agent_headers(NODE_A, "serial-a"),
            json=body,
        )
        assert ended.status_code == 503
        assert attempts == [request.fence] * 4
        assert "grant" not in ended.json()
        if removed is not None:
            from sqlalchemy.orm import make_transient

            make_transient(removed)
            with services.sessions.begin() as session:
                session.add(removed)
        monkeypatch.setattr(authority, "issue_grant", original_issue)
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
        == 403
    )
    assert (
        client.post(
            "/agent/host-runtime/grant",
            headers=agent_headers(NODE_A, "serial-a"),
            json=body | {"reconciliation_identity": None},
        ).status_code
        == 403
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

    operation = services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        OperationKind.RECIPE_STOP,
        "a" * 64,
        STOP_PAYLOAD,
    )
    claim = services.operations.claim(
        NODE_A,
        "serial-a",
        runtime_identity=PACKAGED_RUNTIME_IDENTITY,
        claim_fence=fence,
    )
    assert claim is not None
    assert fenced_operation(services.sessions, claim).id == operation.id

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


def test_internal_value_error_on_observations_is_a_wait_not_a_422(
    agent_system, monkeypatch
) -> None:
    # Wrong implementation: any ValueError answered 422, and the Spark settled
    # and discarded real evidence over a Controller bug.
    client, _services, _, clock = agent_system

    def broken(*_args, **_kwargs):
        raise ValueError("internal decode failure")

    monkeypatch.setattr("vonk_control.agent_api.observations.transition", broken)
    response = client.post(
        "/agent/recipe-runs/observations",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"observed_at": clock.now.isoformat(), "runs": []},
    )

    assert response.status_code == 503
    assert "retry-after" in response.headers


def _sample(clock, offset_seconds: int, **changes) -> dict[str, object]:
    samples: Any = telemetry_payload(
        clock, observed_at=clock.now - timedelta(seconds=offset_seconds)
    )["samples"]
    return samples[0] | changes


def test_telemetry_refusals_of_the_requests_own_bytes_answer_422(agent_system) -> None:
    client, _services, _, clock = agent_system
    headers = agent_headers(NODE_A, "serial-a")

    def post(*samples):
        return client.post(
            "/agent/telemetry", headers=headers, json={"samples": list(samples)}
        ).status_code

    # duplicate sample inside one batch
    assert post(_sample(clock, 10), _sample(clock, 10)) == 422
    # observation times that do not increase inside one batch
    assert post(_sample(clock, 10), _sample(clock, 20)) == 422
    # accepted, then the same boot and time with different content (conflict)
    assert post(_sample(clock, 30)) == 204
    assert post(_sample(clock, 30, disk_free_bytes=1)) == 422
    # a sample older than the stored head of the same boot (regression)
    assert post(_sample(clock, 40)) == 422


def test_inventory_identity_conflict_answers_422(agent_system) -> None:
    client, _services, _, clock = agent_system
    headers = agent_headers(NODE_A, "serial-a")
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
        "gpu_health": GpuHealth.NEEDS_RESET,
        "memory_pool": "separate",
        "artifact_store_read_only": False,
        "capabilities": ["runtime.vonk.v1"],
        "nvidia_driver_version": "580.65.06",
        "container_runtime_version": "28.3.3",
    }
    first = client.post("/agent/inventory", headers=headers, json=payload)
    assert first.status_code == 204, first.text
    changed = payload | {"disk_free_bytes": 600}
    assert (
        client.post("/agent/inventory", headers=headers, json=changed).status_code
        == 422
    )


def test_post_stop_inventory_makes_the_waiting_switch_due_on_arrival(
    agent_system, monkeypatch
) -> None:
    """Wrong implementation this catches: only the worker's run-switch tick
    checked whether the awaited inventory had landed, once per rotation, so a
    load waited 90-129 s after a stop for evidence that was already stored.
    The ingest makes the wait due as the inventory commits; a collection that
    is not newer than the 30 s post-stop floor wakes nothing, and a failing
    wake never costs the Spark its accepted report."""
    from vonk_control.agent_api import observations
    from vonk_control.run_switch_contract.operations import (
        RunSwitchAwaitedInventory,
        RunSwitchOperationResult,
    )
    from vonk_control.run_switch_operations.errors import (
        RunSwitchPostStopEvidencePending,
    )

    client, services, _, clock = agent_system
    floor = clock.now
    held_until = floor + timedelta(seconds=300)
    with services.sessions.begin() as session:
        job = Job(
            id=str(uuid.uuid4()),
            request_id=str(uuid.uuid4()),
            kind="recipe.run-switch.v2",
            state="running",
            actor="admin",
            authority_revision="a" * 64,
            targets=[NODE_A],
            payload_digest="a" * 64,
            payload={},
            created_at=floor,
            updated_at=floor,
            next_action_at=held_until,
            result=RunSwitchOperationResult(
                awaited_inventory=RunSwitchAwaitedInventory(
                    node_id=NODE_A, observed_after=floor
                ),
                retry_reason=RunSwitchPostStopEvidencePending.code,
            ).model_dump(mode="json"),
        )
        session.add(job)
    payload = {
        "schema_version": 1,
        "disk_total_bytes": 1000,
        "disk_free_bytes": 700,
        "host_memory_total_bytes": 2000,
        "host_memory_free_bytes": 1500,
        "gpu_memory_total_bytes": 1000,
        "gpu_memory_free_bytes": 800,
        "gpu_count": 1,
        "memory_pool": "separate",
        "artifact_store_read_only": False,
        "capabilities": ["runtime.vonk.v1"],
        "nvidia_driver_version": "580.65.06",
        "container_runtime_version": "28.3.3",
    }

    def report(observed_at: datetime) -> int:
        return client.post(
            "/agent/inventory",
            headers=agent_headers(NODE_A, "serial-a"),
            json=payload | {"observed_at": observed_at.isoformat()},
        ).status_code

    def due() -> datetime:
        with services.sessions() as session:
            stored = session.get(Job, job.id)
            assert stored is not None
            assert stored.next_action_at is not None
            return stored.next_action_at.replace(tzinfo=UTC)

    # A collection at the floor cannot settle the wait.
    assert report(floor) == 204
    assert due() == held_until
    # A wake that fails leaves the report accepted and the wait as it was.
    clock.now = floor + timedelta(seconds=5)

    def broken(*_args, **_kwargs):
        raise RuntimeError("wake unavailable")

    monkeypatch.setattr(observations, "wake_evidence_waits", broken, raising=False)
    assert report(clock.now) == 204
    assert due() == held_until
    monkeypatch.undo()
    # The next newer collection makes the switch due now, without a tick.
    clock.now = floor + timedelta(seconds=10)
    assert report(clock.now) == 204
    assert due() == clock.now


def test_an_internal_fault_waits_and_the_same_bytes_are_accepted_once_it_clears(
    agent_system, monkeypatch
) -> None:
    # Wrong implementation: the fault answered 422, the Spark discarded the
    # sample, and the evidence was lost for good.
    client, services, _, clock = agent_system
    headers = agent_headers(NODE_A, "serial-a")
    payload = telemetry_payload(clock)
    original = TelemetryRepository.record_batch
    failures = [ValueError("internal decode failure")]

    def flaky(self, *args, **kwargs):
        if failures:
            raise failures.pop()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(TelemetryRepository, "record_batch", flaky)
    waiting = client.post("/agent/telemetry", headers=headers, json=payload)
    assert waiting.status_code == 503
    assert "retry-after" in waiting.headers
    assert (
        client.post("/agent/telemetry", headers=headers, json=payload).status_code
        == 204
    )
    with services.sessions() as session:
        assert session.scalars(select(NodeTelemetrySample)).all()
