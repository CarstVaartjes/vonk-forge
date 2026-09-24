"""The real load route and CLI preserve one reviewed, authorized application."""

from __future__ import annotations

import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from email.message import Message
from urllib.error import URLError
from uuid import uuid4

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import Actor
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import FleetProfileConflict, FleetProfileService
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    Base,
    CatalogDocumentRevision,
    FleetProfile,
    FleetProfileApplication,
    InstallationNode,
    Job,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
    User,
)
from vonk_forge_contracts import RecipeDefinition, content_sha256

from cluster_profiles import cli
from cluster_profiles.control_client import ControlClient

from .test_fleet_profile_api import _client, _headers
from .test_fleet_profile_review import _replace_run
from .test_fleet_profiles import _exact_cleanup_profile, _SwitchAdapter
from .test_fleet_profiles_canonical import NOW, _seed
from .test_recipe_operations import started_recipe


def _profile_api(engine):
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    _seed(sessions)
    api, codec = _client(sessions)
    headers = _headers(codec, "administrator")
    response = api.put(
        "/api/profile/1",
        headers=headers,
        json={"name": "Reviewed idle", "expected_revision": 0},
    )
    assert response.status_code == 200, response.text
    preview = api.post("/api/profile/1/preview", headers=headers)
    assert preview.status_code == 200, preview.text
    return sessions, api, codec, headers, preview.json()


def test_load_precondition_and_original_replay_use_current_authority(postgres_engine):
    sessions, api, codec, headers, preview = _profile_api(postgres_engine)
    path = "/api/profile/1/load"
    key = str(uuid4())
    body = {"request_key": key, "plan_digest": preview["plan_digest"]}
    for missing in (
        {},
        {"request_key": key},
        {"plan_digest": preview["plan_digest"]},
        {**body, "dry_run": True},
    ):
        assert api.post(path, headers=headers, json=missing).status_code == 422
    accepted = api.post(path, headers=headers, json=body)
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["request_key"] == key
    changed = api.put(
        "/api/profile/1",
        headers=headers,
        json={"name": "Later edit", "expected_revision": 1},
    )
    assert changed.status_code == 200
    assert api.post(path, headers=headers, json=body).json() == accepted.json()
    # A fresh request cannot use the review of the previous profile revision.
    stale_review = api.post(
        path, headers=headers, json={**body, "request_key": str(uuid4())}
    )
    assert stale_review.status_code == 409, stale_review.text
    assert stale_review.headers["x-vonk-error-code"] == "profile.stale_plan"
    assert (
        api.post(
            path, headers=headers, json={**body, "plan_digest": "f" * 64}
        ).status_code
        == 409
    )
    assert (
        api.post(path, headers=_headers(codec, "operator"), json=body).status_code
        == 403
    )
    other_headers = {
        "Authorization": "Bearer "
        + codec.issue(Actor("admin", "administrator"), ttl_seconds=100, now=0)
    }
    other_actor_conflict = api.post(path, headers=other_headers, json=body)
    assert other_actor_conflict.status_code == 409, other_actor_conflict.text
    assert other_actor_conflict.headers["x-vonk-error-code"] == "controller.conflict"
    lookup = f"/api/profile/1/requests/{key}"
    assert api.get(lookup, headers=_headers(codec, "operator")).status_code == 404
    assert api.get(lookup, headers=headers).json() == accepted.json()
    with sessions.begin() as session:
        user = session.scalar(select(User).where(User.subject == "administrator"))
        assert user is not None
        user.role = "viewer"
    # The still-valid administrator token cannot override a current downgrade.
    assert api.post(path, headers=headers, json=body).status_code == 403
    assert api.get(lookup, headers=headers).status_code == 200
    with sessions.begin() as session:
        user = session.scalar(select(User).where(User.subject == "administrator"))
        assert user is not None
        user.disabled_at = NOW
    assert api.post(path, headers=headers, json=body).status_code == 403
    assert api.get(lookup, headers=headers).status_code == 403
    with sessions() as session:
        assert list(session.scalars(select(FleetProfileApplication.id))) == [
            accepted.json()["id"]
        ]


def test_cli_recovers_committed_load_after_lost_response_and_profile_edit(
    postgres_engine, tmp_path, capsys
):
    sessions, api, _codec, headers, preview = _profile_api(postgres_engine)
    token = tmp_path / "token"
    token.touch(mode=0o600)
    token.write_text(headers["Authorization"].removeprefix("Bearer "))
    calls = []
    accepted = None

    class Response(io.BytesIO):
        def __init__(self, response):
            super().__init__(response.content)
            self.status = response.status_code
            self.headers = Message()
            for name, value in response.headers.items():
                self.headers[name] = value

        def __exit__(self, *args: object) -> None:
            self.close()

    def opener(request, *, timeout):
        nonlocal accepted
        method = request.get_method()
        calls.append(
            (
                method,
                request.full_url,
                json.loads(request.data) if request.data else None,
            )
        )
        response = api.request(
            method,
            request.full_url,
            headers=dict(request.header_items()),
            content=request.data,
        )
        if method == "POST":
            assert response.status_code == 202, response.text
            accepted = response.json()
            assert (
                api.put(
                    "/api/profile/1",
                    headers=headers,
                    json={"name": "Edited after acceptance", "expected_revision": 1},
                ).status_code
                == 200
            )
            raise URLError("connection lost after acceptance")
        return Response(response)

    key = str(uuid4())
    client = ControlClient("https://forge.example.test", token, opener=opener)
    code = cli.main(
        (
            "--profile",
            "1",
            "profile",
            "load",
            "--expected-plan",
            preview["plan_digest"],
            "--yes",
            "--request-key",
            key,
            "--json",
        ),
        control_client=client,
    )
    captured = capsys.readouterr()
    assert code == 0, captured.out
    assert json.loads(captured.out) == accepted
    assert [method for method, _, _ in calls] == ["POST", "GET"]
    assert calls[0][2] == {
        "request_key": key,
        "plan_digest": preview["plan_digest"],
    }
    assert calls[1][1].endswith(f"/api/profile/1/requests/{key}")
    with sessions() as session:
        assert len(list(session.scalars(select(FleetProfileApplication)))) == 1


@pytest.mark.parametrize("change", ["roster", "authority", "profile"])
def test_change_between_fresh_review_and_admission_refuses_load(
    postgres_engine,
    monkeypatch,
    change,
):
    sessions, api, _codec, headers, preview = _profile_api(postgres_engine)
    original = FleetProfileService._queue_application

    def after_preview(service, reviewed, **kwargs):
        with sessions.begin() as session:
            if change == "roster":
                session.add(
                    AgentNode(
                        node_id="spk_" + "3" * 32,
                        state="active",
                        protocol_version=2,
                        architecture="linux-arm64",
                        capabilities=[],
                        last_seen_at=NOW,
                    )
                )
            elif change == "authority":
                user = session.scalar(
                    select(User).where(User.subject == "administrator")
                )
                assert user is not None
                user.role = "viewer"
            else:
                profile = session.scalar(select(FleetProfile))
                assert profile is not None
                profile.name = "Changed before admission"
                profile.revision += 1
        return original(service, reviewed, **kwargs)

    monkeypatch.setattr(FleetProfileService, "_queue_application", after_preview)
    response = api.post(
        "/api/profile/1/load",
        headers=headers,
        json={
            "request_key": str(uuid4()),
            "plan_digest": preview["plan_digest"],
        },
    )
    assert response.status_code == (403 if change == "authority" else 409), (
        response.text
    )
    with sessions() as session:
        assert session.scalar(select(FleetProfileApplication)) is None


def test_concurrent_duplicate_reconciles_acceptance_before_reporting_stale_review(
    postgres_engine, monkeypatch
):
    sessions, api, _codec, headers, preview = _profile_api(postgres_engine)
    rendezvous = threading.Barrier(2)
    first_committed = threading.Event()
    second_request = ContextVar("second_request", default=False)
    original = FleetProfileService.preview
    body = {"request_key": str(uuid4()), "plan_digest": preview["plan_digest"]}

    def preview_after_lookup(service, *args, **kwargs):
        rendezvous.wait(timeout=5)
        if second_request.get():
            assert first_committed.wait(timeout=5)
        return original(service, *args, **kwargs)

    monkeypatch.setattr(FleetProfileService, "preview", preview_after_lookup)

    def submit(second):
        # Context follows the request into Starlette's worker thread.
        second_request.set(second)
        response = api.post("/api/profile/1/load", headers=headers, json=body)
        if not second:
            assert response.status_code == 202, response.text
            assert (
                api.put(
                    "/api/profile/1",
                    headers=headers,
                    json={
                        "name": "Edited after first acceptance",
                        "expected_revision": 1,
                    },
                ).status_code
                == 200
            )
            first_committed.set()
        return response

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit, second) for second in (False, True)]
        responses = [future.result(timeout=10) for future in futures]
    assert [response.status_code for response in responses] == [202, 202]
    assert responses[0].json() == responses[1].json()
    with sessions() as session:
        assert len(list(session.scalars(select(FleetProfileApplication)))) == 1


def test_admission_serializes_insertion_of_a_previously_unknown_spark(postgres_engine):
    sessions, api, _codec, headers, preview = _profile_api(postgres_engine)
    snapshot_locked = threading.Event()
    release = threading.Event()

    def after_roster_read(
        _connection, _cursor, statement, _parameters, _context, _many
    ):
        if (
            statement.startswith("SELECT agent_nodes.")
            and "FOR UPDATE NOWAIT" in statement
        ):
            snapshot_locked.set()
            assert release.wait(timeout=5)

    def add_node():
        with sessions.begin() as session:
            session.execute(text("SET LOCAL lock_timeout = '100ms'"))
            session.add(
                AgentNode(
                    node_id="spk_" + "3" * 32,
                    state="active",
                    protocol_version=2,
                    architecture="linux-arm64",
                    capabilities=[],
                    last_seen_at=NOW,
                )
            )

    event.listen(postgres_engine, "after_cursor_execute", after_roster_read)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                api.post,
                "/api/profile/1/load",
                headers=headers,
                json={
                    "request_key": str(uuid4()),
                    "plan_digest": preview["plan_digest"],
                },
            )
            try:
                assert snapshot_locked.wait(timeout=5)
                # A row lock on the two known Sparks cannot block this insert.
                with pytest.raises(OperationalError) as failure:
                    add_node()
                assert getattr(failure.value.orig, "sqlstate", None) == "55P03"
            finally:
                release.set()
            response = future.result(timeout=5)
        assert response.status_code == 202, response.text
        assert response.json()["progress"]["intended_profile"]["scope"] == {
            "node_ids": preview["scope"]["node_ids"]
        }
        add_node()
    finally:
        release.set()
        event.remove(postgres_engine, "after_cursor_execute", after_roster_read)


def test_recipe_head_changed_after_review_is_not_substituted_into_admitted_intent(
    postgres_engine, monkeypatch
):
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _seed(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    api, codec = _client(sessions, profiles=service)
    headers = _headers(codec, "administrator")
    created = api.put(
        "/api/profile/1",
        headers=headers,
        json={
            "name": "Exact recipe",
            "expected_revision": 0,
            "assignments": [
                {
                    "recipe_selector": "vonk-forge/synthetic-tiny-image",
                    "spark_ids": ["spk_" + "1" * 32],
                    "desired_state": "running",
                }
            ],
        },
    )
    assert created.status_code == 200, created.text
    preview = api.post("/api/profile/1/preview", headers=headers).json()
    assert preview["allowed"]
    original = FleetProfileService._queue_application

    def change_head(owner, reviewed, **kwargs):
        with sessions.begin() as session:
            previous = session.get(
                CatalogDocumentRevision,
                preview["resolved_assignments"][0]["recipe_revision_id"],
            )
            assert previous is not None
            document = RecipeDefinition.model_validate_json(
                json.dumps(previous.document)
            )
            document = document.model_copy(
                update={
                    "metadata": document.metadata.model_copy(
                        update={"title": "Updated recipe head"}
                    )
                }
            )
            session.add(
                CatalogDocumentRevision(
                    id=str(uuid4()),
                    document_id=previous.document_id,
                    kind="recipe",
                    publisher=previous.publisher,
                    slug=previous.slug,
                    revision_number=2,
                    schema_version=2,
                    state="active",
                    document=document.model_dump(mode="json"),
                    content_digest=content_sha256(document),
                    execution_key="d" * 64,
                    created_by="test",
                    created_at=NOW,
                )
            )
        return original(owner, reviewed, **kwargs)

    monkeypatch.setattr(FleetProfileService, "_queue_application", change_head)
    response = api.post(
        "/api/profile/1/load",
        headers=headers,
        json={
            "request_key": str(uuid4()),
            "plan_digest": preview["plan_digest"],
        },
    )
    assert response.status_code == 409, response.text
    with sessions() as session:
        assert session.scalar(select(FleetProfileApplication)) is None


def _running_profile_api(tmp_path, engine):
    sessions, lifecycle, _adapter, service, profile, installed, nodes = (
        _exact_cleanup_profile(tmp_path, engine=engine)
    )
    run = started_recipe(
        sessions,
        lifecycle,
        installed.owner_id,
        nodes,
        request_id=str(uuid4()),
        alias="reviewed-endpoint",
    )
    api, codec = _client(sessions, profiles=service)
    headers = _headers(codec, "administrator")
    preview = api.post(f"/api/profile/{profile.number}/preview", headers=headers)
    assert preview.status_code == 200 and preview.json()["allowed"], preview.text
    return sessions, api, headers, profile, installed, run, preview.json()


@pytest.mark.parametrize("change", ["run", "installation", "pending-job"])
def test_workload_change_between_review_and_acceptance_is_refused(
    tmp_path, postgres_engine, monkeypatch, change
):
    sessions, api, headers, profile, installed, run, preview = _running_profile_api(
        tmp_path, postgres_engine
    )
    original_queue = FleetProfileService._queue_application
    replacement = None

    def change_effect(service, reviewed, **kwargs):
        nonlocal replacement
        if change == "run":
            replacement = _replace_run(sessions, run.owner_id)
        else:
            replacement = str(uuid4())
            with sessions.begin() as session:
                if change == "installation":
                    original = session.get(RecipeInstallation, installed.owner_id)
                    assert original is not None
                    session.add(
                        RecipeInstallation(
                            **{
                                column.name: getattr(original, column.name)
                                for column in RecipeInstallation.__table__.columns
                                if column.name != "id"
                            },
                            id=replacement,
                        )
                    )
                    for member in session.scalars(
                        select(InstallationNode).where(
                            InstallationNode.installation_id == installed.owner_id
                        )
                    ):
                        session.add(
                            InstallationNode(
                                **{
                                    column.name: getattr(member, column.name)
                                    for column in InstallationNode.__table__.columns
                                    if column.name not in {"id", "installation_id"}
                                },
                                id=str(uuid4()),
                                installation_id=replacement,
                            )
                        )
                else:
                    original = session.get(Job, run.id)
                    assert original is not None
                    session.add(
                        Job(
                            **{
                                column.name: getattr(original, column.name)
                                for column in Job.__table__.columns
                                if column.name not in {"id", "request_id", "state"}
                            },
                            id=replacement,
                            request_id=str(uuid4()),
                            state="queued",
                        )
                    )
        return original_queue(service, reviewed, **kwargs)

    monkeypatch.setattr(FleetProfileService, "_queue_application", change_effect)
    response = api.post(
        f"/api/profile/{profile.number}/load",
        headers=headers,
        json={
            "request_key": str(uuid4()),
            "plan_digest": preview["plan_digest"],
        },
    )
    assert response.status_code == 409, response.text
    assert response.headers["x-vonk-error-code"] == "profile.stale_plan"
    assert "effect" in response.json()["detail"].lower(), response.text
    with sessions() as session:
        assert session.scalar(select(FleetProfileApplication)) is None
        model, expected = {
            "run": (RecipeRun, "running"),
            "installation": (RecipeInstallation, "installed"),
            "pending-job": (Job, "queued"),
        }[change]
        changed = session.get(model, replacement)
        assert changed is not None and changed.state == expected


def test_admission_serializes_run_replacement_until_acceptance_commits(
    tmp_path, postgres_engine
):
    sessions, api, headers, profile, _installed, run, preview = _running_profile_api(
        tmp_path, postgres_engine
    )
    snapshot_locked = threading.Event()
    release = threading.Event()

    def after_roster_read(
        _connection, _cursor, statement, _parameters, _context, _many
    ):
        if (
            statement.startswith("SELECT agent_nodes.")
            and "FOR UPDATE NOWAIT" in statement
        ):
            snapshot_locked.set()
            assert release.wait(timeout=5)

    def change_run():
        with sessions.begin() as session:
            session.execute(text("SET LOCAL lock_timeout = '100ms'"))
            row = session.get(RecipeRun, run.owner_id)
            assert row is not None
            row.alias = "changed-after-review"

    event.listen(postgres_engine, "after_cursor_execute", after_roster_read)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                api.post,
                f"/api/profile/{profile.number}/load",
                headers=headers,
                json={
                    "request_key": str(uuid4()),
                    "plan_digest": preview["plan_digest"],
                },
            )
            try:
                assert snapshot_locked.wait(timeout=5)
                with pytest.raises(OperationalError) as failure:
                    change_run()
                assert getattr(failure.value.orig, "sqlstate", None) == "55P03"
            finally:
                release.set()
            response = future.result(timeout=5)
        assert response.status_code == 202, response.text
        change_run()
    finally:
        release.set()
        event.remove(postgres_engine, "after_cursor_execute", after_roster_read)


def test_first_dispatch_cannot_adopt_a_replacement_run_after_acceptance(
    tmp_path, postgres_engine
):
    sessions, _lifecycle, _adapter, service, profile, installed, nodes = (
        _exact_cleanup_profile(tmp_path, engine=postgres_engine)
    )
    # Start through the same production lifecycle used by the reviewed stop.
    run = started_recipe(
        sessions,
        _lifecycle,
        installed.owner_id,
        nodes,
        request_id=str(uuid4()),
        alias="reviewed-endpoint",
    )
    preview = service.preview(profile.id)
    accepted = service.apply(
        profile.id,
        plan_digest=preview.plan_digest,
        request_key=str(uuid4()),
        actor="admin",
    )
    replacement_id = _replace_run(sessions, run.owner_id)
    with sessions() as session:
        before_jobs = set(session.scalars(select(Job.id)))
    assert service.tick()
    observed = service.application(accepted.id)
    assert observed.state == "failed", observed
    assert observed.status_reason is not None
    assert "unreviewed" in observed.status_reason
    with sessions() as session:
        replacement = session.get(RecipeRun, replacement_id)
        assert replacement is not None and replacement.state == "running"
        assert set(session.scalars(select(Job.id))) == before_jobs


def test_replanned_assignment_child_cannot_add_a_stop_after_queue_creation(
    tmp_path, postgres_engine
):
    sessions, lifecycle, adapter, service, _idle, installed, nodes = (
        _exact_cleanup_profile(tmp_path, engine=postgres_engine)
    )
    run = started_recipe(
        sessions,
        lifecycle,
        installed.owner_id,
        nodes,
        request_id=str(uuid4()),
        alias="reviewed-endpoint",
    )
    with sessions.begin() as session:
        row = session.get(RecipeRun, run.owner_id)
        assert row is not None
        row.route_state = "withdrawn"
        installation = session.get(RecipeInstallation, installed.owner_id)
        assert installation is not None
        revision = session.get(CatalogDocumentRevision, installation.recipe_revision_id)
        assert revision is not None
        selector = f"{revision.publisher}/{revision.slug}"
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Restore serving",
                "assignments": [
                    {
                        "recipe_selector": selector,
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": "reviewed-endpoint",
                    }
                ],
            }
        ),
        actor="admin",
    )
    preview = service.preview(profile.id)
    assert preview.allowed and preview.summary.stops == preview.summary.starts == 1
    accepted = service.apply(
        profile.id,
        plan_digest=preview.plan_digest,
        request_key=str(uuid4()),
        actor="admin",
    )
    replacement = _replace_run(sessions, run.owner_id)
    with sessions.begin() as session:
        # Keep a complete stop contract for the replacement: this regression
        # targets consent, not rejection of an incomplete reservation record.
        for reservation in session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "run",
                ResourceReservation.owner_id == run.owner_id,
            )
        ):
            reservation.owner_id = replacement
    with sessions() as session:
        before_jobs = set(session.scalars(select(Job.id)))
    # The worker already chose this assignment. Run/Switch's fresh plan now
    # discovers a different stop; the profile must not approve that new effect.
    assignment = preview.resolved_assignments[0]
    with pytest.raises(FleetProfileConflict, match="unreviewed"):
        adapter._start_child(
            accepted.id,
            {"kind": "run", "id": assignment.id},
            (assignment,),
            tuple(nodes),
            "admin",
            accepted.request_key,
            1,
            accepted.progress.workload_intent_ordinal,
        )
    with sessions() as session:
        changed = session.get(RecipeRun, replacement)
        assert changed is not None and changed.state == "running"
        assert set(session.scalars(select(Job.id))) == before_jobs


@pytest.mark.parametrize("locked_model", [Job, AgentOperation])
def test_superseded_child_contention_refuses_without_holding_admission(
    tmp_path, postgres_engine, locked_model
):
    sessions, lifecycle, _adapter, service, profile, installed, _nodes = (
        _exact_cleanup_profile(tmp_path, engine=postgres_engine)
    )
    start_plan = lifecycle.preview_run(installed.owner_id, "pending-endpoint")
    pending = lifecycle.start(
        start_plan,
        plan_digest=start_plan.plan_digest,
        actor="admin",
        request_id=str(uuid4()),
    )
    api, codec = _client(sessions, profiles=service)
    headers = _headers(codec, "administrator")
    preview = api.post(f"/api/profile/{profile.number}/preview", headers=headers).json()
    assert preview["allowed"] and preview["effects"]["superseded"]
    with sessions() as session:
        ordinals = tuple(
            session.scalars(
                select(AgentNode.workload_intent_ordinal).order_by(AgentNode.node_id)
            )
        )
    attempted = threading.Event()
    prefix = f"SELECT {locked_model.__tablename__}."

    def before_lock(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith(prefix) and "FOR UPDATE" in statement:
            attempted.set()

    locker = sessions()
    locked_id = (
        pending.id
        if locked_model is Job
        else locker.scalar(
            select(AgentOperation.id).where(AgentOperation.parent_job_id == pending.id)
        )
    )
    assert locked_id is not None
    assert locker.get(locked_model, locked_id, with_for_update=True) is not None
    event.listen(postgres_engine, "before_cursor_execute", before_lock)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                api.post,
                f"/api/profile/{profile.number}/load",
                headers=headers,
                json={
                    "request_key": str(uuid4()),
                    "plan_digest": preview["plan_digest"],
                },
            )
            try:
                assert attempted.wait(timeout=5)
                # The lock is still held. Admission must refuse now, not wait
                # for the worker whose effects it is trying to supersede.
                response = future.result(timeout=1)
                assert response.status_code == 409, response.text
                with sessions() as session:
                    assert session.scalar(select(FleetProfileApplication)) is None
                    assert (
                        tuple(
                            session.scalars(
                                select(AgentNode.workload_intent_ordinal).order_by(
                                    AgentNode.node_id
                                )
                            )
                        )
                        == ordinals
                    )
            finally:
                locker.rollback()
    finally:
        locker.close()
        event.remove(postgres_engine, "before_cursor_execute", before_lock)
