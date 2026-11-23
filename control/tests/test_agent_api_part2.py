from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from httpx2 import ASGITransport, AsyncClient, Response
from sqlalchemy import event, select
from starlette.types import Message
from vonk_agent_protocol import (
    RunState,
    canonical_message,
)
from vonk_agent_protocol.agent_words import (
    RecipeRunDispositionValue,
)
from vonk_control import agent_operation_states as aos
from vonk_control.api import create_app
from vonk_control.auth import TokenCodec
from vonk_control.inventory.items import runtime_value
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperationAttempt,
    CatalogDocument,
    CatalogDocumentRevision,
    Job,
    RecipeBuild,
    RecipeRun,
    RunNode,
)
from vonk_forge_contracts import RecipeDefinition

from . import agent_api_support as _support
from .agent_api_support import (
    NODE_A,
    NODE_B,
    NODE_C,
    PACKAGED_RUNTIME_IDENTITY,
    STOP_PAYLOAD,
    Jobs,
    _canonical_recipe_fixture,
    agent_headers,
    enrollment_grant,
    parent,
    valid_enrollment_body,
)
from .node_contacts import contact_row
from .stored_documents_support import valid_policy_report

agent_system = _support.agent_system


@pytest.mark.parametrize("stored_damage", [False, True])
def test_builder_uploads_digest_verified_docker_archive_without_a_registry(
    agent_system,
    stored_damage,
    monkeypatch,
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
                title=recipe.identity.slug,
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

    # A probe cannot allocate or repair storage. Main created a partial from
    # HEAD alone, and a stale oversized partial was truncated by the read.
    def storage_snapshot():
        return sorted(
            (
                str(path.relative_to(services.artifact_root)),
                path.lstat().st_size,
                path.lstat().st_mtime_ns,
            )
            for path in services.artifact_root.rglob("*")
        )

    before_probe = storage_snapshot()
    empty_cursor = client.head(route, headers=headers)
    assert empty_cursor.status_code == 200
    assert empty_cursor.headers["x-vonk-upload-offset"] == "0"
    assert storage_snapshot() == before_probe
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
    partial = next((services.artifact_root / "image-cache").glob(".*.upload"))
    retained_partial = partial.read_bytes()
    partial.write_bytes(payload + b"oversized checkpoint fault")
    oversized = partial.read_bytes()
    assert client.head(route, headers=headers).headers["x-vonk-upload-offset"] == "0"
    assert partial.read_bytes() == oversized
    partial.write_bytes(retained_partial)
    before_probe = storage_snapshot()
    wrong_identity = client.head(
        route, headers=headers | {"x-vonk-image-digest": "sha256:" + "e" * 64}
    )
    assert wrong_identity.headers["x-vonk-upload-offset"] == "0"
    assert storage_snapshot() == before_probe
    assert client.put(route, headers=headers, content=payload).status_code == 409
    if stored_damage:
        destination = services.artifact_root / "image-cache" / layout_digest
        destination.write_bytes(b"damaged")
    from vonk_control.agent_api.common import _commit_recipe_image_upload

    active = set()

    def began(session, *_args):
        active.add(id(session))

    def ended(session, transaction):
        if transaction.parent is None:
            active.discard(id(session))

    def publish(*args, **kwargs):
        # Wrong implementation: even a short row transaction encloses slow IO.
        assert not active
        return _commit_recipe_image_upload(*args, **kwargs)

    event.listen(services.sessions.class_, "after_begin", began)
    event.listen(services.sessions.class_, "after_transaction_end", ended)
    monkeypatch.setattr(
        "vonk_control.agent_api.artifacts._commit_recipe_image_upload", publish
    )
    try:
        response = client.put(
            route, headers=headers | {"x-vonk-upload-offset": "7"}, content=payload[7:]
        )
    finally:
        event.remove(services.sessions.class_, "after_begin", began)
        event.remove(services.sessions.class_, "after_transaction_end", ended)
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
        competing = None
        try:
            competing = _prepare_recipe_image_upload(tmp_path, "a" * 64)
        except Exception:  # noqa: BLE001, S110 -- cursor preservation and fresh admission decide
            pass
        if competing is not None:
            os.close(competing[0])
        assert competing is None
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
                title=recipe.identity.slug,
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
        responsive = health_completed.wait(timeout=30)
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


def test_agent_routes_answer_401_when_agent_services_are_absent() -> None:
    """Fully forwarded agent headers still get 401 from the middleware.

    The agent route installers rely on this: without services no ``/agent/*``
    handler (other than bootstrap, enroll, renew/expired) can run.
    """
    app = create_app(
        jobs=Jobs(), tokens=TokenCodec(b"k" * 32), trusted_agent_proxy_auth=b"p" * 32
    )
    headers = {
        "x-vonk-agent-proxy-auth": "p" * 32,
        "x-vonk-agent-node": NODE_A,
        "x-vonk-agent-serial": "serial-a",
        "x-vonk-agent-fingerprint": "fingerprint",
        "x-vonk-agent-verified": "1",
        "x-vonk-agent-source": "192.0.2.1",
    }
    assert TestClient(app).post("/agent/claim", headers=headers).status_code == 401


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
        assert (
            contact_row(session.get(AgentNode, NODE_A)).management_observed_at is None
        )
        assert contact_row(session.get(AgentNode, NODE_A)).last_seen_at is None


def test_authenticated_claim_persists_certificate_bound_source(agent_system) -> None:
    client, services, _, clock = agent_system

    response = client.post("/agent/claim", headers=agent_headers(NODE_A, "serial-a"))

    assert response.status_code == 204
    with services.sessions() as session:
        presence = session.get(AgentNode, NODE_A)
        assert presence is not None
        assert contact_row(presence).contact_certificate_serial == "serial-a"
        assert (
            session.get(
                AgentCertificate, contact_row(presence).contact_certificate_serial
            ).fingerprint
            == "fingerprint-serial-a"
        )
        assert contact_row(presence).management_address == "10.0.0.42"
        observed = contact_row(presence).management_observed_at
        assert observed is not None and observed.replace(tzinfo=UTC) == clock.now


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
        assert session.get(AgentNode, NODE_A) is not None


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
        assert runtime_value(node, lambda identity: identity.semantic_version) is None


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
        assert runtime_value(node, lambda identity: identity.architecture) is None


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
        assert runtime_value(node, lambda identity: identity.architecture) is None


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
        contact_row(node).last_seen_at = None
    clock.now += timedelta(seconds=5)
    progress = {key: claim[key] for key in ("fence",)} | {
        "progress": {"phase": "checking"}
    }

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
        seen = contact_row(node).last_seen_at
        assert seen is not None and seen.replace(tzinfo=UTC) == clock.now
        assert contact_row(node).protocol_version == 4
        presence = session.get(AgentNode, NODE_A)
        assert presence is not None
        assert contact_row(presence).management_address == "10.0.0.43"
        observed = contact_row(presence).management_observed_at
        assert observed is not None and observed.replace(tzinfo=UTC) == clock.now


def test_authenticated_result_preserves_claim_advertised_protocol_after_exact_fence_validation(
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
        "/agent/claim",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"protocol_version": 4},
    ).json()
    with services.sessions.begin() as session:
        node = session.get(AgentNode, NODE_A)
        assert node is not None
        contact_row(node).last_seen_at = None
    clock.now += timedelta(seconds=5)
    result = {key: claim[key] for key in ("fence",)} | {
        "state": "succeeded",
        "result": {},
    }

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
        seen = contact_row(node).last_seen_at
        assert seen is not None and seen.replace(tzinfo=UTC) == clock.now
        assert contact_row(node).protocol_version == 4
        presence = session.get(AgentNode, NODE_A)
        assert presence is not None
        assert contact_row(presence).management_address == "10.0.0.44"
        observed = contact_row(presence).management_observed_at
        assert observed is not None and observed.replace(tzinfo=UTC) == clock.now


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
        assert contact_row(node).last_seen_at is None
        assert contact_row(node).protocol_version is None

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
        presence = session.get(AgentNode, NODE_A)
        assert node is not None
        assert presence is not None
        recorded = contact_row(presence).management_observed_at
        assert recorded is not None
        observed_at = recorded.replace(tzinfo=UTC)
        contact_row(node).last_seen_at = None
        contact_row(node).protocol_version = None
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
        assert contact_row(node).last_seen_at is None
        assert contact_row(node).protocol_version is None
        presence = session.get(AgentNode, NODE_A)
        assert presence is not None
        assert contact_row(presence).management_address == "10.0.0.42"
        observed = contact_row(presence).management_observed_at
        assert observed is not None and observed.replace(tzinfo=UTC) == observed_at


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
        assert contact_row(node).last_seen_at is None
        assert contact_row(node).protocol_version is None


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
    # A result for another node's fence is an authority edge (403), not a
    # verdict on the request's own bytes.
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
        == 403
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
    client, services, _, _ = agent_system

    response = client.get("/agent/bootstrap")

    assert response.status_code == 503
    assert "host_helper_authority_public_key" not in response.json()
    object.__setattr__(
        services,
        "host_runtime_authority",
        SimpleNamespace(public_key_document={"public_key": "11" * 32}),
    )
    accepted = client.get("/agent/bootstrap")
    assert accepted.status_code == 200
    assert accepted.json()["host_helper_authority_public_key"] == "11" * 32


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
        session.flush()
        session.add(
            RunNode(
                run_id=known_run_id,
                node_id=NODE_A,
                rank=0,
                role="main",
                state=RunState.FAILED,
                port=8000,
                reserved_memory_bytes=0,
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
    # A run wanted on A owns no files on B after re-enrollment/placement drift.
    foreign = disposition(known_run_id, agent_headers(NODE_B, "serial-b"))
    assert foreign.status_code == 204
    assert (
        foreign.headers.get("x-vonk-recipe-run-disposition")
        == RecipeRunDispositionValue.UNOWNED.value
    )
    assert "x-vonk-recipe-run-generation" not in foreign.headers
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

    from vonk_agent_protocol.failure_evidence import FailureDiagnostics

    from .host_memory_guard_support import guard_diagnostics

    diagnostics = guard_diagnostics(clock.now.isoformat())
    run: dict[str, object] = {
        "run_id": run_id,
        "run_generation": 2,
        "process_running": False,
        "endpoint_ready": None,
        "failure_diagnostics": json.loads(canonical_message(diagnostics)),
    }
    stale_generation = report(run | {"run_generation": 1})
    # Superseded content is acknowledged without effects, so a durable sender
    # can advance to a fresh inspection instead of retrying a stale generation.
    assert stale_generation.status_code == 204
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
        assert (
            FailureDiagnostics.model_validate_json(
                json.dumps(node.last_exit_diagnostics)
            )
            == diagnostics
        )

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

    # A delayed durable replay, even beyond the old five-minute acceptance
    # window, cannot undo the newer live observation or block the sender.
    clock.now += timedelta(minutes=10)
    assert report(run).status_code == 204
    with services.sessions() as session:
        node = session.scalar(select(RunNode).where(RunNode.run_id == run_id))
        assert node is not None
        assert node.state == RunState.RUNNING
        assert node.observation_failure_diagnostics is None
        assert node.observation_process_running is True
        assert node.observation_observed_at is not None
        assert node.observation_observed_at.replace(tzinfo=UTC) == recovered_at


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_guard_stop_of_a_serving_co_tenant_is_not_the_runs_failure(
    agent_system,
) -> None:
    """The guard stops every container on the Spark, innocent ones included.

    Two runs share NODE_A. The run that was serving (seen alive at its current
    generation) is a co-tenant: its stop is kept as exit evidence only, so
    recovery relaunches it as it would any lost rank. The run never seen alive
    keeps the guard stop as its failure. Catches settling the co-tenant for good.
    """
    from .host_memory_guard_support import guard_diagnostics

    client, services, _, clock = agent_system
    serving, tried = (
        "70000000-0000-4000-8000-0000000000a1",
        "70000000-0000-4000-8000-0000000000a2",
    )
    with services.sessions.begin() as session:
        for run_id in (serving, tried):
            session.add(
                RecipeRun(
                    id=run_id,
                    installation_id="80000000-0000-4000-8000-00000000000a",
                    mapping_id="90000000-0000-4000-8000-00000000000a",
                    mapping_generation=1,
                    run_generation=1,
                    alias=f"co-tenant-{run_id[-1]}",
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
                    port=8888 if run_id == serving else 8889,
                    reserved_memory_bytes=1,
                    updated_at=clock.now - timedelta(seconds=1),
                )
            )

    def report(run_id: str, *, running: bool, at: datetime):
        item: dict[str, object] = {
            "run_id": run_id,
            "run_generation": 1,
            "process_running": running,
            "endpoint_ready": None,
            "failure_diagnostics": None
            if running
            else json.loads(canonical_message(guard_diagnostics(at.isoformat()))),
        }
        return client.post(
            "/agent/recipe-runs/observations",
            headers=agent_headers(NODE_A, "serial-a"),
            json={"observed_at": at.isoformat(), "runs": [item]},
        )

    assert report(serving, running=True, at=clock.now).status_code == 204
    stop = clock.now + timedelta(seconds=5)
    for run_id in (serving, tried):
        assert report(run_id, running=False, at=stop).status_code == 204
    # A replay of the same stop changes nothing.
    again = stop + timedelta(seconds=5)
    for run_id in (serving, tried):
        assert report(run_id, running=False, at=again).status_code == 204
    with services.sessions() as session:
        kept = {
            run_id: session.scalar(select(RunNode).where(RunNode.run_id == run_id))
            for run_id in (serving, tried)
        }
    assert all(
        node is not None and node.state != RunState.RUNNING for node in kept.values()
    )
    co_tenant, own = kept[serving], kept[tried]
    assert co_tenant is not None and own is not None
    assert co_tenant.observation_failure_diagnostics is None
    assert co_tenant.last_exit_diagnostics is not None
    assert own.observation_failure_diagnostics is not None


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
