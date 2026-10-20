"""Committed agent owners survive transport loss, restart and commit faults."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, update
from sqlalchemy.exc import OperationalError
from vonk_agent_protocol import AgentClaim, AgentProgress, AgentResult, AgentResultState
from vonk_agent_protocol.contracts import AgentOperation as OperationKind
from vonk_agent_protocol.inventory import InventoryRequest
from vonk_agent_protocol.recipe_operations import RecipeStopResult
from vonk_control.agent_jobs import AgentJobService
from vonk_control.api import create_app
from vonk_control.models import (
    AgentCertificate,
    AgentOperationAttempt,
    NodeInventorySnapshot,
    NodeTelemetrySample,
    RunNode,
)
from vonk_control.operator_receipt_contract import AcceptanceReceipt, ReceiptCode

from .test_agent_api import (
    NODE_A,
    NODE_C,
    PACKAGED_RUNTIME_IDENTITY,
    STOP_PAYLOAD,
    Jobs,
    agent_headers,
    enrollment_grant,
    make_agent_system,
    parent,
    telemetry_payload,
    valid_enrollment_body,
)
from .test_operator_acceptance_postgres import DropFirstResponse


def _inventory(clock):
    return InventoryRequest(
        schema_version=1,
        observed_at=clock.now,
        disk_total_bytes=10**12,
        disk_free_bytes=8 * 10**11,
        host_memory_total_bytes=128 * 10**9,
        host_memory_free_bytes=90 * 10**9,
        gpu_memory_total_bytes=128 * 10**9,
        gpu_memory_free_bytes=90 * 10**9,
        gpu_count=1,
        memory_pool="shared",
        artifact_store_read_only=False,
        capabilities=[],
        nvidia_driver_version="580.65.06",
        container_runtime_version="28.3.3",
    ).model_dump(mode="json", exclude_none=True)


def _run_observation(services, engine, clock):

    from .test_catalog_revision_collection import Catalog

    world = Catalog(engine)
    _, run_id = world.workload(
        world.revision("observed", 1), touched=clock.now - timedelta(seconds=1)
    )
    with services.sessions.begin() as session:
        node = session.scalar(select(RunNode).where(RunNode.run_id == run_id))
        assert node is not None
        node.node_id = NODE_A
    return {
        "observed_at": clock.now.isoformat(),
        "runs": [
            {
                "run_id": run_id,
                "run_generation": 1,
                "process_running": True,
                "endpoint_ready": True,
            }
        ],
    }


def _restart(services, codec):
    # New service and app instances, same PostgreSQL/store authorities.
    return TestClient(
        create_app(
            jobs=Jobs(),
            tokens=codec,
            now=lambda: 0,
            agent=replace(
                services,
                operations=AgentJobService(services.sessions, clock=services.clock),
            ),
            trusted_agent_proxy_auth=b"p" * 32,
        )
    )


def _reference(response):
    assert 200 <= response.status_code < 300, response.text
    return AcceptanceReceipt.model_validate_json(response.headers["x-vonk-receipt"])


@pytest.mark.parametrize(
    "family",
    [
        "claim",
        "inventory",
        "telemetry",
        "result",
        "heartbeat",
        "observations",
        "enrollment",
    ],
)
def test_lost_agent_response_reuses_existing_owner_after_restart(
    tmp_path, postgres_engine, family
):
    """Catches duplicate claims/snapshots/issuance or an acknowledgement without commit."""
    client, services, codec, clock = make_agent_system(tmp_path, engine=postgres_engine)
    headers = agent_headers(NODE_A, "serial-a") | {"X-Request-ID": str(uuid4())}
    path = f"/agent/{family}"
    if family in {"claim", "result", "heartbeat"}:
        services.operations.enqueue(
            parent(services.sessions, clock).id,
            NODE_A,
            OperationKind.RECIPE_STOP,
            "a" * 64,
            STOP_PAYLOAD,
        )
        body = {
            "protocol_version": 4,
            "runtime_identity": PACKAGED_RUNTIME_IDENTITY,
            "wait_seconds": 0,
        }
        if family != "claim":
            claim = AgentClaim.model_validate_json(
                client.post("/agent/claim", headers=headers).content
            )
            body = (
                AgentResult(
                    fence=claim.fence,
                    state=AgentResultState.SUCCEEDED,
                    result=RecipeStopResult(),
                )
                if family == "result"
                else AgentProgress(fence=claim.fence)
            ).model_dump(mode="json")
    elif family == "inventory":
        body = _inventory(clock)
    elif family == "telemetry":
        body = telemetry_payload(clock)
    elif family == "observations":
        path = "/agent/recipe-runs/observations"
        body = _run_observation(services, postgres_engine, clock)
    else:
        path = "/agent/enroll"
        body = json.loads(valid_enrollment_body(enrollment_grant(services)))
        headers = {"X-Request-ID": str(uuid4())}
    lost = TestClient(DropFirstResponse(client.app, path))
    with pytest.raises(httpx.ReadError):
        lost.post(path, json=body, headers=headers)
    with services.sessions() as session:
        if family in {"claim", "heartbeat", "result"}:
            assert (
                session.scalar(select(func.count()).select_from(AgentOperationAttempt))
                == 1
            )
        elif family == "inventory":
            assert (
                session.scalar(select(func.count()).select_from(NodeInventorySnapshot))
                == 1
            )
        elif family == "telemetry":
            assert (
                session.scalar(select(func.count()).select_from(NodeTelemetrySample))
                == 1
            )
        elif family == "observations":
            assert (
                session.scalar(
                    select(RunNode.observation_observed_at).where(
                        RunNode.node_id == NODE_A
                    )
                )
                == clock.now
            )
        elif family == "enrollment":
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AgentCertificate)
                    .where(AgentCertificate.node_id == NODE_C)
                )
                == 1
            )
    restarted = _restart(services, codec)
    receipt = _reference(restarted.post(path, json=body, headers=headers))
    assert _reference(restarted.post(path, json=body, headers=headers)) == receipt
    if family == "inventory":
        changed = restarted.post(
            path, json={**body, "disk_free_bytes": 42}, headers=headers
        )
        assert changed.status_code == 422
    elif family == "claim":
        changed = restarted.post(
            path, json={**body, "preflight_fingerprint": "b" * 64}, headers=headers
        )
        assert changed.status_code == 422
        with services.sessions() as session:
            assert (
                session.scalar(select(func.count()).select_from(AgentOperationAttempt))
                == 1
            )


@pytest.mark.parametrize(
    "family",
    [
        "claim",
        "inventory",
        "telemetry",
        "result",
        "heartbeat",
        "observations",
        "enrollment",
    ],
)
def test_agent_commit_failure_never_emits_success_then_recovers(
    tmp_path, postgres_engine, family
):
    """Catches returning 204 before commit or poisoning the same authorized retry."""
    client, services, codec, clock = make_agent_system(tmp_path, engine=postgres_engine)
    headers = agent_headers(NODE_A, "serial-a") | {"X-Request-ID": str(uuid4())}
    path = f"/agent/{family}"
    if family in {"claim", "result", "heartbeat"}:
        services.operations.enqueue(
            parent(services.sessions, clock).id,
            NODE_A,
            OperationKind.RECIPE_STOP,
            "a" * 64,
            STOP_PAYLOAD,
        )
        body = {
            "protocol_version": 4,
            "runtime_identity": PACKAGED_RUNTIME_IDENTITY,
            "wait_seconds": 0,
        }
        if family != "claim":
            claim = AgentClaim.model_validate_json(
                client.post("/agent/claim", headers=headers).content
            )
            body = {"fence": claim.fence}
            if family == "result":
                body |= {
                    "state": AgentResultState.SUCCEEDED,
                    "result": {},
                }
    elif family == "inventory":
        body = _inventory(clock)
    elif family == "telemetry":
        body = telemetry_payload(clock)
    elif family == "observations":
        path = "/agent/recipe-runs/observations"
        body = _run_observation(services, postgres_engine, clock)
    else:
        path = "/agent/enroll"
        body = json.loads(valid_enrollment_body(enrollment_grant(services)))
        headers = {}

    commit_attempts = []

    def fail_commit(session):
        if not session.in_nested_transaction():
            commit_attempts.append(True)
            raise OperationalError("COMMIT", None, RuntimeError("injected commit loss"))

    event.listen(services.sessions, "before_commit", fail_commit)
    try:
        response = TestClient(client.app, raise_server_exceptions=False).post(
            path, json=body, headers=headers
        )
        assert response.status_code >= 400
        assert "x-vonk-receipt" not in response.headers
    finally:
        event.remove(services.sessions, "before_commit", fail_commit)
    assert commit_attempts
    _reference(_restart(services, codec).post(path, json=body, headers=headers))


@pytest.mark.usefixtures("damaged_json_rows")
def test_large_inventory_and_damaged_history_do_not_block_exact_ingress(
    tmp_path, postgres_engine
):
    """Catches global catalogue/history validation and repeated parsing of large inventories."""
    client, services, codec, clock = make_agent_system(tmp_path, engine=postgres_engine)
    headers = agent_headers(NODE_A, "serial-a")
    body = _inventory(clock)
    from vonk_agent_protocol import canonical_message

    from cluster_profiles.control_limits import MAX_CONTROL_DOCUMENT_BYTES

    item_bytes = len(canonical_message({"sha256": "f" * 64, "bytes": 1024**2})) + 1
    # Exercise most of the shared transport budget; the Rust producer drops
    # optional model evidence beyond it while preserving mandatory capacity.
    body["model_objects"] = [
        {"sha256": hashlib.sha256(str(i).encode()).hexdigest(), "bytes": 1024**2}
        for i in range((MAX_CONTROL_DOCUMENT_BYTES * 4 // 5) // item_bytes)
    ]
    assert (
        MAX_CONTROL_DOCUMENT_BYTES * 3 // 4
        < len(canonical_message(body))
        < MAX_CONTROL_DOCUMENT_BYTES
    )
    old = _reference(client.post("/agent/inventory", json=body, headers=headers))
    # JSON damage is historical and outside this request's owner.
    with services.sessions.begin() as session:
        session.execute(
            update(NodeInventorySnapshot)
            .where(NodeInventorySnapshot.id == old.id)
            .values(model_objects=[{"damaged": True}])
        )
    clock.now += timedelta(seconds=1)
    body["observed_at"] = clock.now.isoformat()
    queries = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        queries.append(statement.lower())

    event.listen(postgres_engine, "before_cursor_execute", capture)
    try:
        receipt = _reference(
            client.post("/agent/inventory", json=body, headers=headers)
        )
        _reference(client.post("/agent/claim", headers=headers))
    finally:
        event.remove(postgres_engine, "before_cursor_execute", capture)
    assert receipt.id != old.id
    assert not any("catalog_document" in query for query in queries)
    assert all(
        "where" in query
        for query in queries
        if query.startswith("select") and "node_inventory_snapshots" in query
    )
    assert (
        _reference(
            _restart(services, codec).post(
                "/agent/inventory", json=body, headers=headers
            )
        )
        == receipt
    )


def test_schema_valid_result_without_committed_report_is_refused(
    tmp_path, postgres_engine, monkeypatch
):
    """Catches an unchanged 204 from a no-op result writer on the mounted result class."""
    client, services, _, clock = make_agent_system(tmp_path, engine=postgres_engine)
    headers = agent_headers(NODE_A, "serial-a")
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        OperationKind.RECIPE_STOP,
        "a" * 64,
        STOP_PAYLOAD,
    )
    claim = client.post("/agent/claim", headers=headers).json()
    monkeypatch.setattr(
        services.operations, "record_result", lambda *_args, **_kwargs: None
    )
    response = client.post(
        "/agent/result",
        headers=headers,
        json={
            "fence": claim["fence"],
            "state": AgentResultState.SUCCEEDED,
            "result": {},
        },
    )
    assert response.status_code == 409
    assert "x-vonk-receipt" not in response.headers


def test_image_upload_commits_binding_after_publication_and_recovers(
    tmp_path, postgres_engine
):
    """Catches a successful PUT with bytes published but no committed build identity."""
    from vonk_agent_protocol.agent_words import ProgressPhase
    from vonk_control.models import RecipeBuild

    from .stored_documents_support import valid_policy_report
    from .test_catalog_revision_collection import Catalog

    client, services, codec, clock = make_agent_system(tmp_path, engine=postgres_engine)
    world = Catalog(postgres_engine)
    revision_id = world.revision("receipt-upload", 1)
    build_id = str(uuid4())
    with services.sessions.begin() as session:
        session.add(
            RecipeBuild(
                id=build_id,
                recipe_revision_id=revision_id,
                builder_node_id=NODE_A,
                source_bundle_sha256="a" * 64,
                build_input_sha256="b" * 64,
                state=ProgressPhase.BUILDING,
                policy_report=valid_policy_report(),
                plan={},
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
    payload = b"verified image archive"
    digest = hashlib.sha256(payload).hexdigest()
    headers = agent_headers(NODE_A, "serial-a") | {
        "content-type": "application/x-tar",
        "x-vonk-image-digest": "sha256:" + "d" * 64,
        "x-vonk-oci-layout-sha256": digest,
        "x-vonk-image-bytes": str(len(payload)),
    }
    path = f"/agent/recipe-builds/{build_id}/image"

    def fail_commit(session):
        raise OperationalError(
            "COMMIT", None, RuntimeError("publication before SQL fault")
        )

    event.listen(services.sessions, "before_commit", fail_commit)
    try:
        failed = TestClient(client.app, raise_server_exceptions=False).put(
            path, headers=headers, content=payload
        )
        assert failed.status_code >= 400
        assert "x-vonk-receipt" not in failed.headers
    finally:
        event.remove(services.sessions, "before_commit", fail_commit)
    assert (services.artifact_root / "image-cache" / digest).read_bytes() == payload
    with services.sessions() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None and build.oci_layout_sha256 is None
    with pytest.raises(httpx.ReadError):
        TestClient(DropFirstResponse(client.app, path)).put(
            path, headers=headers, content=payload
        )
    replay = _restart(services, codec).put(path, headers=headers, content=payload)
    receipt = _reference(replay)
    assert receipt.id == build_id and receipt.content_digest == digest
    probe = _restart(services, codec).head(path, headers=headers)
    assert probe.headers["x-vonk-upload-complete"] == "true"


def test_lost_login_response_requires_fresh_authentication(tmp_path, postgres_engine):
    """Catches applying generic request replay to credential issuance at the security edge."""
    from sqlalchemy.orm import sessionmaker
    from vonk_control.auth import TokenCodec
    from vonk_control.browser_auth import BrowserAuthService
    from vonk_control.models import Base, LoginSession, User

    from .test_auth_api import ADMIN_PASSWORD, ADMIN_VERIFIER, NOW, ORIGIN

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            User(
                subject="admin", role="administrator", password_verifier=ADMIN_VERIFIER
            )
        )
    key = b"credential-edge-fault-key-32-bytes"

    def application():
        return create_app(
            jobs=Jobs(),
            tokens=TokenCodec(key),
            now=lambda: int(NOW.timestamp()),
            browser_auth=BrowserAuthService(
                sessions, token_signing_key=key, clock=lambda: NOW
            ),
        )

    path = "/api/auth/login"
    headers = {"origin": ORIGIN, "X-Request-ID": str(uuid4())}
    body = {"subject": "admin", "password": ADMIN_PASSWORD}
    with pytest.raises(httpx.ReadError):
        TestClient(DropFirstResponse(application(), path), base_url=ORIGIN).post(
            path, json=body, headers=headers
        )
    with sessions() as session:
        previous = session.scalar(select(LoginSession.digest))
        assert previous is not None
    response = TestClient(application(), base_url=ORIGIN).post(
        path, json=body, headers=headers
    )
    assert response.status_code == 200
    assert "x-vonk-receipt" not in response.headers
    with sessions() as session:
        digests = session.scalars(select(LoginSession.digest)).all()
        assert len(digests) == len(set(digests)) == 2
        assert previous in digests


def test_inventory_receipt_binds_measurements_and_stale_replay_cannot_rewind_sizes(
    tmp_path, postgres_engine
):
    """Catches snapshot/size dual commits and accepting changed bytes under the same report identity."""
    from vonk_control.models import InstallationNode

    from .test_catalog_revision_collection import Catalog

    client, services, codec, clock = make_agent_system(tmp_path, engine=postgres_engine)
    world = Catalog(postgres_engine)
    installation, _ = world.workload(world.revision("measured", 1))
    with services.sessions.begin() as session:
        rank = session.scalar(
            select(InstallationNode).where(
                InstallationNode.installation_id == installation
            )
        )
        assert rank is not None
        rank.node_id = NODE_A
    headers = agent_headers(NODE_A, "serial-a")
    first = _inventory(clock) | {
        "installation_bytes": [{"installation_id": installation, "bytes": 42}]
    }
    receipt = _reference(client.post("/agent/inventory", json=first, headers=headers))
    clock.now += timedelta(seconds=1)
    newer = _inventory(clock) | {
        "installation_bytes": [{"installation_id": installation, "bytes": 99}]
    }
    _reference(client.post("/agent/inventory", json=newer, headers=headers))
    assert (
        _reference(
            _restart(services, codec).post(
                "/agent/inventory", json=first, headers=headers
            )
        )
        == receipt
    )
    changed = first | {
        "installation_bytes": [{"installation_id": installation, "bytes": 1}]
    }
    assert (
        client.post("/agent/inventory", json=changed, headers=headers).status_code
        == 422
    )
    with services.sessions() as session:
        assert (
            session.scalar(
                select(InstallationNode.installed_bytes).where(
                    InstallationNode.installation_id == installation
                )
            )
            == 99
        )


def test_artifact_output_commit_fault_and_lost_response_reuse_attachment(
    tmp_path, postgres_engine, monkeypatch
):
    """Catches acknowledging a published blob before its exact job/file attachment commits."""
    from vonk_control.agent_api import AgentApiServices
    from vonk_control.artifact_blob_store import ArtifactBlobStore
    from vonk_control.artifact_jobs import ArtifactJobService
    from vonk_control.auth import TokenCodec
    from vonk_control.models import ArtifactJobFile
    from vonk_control.presence import AgentPresenceService, ManagementAddressPolicy
    from vonk_control.source_bundles import SourceBundleStore

    from .test_artifact_jobs import running_artifact_service, submitted_artifact_job
    from .test_recipe_operations import NOW

    sessions, operations, _queue, artifacts, run_id, node_id = running_artifact_service(
        tmp_path, engine=postgres_engine
    )
    job = submitted_artifact_job(artifacts, run_id, request_suffix=730)
    codec = TokenCodec(b"artifact-output-receipt-test-key32")
    services = AgentApiServices(
        enrollment=None,
        operations=AgentJobService(sessions, clock=lambda: NOW),
        sessions=sessions,
        clock=lambda: NOW,
        presence=AgentPresenceService(
            sessions, ManagementAddressPolicy.parse("192.168.1.0/24"), clock=lambda: NOW
        ),
        artifact_root=tmp_path / "agent-artifacts",
        source_bundles=SourceBundleStore(tmp_path / "source-bundles"),
    )

    def application(service):
        return create_app(
            jobs=Jobs(),
            tokens=codec,
            agent=services,
            artifact_jobs=service,
            trusted_agent_proxy_auth=b"p" * 32,
            now=lambda: 0,
        )

    headers = agent_headers(node_id, "serial-0") | {
        "x-vonk-agent-fingerprint": "fingerprint-0",
        "x-vonk-agent-source": "192.168.1.211",
        "X-Vonk-Artifact-Name": "result.png",
        "Content-Type": "image/png",
    }
    payload = b"png-output"
    digest = hashlib.sha256(payload).hexdigest()
    path = f"/agent/recipe-jobs/{job.id}/outputs/{digest}"
    client = TestClient(application(artifacts))
    original = artifacts.put_output_stream

    async def unchanged(*args, **kwargs):
        return None

    monkeypatch.setattr(artifacts, "put_output_stream", unchanged)
    assert client.put(path, headers=headers, content=payload).status_code == 409
    monkeypatch.setattr(artifacts, "put_output_stream", original)

    def fail_commit(session):
        raise OperationalError("COMMIT", None, RuntimeError("lost attachment commit"))

    event.listen(sessions, "before_commit", fail_commit)
    try:
        refused = TestClient(client.app, raise_server_exceptions=False).put(
            path, headers=headers, content=payload
        )
        assert refused.status_code >= 400
    finally:
        event.remove(sessions, "before_commit", fail_commit)
    with sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ArtifactJobFile)
                .where(
                    ArtifactJobFile.artifact_job_id == job.id,
                    ArtifactJobFile.name == "result.png",
                )
            )
            == 0
        )
    with pytest.raises(httpx.ReadError):
        TestClient(DropFirstResponse(client.app, path)).put(
            path, headers=headers, content=payload
        )
    restarted = ArtifactJobService(
        sessions,
        recipe_operations=operations,
        blob_store=ArtifactBlobStore(tmp_path / "artifact-blobs"),
        clock=lambda: NOW,
    )
    receipt = _reference(
        TestClient(application(restarted)).put(path, headers=headers, content=payload)
    )
    assert receipt.id == job.id and receipt.content_digest == digest
    with sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(ArtifactJobFile)
                .where(
                    ArtifactJobFile.artifact_job_id == job.id,
                    ArtifactJobFile.name == "result.png",
                )
            )
            == 1
        )


def test_invalid_claim_request_identity_is_refused_before_effects(
    tmp_path, postgres_engine
):
    """Catches replacing an invalid idempotency identity with a newly generated trace ID."""
    client, services, _, clock = make_agent_system(tmp_path, engine=postgres_engine)
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        OperationKind.RECIPE_STOP,
        "a" * 64,
        STOP_PAYLOAD,
    )
    response = client.post(
        "/agent/claim",
        headers=agent_headers(NODE_A, "serial-a") | {"X-Request-ID": "invalid"},
    )
    assert response.status_code == 422
    with services.sessions() as session:
        assert (
            session.scalar(select(func.count()).select_from(AgentOperationAttempt)) == 0
        )


def test_unknown_receipt_read_after_commit_preserves_owner_and_retry(
    tmp_path, postgres_engine, monkeypatch
):
    """Catches an unknown owner read being converted to a terminal internal-error response."""
    from vonk_agent_protocol import UnknownOutcomeError, WaitReason
    from vonk_control import agent_acceptance

    client, services, _, clock = make_agent_system(tmp_path, engine=postgres_engine)
    headers = agent_headers(NODE_A, "serial-a")
    body = _inventory(clock)
    original = agent_acceptance.resolve_agent_receipt

    def unknown_owner(*args, **kwargs):
        raise UnknownOutcomeError(
            "injected owner observation loss", reason=WaitReason.OBSERVATION_UNAVAILABLE
        )

    monkeypatch.setattr(agent_acceptance, "resolve_agent_receipt", unknown_owner)
    failed = client.post("/agent/inventory", json=body, headers=headers)
    assert failed.status_code == 503
    assert failed.headers["x-vonk-error-code"] == ReceiptCode.UNAVAILABLE
    assert 0 < int(failed.headers["retry-after"]) <= 300
    assert "x-vonk-receipt" not in failed.headers
    with services.sessions() as session:
        assert (
            session.scalar(select(func.count()).select_from(NodeInventorySnapshot)) == 1
        )
    monkeypatch.setattr(agent_acceptance, "resolve_agent_receipt", original)
    _reference(client.post("/agent/inventory", json=body, headers=headers))
    with services.sessions() as session:
        assert (
            session.scalar(select(func.count()).select_from(NodeInventorySnapshot)) == 1
        )
