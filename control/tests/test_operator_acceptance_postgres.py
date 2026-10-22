"""HTTP acceptance survives lost responses, restart and PostgreSQL faults."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import LifecycleState
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_control.api import create_app
from vonk_control.auth import TokenCodec
from vonk_control.jobs import JobService
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import Base, FleetProfile, FleetProfileApplication, Job, User
from vonk_control.recipe_image_availability import RecipeImageAvailabilityService
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

from .runtime_identity_support import PACKAGED_RUNTIME_IDENTITY, inventory_items_fixture
from .test_fleet_profile_api import _client, _headers
from .test_model_cache_cancel_recovery import _artifact
from .test_profile_cancel_receipt_postgres import _application
from .test_recipe_image_availability import _add_revision, _recipe, _runtime


class DropFirstResponse:
    """Discard committed ASGI output at the transport seam, once."""

    def __init__(self, app, path):
        self.app, self.path, self.dropped = app, path, False

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] == "http"
            and scope["method"] in {"PUT", "POST"}
            and scope["path"] == self.path
            and not self.dropped
        ):
            messages = []

            async def hold(message):
                messages.append(message)

            await self.app(scope, receive, hold)
            assert messages[0]["status"] < 300
            self.dropped = True
            raise httpx.ReadError("injected lost HTTP response")
        await self.app(scope, receive, send)


def _receipt(api, response, headers):
    assert 200 <= response.status_code < 300, response.text
    reference = json.loads(response.headers["x-vonk-receipt"])
    fetched = api.get(reference["href"], headers=headers)
    assert fetched.status_code == 200, fetched.text
    return reference


def _api(sessions, **services):
    codec = TokenCodec(b"operator-receipts-postgres-key-32")
    app = create_app(
        jobs=JobService(sessions, clock=lambda: datetime.now(UTC)),
        tokens=codec,
        now=lambda: 1,
        **services,
    )
    return TestClient(app), _headers(codec, "operator")


def test_profile_save_lost_response_replays_committed_revision(postgres_engine):
    """Catches incrementing a revision or refusing the exact save after response loss."""
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    first, codec = _client(sessions)
    headers = _headers(codec, "administrator")
    body = {"name": "Durable save", "assignments": []}
    transport = TestClient(DropFirstResponse(first.app, "/api/profile/1"))
    with pytest.raises(httpx.ReadError):
        transport.put("/api/profile/1", json=body, headers=headers)
    with sessions() as session:
        saved = session.scalar(select(FleetProfile))
        assert saved is not None and saved.revision == 1
        identity = saved.id
    restarted, _ = _client(sessions)
    replay = restarted.put("/api/profile/1", json=body, headers=headers)
    reference = _receipt(restarted, replay, headers)
    assert (reference["id"], reference["revision"]) == (identity, 1)
    assert (
        restarted.put(
            "/api/profile/1", json={**body, "name": "Changed request"}, headers=headers
        ).status_code
        == 409
    )


def test_profile_load_lost_response_reconnects_after_restart(postgres_engine):
    """Catches emitting an acceptance before commit or accepting a second load on replay."""
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    first, codec = _client(sessions, with_idle_spark=True)
    headers = _headers(codec, "administrator")
    assert (
        first.put(
            "/api/profile/1", json={"name": "Idle", "assignments": []}, headers=headers
        ).status_code
        == 200
    )
    key = str(uuid4())
    path = "/api/profile/1/load"
    with pytest.raises(httpx.ReadError):
        TestClient(DropFirstResponse(first.app, path)).post(
            path, json={"request_key": key}, headers=headers
        )
    with sessions() as session:
        assert (
            session.scalar(select(func.count()).select_from(FleetProfileApplication))
            == 1
        )
        accepted = session.scalar(select(FleetProfileApplication))
        assert accepted is not None and accepted.request_key == key
        identity = accepted.id
    restarted, _ = _client(sessions)
    response = restarted.post(path, json={"request_key": key}, headers=headers)
    assert _receipt(restarted, response, headers)["id"] == identity
    with sessions() as session:
        assert (
            session.scalar(select(func.count()).select_from(FleetProfileApplication))
            == 1
        )


@pytest.mark.parametrize("action", ["save", "load", "cancel"])
def test_commit_failure_never_emits_acceptance_and_same_request_recovers(
    tmp_path, postgres_engine, action
):
    """Catches response serialization preceding commit and poisoned retries after rollback."""
    if action == "cancel":
        sessions, _runs, profiles, profile, application, *_ = _application(
            tmp_path, engine=postgres_engine
        )
        api, codec = _client(sessions, profiles=profiles)
        path = f"/api/profile/applications/{application.id}/cancel"
        body = {"profile_number": profile.number, "request_key": str(uuid4())}
        method = "POST"
    else:
        Base.metadata.create_all(postgres_engine)
        sessions = sessionmaker(postgres_engine, expire_on_commit=False)
        api, codec = _client(sessions, with_idle_spark=True)
        path, body, method = (
            "/api/profile/1",
            {"name": "Commit fault", "assignments": []},
            "PUT",
        )
        if action == "load":
            assert (
                api.put(
                    path, json=body, headers=_headers(codec, "administrator")
                ).status_code
                == 200
            )
            path, body, method = (
                "/api/profile/1/load",
                {"request_key": str(uuid4())},
                "POST",
            )
    headers = _headers(codec, "administrator")
    failed = False

    def fail_commit(session):
        nonlocal failed
        if not failed and not session.in_nested_transaction():
            failed = True
            raise OperationalError(
                "COMMIT", None, RuntimeError("injected commit failure")
            )

    event.listen(sessions, "before_commit", fail_commit)
    try:
        refused = api.request(method, path, json=body, headers=headers)
        assert refused.status_code == 503, refused.text
        assert "x-vonk-receipt" not in refused.headers
    finally:
        event.remove(sessions, "before_commit", fail_commit)
    assert failed
    recovered = api.request(method, path, json=body, headers=headers)
    receipt = _receipt(api, recovered, headers)
    replayed = api.request(method, path, json=body, headers=headers)
    assert _receipt(api, replayed, headers) == receipt


def test_model_cancel_dropped_response_and_schema_valid_unchanged_success(
    tmp_path, postgres_engine, monkeypatch
):
    """Catches acknowledging unchanged 202 with no cancel key in the existing owner."""
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(User(subject="operator", role="operator"))
    cache_root = tmp_path / "cache"
    service = ModelCacheService(
        sessions, cache_root, reserve_bytes=0, fixture_sources=True
    )
    try:
        artifact = _artifact(tmp_path / "source", b"receipt boundary")
        preview = service.download_preview(
            model_content_sha256="a" * 64, artifacts=[artifact]
        )
        operation = service.start_download(
            actor="operator",
            request_key=str(uuid4()),
            selector="fixture",
            plan_digest=str(preview["plan_digest"]),
            model_content_sha256="a" * 64,
            artifacts=[artifact],
        )
        api, headers = _api(sessions, model_cache=service)
        path = f"/api/model/operations/{operation.id}/cancel"
        body = {"request_key": str(uuid4()), "reason": "Stop exact transfer"}
        original = service.cancel_operation
        monkeypatch.setattr(
            service,
            "cancel_operation",
            lambda *_args, **_kwargs: service.get_operation(operation.id),
        )
        unchanged = api.post(path, json=body, headers=headers)
        assert unchanged.status_code == 409
        assert unchanged.headers["x-vonk-error-code"]
        monkeypatch.setattr(service, "cancel_operation", original)
        with pytest.raises(httpx.ReadError):
            TestClient(DropFirstResponse(api.app, path)).post(
                path, json=body, headers=headers
            )
        service.close()
        service = ModelCacheService(
            sessions, cache_root, reserve_bytes=0, fixture_sources=True
        )
        restarted, headers = _api(sessions, model_cache=service)
        receipt = _receipt(
            restarted, restarted.post(path, json=body, headers=headers), headers
        )
        assert receipt["request_id"] == body["request_key"]
        assert (
            restarted.post(
                path, json={**body, "request_key": str(uuid4())}, headers=headers
            ).status_code
            == 409
        )
    finally:
        service.close()


def test_recipe_cancellation_receipt_survives_restart(tmp_path, postgres_engine):
    """Catches cancellation lookup depending on process memory or original request id."""
    from vonk_control.models import CatalogDocument

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    recipe = _recipe("recipe-source-build.json")
    revision = uuid4().hex[:24]
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(
            CatalogDocument(
                id="document-" + revision,
                kind="recipe",
                publisher=recipe.identity.publisher,
                slug=recipe.identity.slug,
                title=recipe.metadata.title,
                created_by="operator",
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        _add_revision(session, revision, recipe)
        session.add(User(subject="operator", role="operator"))

    def authority(_revision, *, force=False):
        return recipe, _runtime()

    def service():
        return RecipeImageAvailabilityService(
            sessions,
            storage=FilesystemRuntimeImageStorage(tmp_path / "images"),
            authority=authority,
            clock=lambda: now,
        )

    owner = service()
    operation = owner.start(revision, actor="operator", request_id=str(uuid4()))
    api, headers = _api(sessions, recipe_image_availability=owner)
    path = f"/api/recipe/operations/{operation.id}/cancel"
    body = {"request_key": str(uuid4()), "reason": "Stop preparation"}
    with pytest.raises(httpx.ReadError):
        TestClient(DropFirstResponse(api.app, path)).post(
            path, json=body, headers=headers
        )
    restarted, headers = _api(sessions, recipe_image_availability=service())
    receipt = _receipt(
        restarted, restarted.post(path, json=body, headers=headers), headers
    )
    assert receipt["request_id"] == body["request_key"]
    with sessions() as session:
        assert session.get_one(Job, operation.id).state == LifecycleState.CANCELLED


def test_key_receipts_replay_without_repeating_external_effects(
    tmp_path, postgres_engine
):
    """Catches losing key acceptance or treating transport uncertainty as success."""
    import httpx2
    from vonk_control.gateway_keys import GatewayKeyService

    from .test_gateway_keys import MASTER, FakeLiteLlm

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(User(subject="administrator", role="administrator"))
    peer = FakeLiteLlm()

    def service():
        return GatewayKeyService(
            master_key=lambda: MASTER,
            transport=httpx2.MockTransport(peer.handle),
            intent_root=tmp_path / "keys",
        )

    owner = service()
    codec = TokenCodec(b"key-receipt-authority-32-bytes!!!")
    headers = _headers(codec, "administrator")

    def api(owner):
        return TestClient(
            create_app(
                jobs=JobService(sessions, clock=lambda: datetime.now(UTC)),
                tokens=codec,
                now=lambda: 1,
                gateway_keys=owner,
            )
        )

    first = api(owner)
    key = f"{uuid4()}/α"
    body = {"name": "receipt", "request_id": key}
    with pytest.raises(httpx.ReadError):
        TestClient(DropFirstResponse(first.app, "/api/key")).post(
            "/api/key", json=body, headers=headers
        )
    restarted = api(service())
    replay = restarted.post("/api/key", json=body, headers=headers)
    receipt = _receipt(restarted, replay, headers)
    assert receipt["request_id"] == key
    assert peer.counter == 1
    assert (
        replay.json()["key"] not in restarted.get(receipt["href"], headers=headers).text
    )
    invalid = restarted.post(
        "/api/key/receipt/revoke",
        params={"request_id": "x" * 129},
        headers=headers,
    )
    assert invalid.status_code == 422, invalid.text
    assert "receipt" in peer.keys
    revoke_key = f"{uuid4()}/α"
    revoke = f"/api/key/receipt/revoke?request_id={revoke_key}"
    accepted = restarted.post(revoke, headers=headers)
    assert _receipt(restarted, accepted, headers)["request_id"] == revoke_key
    assert (
        _receipt(restarted, restarted.post(revoke, headers=headers), headers)[
            "request_id"
        ]
        == revoke_key
    )
    assert not peer.keys
    fresh_revoke = str(uuid4())
    refused = restarted.post(
        f"/api/key/receipt/revoke?request_id={fresh_revoke}", headers=headers
    )
    assert refused.status_code == 409, refused.text
    assert (
        restarted.get(
            "/api/key/receipt/requests",
            params={"request_id": fresh_revoke},
            headers=headers,
        ).status_code
        == 404
    )


def test_unknown_key_mutation_is_retryable_without_success(postgres_engine):
    """Catches a schema-valid 202/200 UnknownError without any owning receipt."""
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    codec = TokenCodec(b"missing-key-receipt-authority-32!")
    api = TestClient(
        create_app(
            jobs=JobService(sessions, clock=lambda: datetime.now(UTC)),
            tokens=codec,
            now=lambda: 1,
        )
    )
    headers = _headers(codec, "administrator")
    for path, body in [
        ("/api/key", {"name": "missing"}),
        ("/api/key/missing/revoke", None),
        ("/api/key/missing/roll", None),
    ]:
        response = api.post(path, json=body, headers=headers)
        assert response.status_code == 503, response.text
        assert response.headers["Retry-After"]
        assert "x-vonk-receipt" not in response.headers


def test_artifact_create_upload_and_cancel_resolve_existing_job(
    tmp_path, postgres_engine
):
    """Catches accepting a terminal no-op cancel or losing an upload's publication."""
    import hashlib

    from .test_artifact_jobs import artifact_create_request, running_artifact_service

    sessions, _operations, _queue, owner, run_id, _node = running_artifact_service(
        tmp_path, engine=postgres_engine
    )
    api, headers = _api(sessions, artifact_jobs=owner)
    key = str(uuid4())
    request = artifact_create_request(run_id, key)
    body = {
        name: value
        for name, value in request.items()
        if name not in {"run_id", "actor", "request_id"}
    }
    create_path = f"/api/recipe/runs/{run_id}/artifact-jobs"
    create_headers = {**headers, "X-Request-ID": key}
    with pytest.raises(httpx.ReadError):
        TestClient(DropFirstResponse(api.app, create_path)).post(
            create_path, json=body, headers=create_headers
        )
    created = api.post(create_path, json=body, headers=create_headers)
    identity = _receipt(api, created, headers)["id"]
    content = b"png"
    uploaded = api.put(
        f"/api/artifact-jobs/{identity}/inputs/input.png",
        content=content,
        headers={
            **headers,
            "Content-Type": "image/png",
            "Content-Length": str(len(content)),
            "X-Content-SHA256": hashlib.sha256(content).hexdigest(),
        },
    )
    assert _receipt(api, uploaded, headers)["id"] == identity
    finalize = api.post(f"/api/artifact-jobs/{identity}/finalize", headers=headers)
    assert _receipt(api, finalize, headers)["id"] == identity
    cancel_path = f"/api/artifact-jobs/{identity}/cancel"
    cancel_headers = {**headers, "X-Request-ID": str(uuid4())}
    accepted = api.post(
        cancel_path, json={"reason": "Stop draft"}, headers=cancel_headers
    )
    assert (
        _receipt(api, accepted, headers)["request_id"] == cancel_headers["X-Request-ID"]
    )
    assert (
        api.post(
            cancel_path, json={"reason": "Stop draft"}, headers=cancel_headers
        ).status_code
        == 200
    )
    assert (
        api.post(
            cancel_path,
            json={"reason": "Stop draft"},
            headers={**headers, "X-Request-ID": str(uuid4())},
        ).status_code
        == 409
    )


def test_fleet_rename_is_a_committed_resource_receipt(tmp_path, postgres_engine):
    """Catches returning a renamed projection before the node profile committed."""
    from vonk_control.fleet_projection import FleetProjection
    from vonk_control.models import AgentNode, AgentNodeProfile

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    node_id = "spk_" + "1" * 32
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=node_id,
                state="active",
                inventory_snapshot=inventory_items_fixture(
                    node_id,
                    observed_at=None,
                    identity=AgentRuntimeIdentity.model_validate(
                        {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                    ),
                ),
            )
        )
        session.flush()
        session.add(AgentNodeProfile(node_id=node_id, display_name="Original"))
    projection = FleetProjection(sessions, clock=lambda: now)
    api, headers = _api(sessions, fleet_projection=projection)
    path = f"/api/fleet/{node_id}/rename"
    body = {"display_name": "Receipt Spark"}
    with pytest.raises(httpx.ReadError):
        TestClient(DropFirstResponse(api.app, path)).post(
            path, json=body, headers=headers
        )
    with sessions() as session:
        assert (
            session.get_one(AgentNodeProfile, node_id).display_name
            == body["display_name"]
        )
    assert (
        _receipt(api, api.post(path, json=body, headers=headers), headers)["id"]
        == node_id
    )


def test_job_resume_replays_committed_owner_after_response_loss(
    tmp_path, postgres_engine
):
    """Catches a second resume refusing the retry authorization already committed."""
    from .agent_fences import park_for_operator
    from .test_operation_api import MutableClock, _claim_parked, _parked_stop_services

    clock = MutableClock(datetime.now(UTC))
    sessions, queue, operations, _order, job_id = _parked_stop_services(
        tmp_path, clock=clock, engine=postgres_engine
    )
    claim = _claim_parked(queue)
    assert claim is not None
    park_for_operator(sessions, queue, claim, "Inspect effect")
    api, headers = _api(sessions, operations=operations)
    path = f"/api/jobs/{job_id}/resume"
    headers = {**headers, "X-Request-ID": str(uuid4())}
    with pytest.raises(httpx.ReadError):
        TestClient(DropFirstResponse(api.app, path)).post(path, headers=headers)
    with sessions() as session:
        assert session.get_one(Job, job_id).state == LifecycleState.QUEUED
    receipt = _receipt(api, api.post(path, headers=headers), headers)
    assert receipt["id"] == job_id
    next_claim = _claim_parked(queue)
    assert next_claim is not None
    restarted, _ = _api(sessions, operations=operations)
    assert (
        _receipt(restarted, restarted.post(path, headers=headers), headers) == receipt
    )
    park_for_operator(sessions, queue, next_claim, "Inspect the next attempt")
    newer_headers = {**headers, "X-Request-ID": str(uuid4())}
    _receipt(restarted, restarted.post(path, headers=newer_headers), newer_headers)
    newer_claim = _claim_parked(queue)
    assert newer_claim is not None
    park_for_operator(sessions, queue, newer_claim, "Inspect the latest attempt")
    assert (
        _receipt(restarted, restarted.post(path, headers=headers), headers) == receipt
    )
    assert _claim_parked(queue) is None
    latest_headers = {**headers, "X-Request-ID": str(uuid4())}
    _receipt(restarted, restarted.post(path, headers=latest_headers), latest_headers)
    assert _claim_parked(queue) is not None

    fresh = JobService(sessions, clock=clock).enqueue(
        "reconcile", "operator", "f" * 40, [_order.node_id], {}, request_id=str(uuid4())
    )
    assert fresh.id != job_id


def test_job_commit_failure_rolls_back_acceptance_and_authorization(
    tmp_path, postgres_engine
):
    """Catches a receipt committed separately from the retry authorization it proves."""
    from .agent_fences import park_for_operator
    from .test_operation_api import MutableClock, _claim_parked, _parked_stop_services

    clock = MutableClock(datetime.now(UTC))
    sessions, queue, operations, _order, job_id = _parked_stop_services(
        tmp_path, clock=clock, engine=postgres_engine
    )
    claim = _claim_parked(queue)
    assert claim is not None
    park_for_operator(sessions, queue, claim, "Inspect effect")
    api, headers = _api(sessions, operations=operations)
    headers = {**headers, "X-Request-ID": str(uuid4())}
    path = f"/api/jobs/{job_id}/resume"

    def fail_commit(session):
        if not session.in_nested_transaction():
            raise OperationalError(
                "COMMIT", None, RuntimeError("injected commit failure")
            )

    with sessions() as session:
        initial_state = session.get_one(Job, job_id).state
    invalid = api.post(path, headers={**headers, "X-Request-ID": "not-a-request-key"})
    assert invalid.status_code == 422
    event.listen(sessions, "before_commit", fail_commit)
    try:
        failed = api.post(path, headers=headers)
        assert failed.status_code == 503, failed.text
        assert "x-vonk-receipt" not in failed.headers
    finally:
        event.remove(sessions, "before_commit", fail_commit)
    with sessions() as session:
        job = session.get_one(Job, job_id)
        assert job.operator_decisions is None
        assert job.state == initial_state
    assert (
        _receipt(api, api.post(path, headers=headers), headers)["request_id"]
        == headers["X-Request-ID"]
    )

    assert _claim_parked(queue) is not None
    fresh = JobService(sessions, clock=clock).enqueue(
        "reconcile", "operator", "f" * 40, [_order.node_id], {}, request_id=str(uuid4())
    )
    assert fresh.id != job_id


def test_profile_preview_has_observation_semantics_without_writes(postgres_engine):
    """Catches a preview using observation policy while committing a hidden mutation."""
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    api, codec = _client(sessions, with_idle_spark=True)
    headers = _headers(codec, "administrator")
    assert (
        api.put(
            "/api/profile/1",
            json={"name": "Observe", "assignments": []},
            headers=headers,
        ).status_code
        == 200
    )
    writes = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    event.listen(postgres_engine, "before_cursor_execute", capture)
    try:
        preview = api.post("/api/profile/1/preview", headers=headers)
        assert preview.status_code == 200, preview.text
        assert "x-vonk-receipt" not in preview.headers
        assert not writes
    finally:
        event.remove(postgres_engine, "before_cursor_execute", capture)


def test_save_receipt_cannot_adopt_an_intervening_revision(
    postgres_engine, monkeypatch
):
    """Catches accepting the same content after its reviewed revision was replaced."""
    from vonk_control.fleet_profiles import FleetProfileService

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    profiles = FleetProfileService(sessions, clock=lambda: datetime.now(UTC))
    api, codec = _client(sessions, profiles=profiles)
    headers = _headers(codec, "administrator")
    update = profiles.update_number

    def intervening_save(number, body, *, actor):
        accepted = update(number, body, actor=actor)
        second = update(
            number,
            body.model_copy(
                update={"name": "Intervening", "expected_revision": accepted.revision}
            ),
            actor=actor,
        )
        update(
            number,
            body.model_copy(update={"expected_revision": second.revision}),
            actor=actor,
        )
        return accepted

    monkeypatch.setattr(profiles, "update_number", intervening_save)
    response = api.put(
        "/api/profile/1",
        json={"name": "Same content", "assignments": []},
        headers=headers,
    )
    assert response.status_code == 409, response.text
    assert "x-vonk-receipt" not in response.headers
    monkeypatch.setattr(profiles, "update_number", update)
    current = api.put(
        "/api/profile/1",
        json={"name": "Same content", "assignments": []},
        headers=headers,
    )
    assert _receipt(api, current, headers)["revision"] == 3
