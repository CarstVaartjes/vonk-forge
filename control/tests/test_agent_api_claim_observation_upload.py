"""Agent API: claim observation upload."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, timedelta
from importlib.resources import files
from threading import Event

import pytest
from fastapi.testclient import TestClient
from httpx2 import ASGITransport, AsyncClient, Response
from sqlalchemy import event
from starlette.types import Message
from vonk_forge_contracts import RecipeDefinition, document_sha256

from tests.token_support import TokenCodec
from vonk_agent_protocol.reason_codes.profiles import ProjectionCode
from vonk_agent_protocol.reason_codes.sources import SourceBundleCode
from vonk_agent_protocol.recipe_observations import RecipeRunObservationsWire
from vonk_agent_protocol.wire_model import canonical_message
from vonk_control.agent_api.common import (
    _commit_recipe_image_upload,
    _prepare_recipe_image_upload,
)
from vonk_control.agent_jobs.contracts import AgentContactIdentityMismatch
from vonk_control.api.application import create_app
from vonk_control.categorized_errors import BookkeepingUnknown
from vonk_control.fleet_projection.service import FleetProjection
from vonk_control.inventory.items import runtime_value
from vonk_control.models.cache import RecipeBuild
from vonk_control.models.catalog import (
    CatalogDocument,
    CatalogDocumentRevision,
    RecipeSourceBundle,
)
from vonk_control.models.fleet import AgentCertificate, AgentNode
from vonk_control.source_bundles import SourceBundleUnknown, generate_source_bundle

from .agent_api_support import NODE_A, NODE_B, Jobs, agent_headers
from .node_contacts import contact_row
from .stored_documents_support import valid_policy_report


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


def test_claim_security_refusal_stays_403_and_unknown_outcome_keeps_its_wait(
    agent_system, monkeypatch
) -> None:

    client, services, _, _clock = agent_system
    headers = agent_headers(NODE_A, "serial-a")

    def refuse(*_args, **_kwargs):
        raise AgentContactIdentityMismatch("contact source is not the node")

    monkeypatch.setattr(services.operations, "claim", refuse)
    response = client.post("/agent/claim", headers=headers)
    assert response.status_code == 403

    def unknown(*_args, **_kwargs):
        raise BookkeepingUnknown("bookkeeping damaged")

    monkeypatch.setattr(services.operations, "claim", unknown)
    # Not a 422: the typed wait (503 with Retry-After) survives.
    waiting = client.post("/agent/claim", headers=headers)
    assert waiting.status_code == 503
    assert "retry-after" in waiting.headers


def test_incomplete_observation_warns_with_counts_and_recovers_in_fleet_health(
    agent_system, caplog
):
    # Wrong implementation: incomplete bodies are info-only, and authenticated
    # delivery failures disappear from fleet health while evidence is lost.

    client, services, _, clock = agent_system
    payload = canonical_message(
        RecipeRunObservationsWire(observed_at=clock.now, runs=[])
    )

    async def cut_off(scope, receive, send):
        received = False

        async def partial_receive():
            nonlocal received
            if received:
                return {"type": "http.disconnect"}
            received = True
            return {"type": "http.request", "body": payload[:7], "more_body": True}

        await client.app(scope, partial_receive, send)

    async def attempt():
        async with AsyncClient(
            transport=ASGITransport(app=cut_off), base_url="http://testserver"
        ) as peer:
            return await peer.post(
                "/agent/recipe-runs/observations",
                headers={
                    **agent_headers(NODE_A, "serial-a"),
                    "content-type": "application/json",
                },
                content=payload,
            )

    caplog.set_level(logging.INFO)
    assert asyncio.run(attempt()).status_code == 400
    event = next(
        r for r in caplog.records if "agent.request_rejected" in r.getMessage()
    )
    assert event.levelno == logging.WARNING
    evidence = json.loads(event.getMessage())
    assert evidence["received_body_bytes"] == 7
    assert evidence["expected_body_bytes"] == len(payload)
    assert evidence["level"] == "warning"
    assert evidence["node_id"] == NODE_A
    node = next(
        n for n in FleetProjection(services.sessions).read().nodes if n.id == NODE_A
    )
    warning = next(
        w
        for w in node.warnings
        if w.code == ProjectionCode.OBSERVATION_TRANSFER_UNAVAILABLE
    )
    assert "HTTP 400" in warning.detail
    assert (
        client.post(
            "/agent/recipe-runs/observations",
            headers={
                **agent_headers(NODE_A, "serial-a"),
                "content-type": "application/json",
            },
            content=payload,
        ).status_code
        == 204
    )
    node = next(
        n for n in FleetProjection(services.sessions).read().nodes if n.id == NODE_A
    )
    assert all(
        w.code != ProjectionCode.OBSERVATION_TRANSFER_UNAVAILABLE for w in node.warnings
    )


def test_old_observation_content_can_be_replayed_after_lost_ack(agent_system):
    # Wrong implementation: the retryable pending report becomes a permanent
    # 422 after five minutes, although its timestamp still fences newer state.
    client, _, _, clock = agent_system
    payload = canonical_message(
        RecipeRunObservationsWire(observed_at=clock.now, runs=[])
    )
    headers = {**agent_headers(NODE_A, "serial-a"), "content-type": "application/json"}
    assert (
        client.post(
            "/agent/recipe-runs/observations", headers=headers, content=payload
        ).status_code
        == 204
    )
    clock.now += timedelta(minutes=10)
    assert (
        client.post(
            "/agent/recipe-runs/observations", headers=headers, content=payload
        ).status_code
        == 204
    )


def test_builder_can_download_only_its_authorized_canonical_source_bundle(
    agent_system,
    monkeypatch,
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

    exact_get = services.source_bundles.get
    attempts = []

    def missing_source(digest):
        attempts.append(digest)
        raise SourceBundleUnknown(
            SourceBundleCode.STORAGE_UNAVAILABLE, "source storage unreadable"
        )

    monkeypatch.setattr(services.source_bundles, "get", missing_source)
    unknown = client.get(
        f"/agent/source-bundles/{bundle.sha256}",
        headers=agent_headers(NODE_A, "serial-a"),
    )
    # A bounded retry preserves exact bytes and admits a fresh recovering read.
    assert 0 < int(unknown.headers["retry-after"]) <= 300
    assert "etag" not in unknown.headers
    assert unknown.content != bundle.archive
    assert attempts == [bundle.sha256] * 4
    attempts.clear()

    def recovering_source(digest):
        attempts.append(digest)
        if len(attempts) == 1:
            raise SourceBundleUnknown(
                SourceBundleCode.STORAGE_UNAVAILABLE, "source storage unreadable"
            )
        return exact_get(digest)

    monkeypatch.setattr(services.source_bundles, "get", recovering_source)
    response = client.get(
        f"/agent/source-bundles/{bundle.sha256}",
        headers=agent_headers(NODE_A, "serial-a"),
    )
    assert response.status_code == 200
    assert response.content == bundle.archive
    assert attempts == [bundle.sha256] * 2
    assert response.headers["etag"] == f'"sha256:{bundle.sha256}"'
    assert (
        client.get(
            f"/agent/source-bundles/{bundle.sha256}",
            headers=agent_headers(NODE_B, "serial-b"),
        ).status_code
        == 404
    )
    assert client.get(f"/agent/source-bundles/{bundle.sha256}").status_code == 401


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

    descriptor, partial = _prepare_recipe_image_upload(tmp_path, "a" * 64)
    try:
        os.write(descriptor, b"partial archive")
        competing = None
        with suppress(Exception):  # cursor preservation and fresh admission decide
            competing = _prepare_recipe_image_upload(tmp_path, "a" * 64)
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
    headers = dict(start["headers"])
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
        assert observed is not None
        assert observed.replace(tzinfo=UTC) == clock.now


@pytest.mark.parametrize(
    "hostname",
    ["", "-spark", "spark_bbbb", "spark bbbb", "spark..lab", "a" * 256],
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
    [
        ("active_slot", "B"),
        ("agent_sha256", "c" * 64),
        ("platform_version", "1.2.3"),
        ("supervisor_generation", 7),
        ("supervisor_ready_generation", 7),
        ("activation_deadline", "2026-08-20T12:00:00Z"),
    ],
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


@pytest.mark.parametrize("architecture", ["linux-amd64", "linux-arm64"])
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
