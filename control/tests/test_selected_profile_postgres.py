from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import (
    FleetProfileAdmissionEffectBusy,
    FleetProfileService,
)
from vonk_control.models import (
    AgentNode,
    Base,
    CatalogDocumentRevision,
    FleetProfileApplication,
    FleetProfileSelection,
    Job,
    User,
)
from vonk_control.recipe_operation_worker import RecipeOperationWorker
from vonk_forge_contracts import RecipeDefinition, document_sha256

from .test_fleet_profiles import (
    _assessment,
    _exact_preparation,
    _input,
    _node_id,
    _seed,
    _SwitchAdapter,
    _uuid,
)
from .test_fleet_profiles_canonical import (
    NODE_1,
    RECIPE_REVISION_ID,
)
from .test_fleet_profiles_canonical import _seed as _seed_canonical

NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)


def _service(
    sessions: sessionmaker[Session], adapter: _SwitchAdapter | None = None
) -> FleetProfileService:
    return FleetProfileService(
        sessions,
        clock=lambda: NOW,
        switch_adapter=adapter or _SwitchAdapter(),
        assessment_provider=lambda _session, _assignment, node_ids, **_kwargs: (
            _assessment(_exact_preparation(node_ids))
        ),
    )


def _tick_selected_when_due(service: FleetProfileService, application_id: str) -> None:
    """Observe the selected accepted child only at its durable retry time."""
    before = service.application(application_id)
    due = before.progress.retry_due_at
    if due is not None and due > service._clock():
        service.tick()
        held = service.application(application_id)
        assert held.current_operation_id == before.current_operation_id
        assert held.progress.step_results == before.progress.step_results
        service._clock = lambda due=due: due
    service.tick()


class _BusyOnceAdapter(_SwitchAdapter):
    def __init__(self) -> None:
        super().__init__()
        self._busy = True

    def validate_resources_in_session(self, session, assignments, reviewed) -> None:
        if self._busy:
            self._busy = False
            raise FleetProfileAdmissionEffectBusy("temporary test admission block")


class _DueDatabaseWork:
    """A sibling coordinator that advances one already-due PostgreSQL job."""

    def __init__(self, sessions: sessionmaker[Session], job_id: str) -> None:
        self._sessions = sessions
        self._job_id = job_id

    def tick(self) -> bool:
        with self._sessions.begin() as session:
            job = session.get(Job, self._job_id)
            if job is None or job.state != "queued":
                return False
            job.state = "succeeded"
            return True


class _NoopRoutes:
    def publish_run(self, run_id: str) -> object:
        del run_id
        raise AssertionError("the roster-authority test has no running recipe")

    def maintain(self) -> bool:
        return False


def _selected(engine: Engine) -> dict[str, object]:
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT generation, profile_id, profile_revision, application_id, "
                    "roster_digest FROM fleet_profile_selection WHERE singleton_id = 1"
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


@pytest.mark.parametrize("authority_change", ["removed", "disabled", "demoted"])
def test_selected_running_profile_reconciles_drift_without_roster_change(
    postgres_engine: Engine,
    authority_change: str,
) -> None:
    """A lost/dead run with the same roster reuses the accepted profile intent."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _recipe_id, recipe_revision_id = _seed(sessions)
    service = _service(sessions)
    profile = service.create(_input(recipe_revision_id), actor="admin")
    preview = service.preview(profile.id)
    assert preview.allowed is True, preview.reasons
    accepted = service.apply(
        profile.id,
        request_key=_uuid(881),
        actor="admin",
    )
    for _ in range(8):
        if accepted.state == "succeeded":
            break
        _tick_selected_when_due(service, accepted.id)
        accepted = service.application(accepted.id)
    assert accepted.state == "succeeded"
    initial_selection = _selected(postgres_engine)
    initial_generation = initial_selection["generation"]
    assert isinstance(initial_generation, int)

    with sessions.begin() as session:
        author = session.scalar(select(User).where(User.subject == "admin"))
        assert author is not None
        if authority_change == "removed":
            session.delete(author)
        elif authority_change == "disabled":
            author.disabled_at = NOW
        else:
            author.role = "viewer"

    # The accepted assignment is still desired, but its run/runtime has
    # disappeared. The enrolled roster remains exactly the same.
    assert service.tick() is True
    reconciled_selection = _selected(postgres_engine)
    assert reconciled_selection["generation"] == initial_generation + 1
    assert reconciled_selection["profile_id"] == profile.id
    assert reconciled_selection["roster_digest"] == initial_selection["roster_digest"]
    assert reconciled_selection["application_id"] != accepted.id
    reconciled = service.application(str(reconciled_selection["application_id"]))
    for _ in range(8):
        if service.application(reconciled.id).state == "succeeded":
            break
        _tick_selected_when_due(service, reconciled.id)
    repaired = service.application(reconciled.id)
    assert repaired.state == "succeeded"
    assert repaired.progress.intended_profile is not None
    assert accepted.progress.intended_profile is not None
    assert repaired.progress.intended_profile.assignments == (
        accepted.progress.intended_profile.assignments
    )


def test_selected_empty_profile_keeps_new_spark_idle_after_restart_and_saved_edit(
    postgres_engine: Engine,
) -> None:
    """A join reconciles the accepted profile snapshot, not current draft edits."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _recipe_id, recipe_revision_id = _seed(sessions)
    service = _service(sessions)
    profile = service.create(
        FleetProfileInput(name="Idle fleet", assignments=[]), actor="admin"
    )
    preview = service.preview(profile.id)
    assert preview.allowed is True, preview.reasons
    accepted = service.apply(
        profile.id,
        request_key=_uuid(880),
        actor="admin",
    )
    assert accepted.state == "succeeded"
    initial_selection = _selected(postgres_engine)

    joined_node = _node_id(2)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=joined_node,
                state="active",
                protocol_version=1,
                architecture="linux-arm64",
                last_seen_at=NOW,
            )
        )
    # Editing a saved profile is authoring only. Even an edit that assigns the
    # newly enrolled Spark must not silently change the accepted all-idle load.
    draft = _input(recipe_revision_id).model_copy(
        update={
            "expected_revision": 1,
            "assignments": [
                _input(recipe_revision_id)
                .assignments[0]
                .model_copy(update={"spark_ids": [joined_node]})
            ],
        }
    )
    service.update(profile.id, draft, actor="admin")

    # A fresh engine/service instance models a worker restart against the same
    # durable PostgreSQL authority.
    restarted_engine = create_engine(postgres_engine.url)
    try:
        restarted_sessions = sessionmaker(restarted_engine, expire_on_commit=False)
        restarted = _service(restarted_sessions)
        assert restarted.tick() is True

        selected = _selected(restarted_engine)
        assert selected["generation"] == 2
        assert selected["profile_id"] == profile.id
        assert selected["profile_revision"] == 1
        assert selected["application_id"] != accepted.id
        assert selected["roster_digest"] != initial_selection["roster_digest"]

        application = restarted.application(str(selected["application_id"]))
        assert application.state == "succeeded"
        assert application.progress.intended_profile is not None
        assert application.progress.intended_profile.assignments == []
        assert application.progress.intended_profile.scope.node_ids == sorted(
            (_node_id(1), joined_node)
        )
        view = restarted.get(profile.id)
        assert view.revision == 2
        assert view.status == "loaded"
        assert view.loaded_revision == 1
        joined = next(item for item in view.fleet if item.selector == joined_node)
        assert joined.state == "Idle"
        assert view.assignments[0].spark_ids == [joined_node]
    finally:
        restarted_engine.dispose()


def test_blocked_roster_preview_retries_without_advancing_selection(
    postgres_engine: Engine,
) -> None:
    """A temporary roster blocker cannot poison the selected generation."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _seed(sessions)
    service = _service(sessions)
    profile = service.create(
        FleetProfileInput(name="Idle fleet", assignments=[]), actor="admin"
    )
    accepted = service.apply(
        profile.id,
        request_key=_uuid(887),
        actor="admin",
    )
    assert accepted.state == "succeeded"
    original_selection = _selected(postgres_engine)
    assert isinstance(original_selection["generation"], int)
    joined_node = _node_id(2)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=joined_node,
                state="active",
                protocol_version=1,
                architecture="linux-arm64",
                last_seen_at=NOW,
            )
        )

    actual_preview = service.preview
    blocked = True

    def preview_with_temporary_blocker(*args, **kwargs):
        nonlocal blocked
        result = actual_preview(*args, **kwargs)
        if blocked and kwargs.get("accepted_intent") is not None:
            return result.model_copy(update={"allowed": False})
        return result

    service.preview = preview_with_temporary_blocker
    assert service.tick() is False
    still_selected = _selected(postgres_engine)
    assert still_selected["generation"] == original_selection["generation"]
    assert still_selected["application_id"] == accepted.id
    assert still_selected["roster_digest"] == original_selection["roster_digest"]

    blocked = False
    assert service.tick() is True
    reconciled = _selected(postgres_engine)
    assert reconciled["generation"] == int(original_selection["generation"]) + 1
    assert reconciled["application_id"] != accepted.id
    current = service.application(str(reconciled["application_id"]))
    assert current.progress.intended_profile is not None
    assert current.progress.intended_profile.scope.node_ids == sorted(
        (_node_id(1), joined_node)
    )


@pytest.mark.parametrize("authority_change", ["disabled", "demoted"])
@pytest.mark.usefixtures("damaged_json_rows")
def test_revoked_selected_actor_does_not_block_sibling_worker_and_roster_recovers(
    postgres_engine: Engine, authority_change: str
) -> None:
    """Platform maintenance repairs membership without the original admin."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _seed(sessions)
    service = _service(sessions)
    profile = service.create(
        FleetProfileInput(name="Idle fleet", assignments=[]), actor="admin"
    )
    accepted = service.apply(
        profile.id,
        request_key=_uuid(889),
        actor="admin",
    )
    assert accepted.state == "succeeded"
    original_selection = _selected(postgres_engine)
    original_generation = original_selection["generation"]
    assert isinstance(original_generation, int)

    joined_node = _node_id(2)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=joined_node,
                state="active",
                protocol_version=1,
                architecture="linux-arm64",
                last_seen_at=NOW,
            )
        )
        author = session.scalar(select(User).where(User.subject == "admin"))
        assert author is not None
        if authority_change == "disabled":
            author.disabled_at = NOW
        else:
            author.role = "viewer"
        due_job_id = _uuid(890)
        session.add(
            Job(
                id=due_job_id,
                request_id=_uuid(891),
                kind="recipe.run-switch.v2",
                state="queued",
                actor="admin",
                authority_revision="a" * 64,
                targets=[_node_id(1)],
                payload_digest="b" * 64,
                payload={"workload_intent_ordinal": 0},
                result={},
                created_at=NOW,
                updated_at=NOW,
            )
        )

    sibling = _DueDatabaseWork(sessions, due_job_id)
    worker = RecipeOperationWorker(
        sessions,
        _NoopRoutes(),
        clock=lambda: NOW,
        fleet_profiles=service,
        run_switches=sibling,
    )

    # Before the fix, the permission exception escaped the profile coordinator
    # and prevented RecipeOperationWorker from reaching the sibling job.
    assert worker.tick() is True
    with sessions() as session:
        due = session.get(Job, due_job_id)
        assert due is not None and due.state == "succeeded"
        assert len(tuple(session.scalars(select(FleetProfileApplication.id)))) == 2
    view = service.get(profile.id)
    assert not any("restore" in action.lower() for action in view.next_actions)
    assert not any("authority" in warning.lower() for warning in view.warnings)
    reconciled_selection = _selected(postgres_engine)
    assert reconciled_selection["generation"] == original_generation + 1
    assert reconciled_selection["profile_id"] == profile.id
    application_id = str(reconciled_selection["application_id"])
    for _ in range(8):
        if service.application(application_id).state == "succeeded":
            break
        _tick_selected_when_due(service, application_id)
    application = service.application(application_id)
    assert application.state == "succeeded"
    assert application.progress.intended_profile is not None
    assert application.progress.intended_profile.assignments == []
    assert application.progress.intended_profile.scope.node_ids == sorted(
        (_node_id(1), joined_node)
    )


def test_empty_live_fleet_accepts_an_all_idle_profile(postgres_engine: Engine) -> None:
    """The saved profile still governs a fleet with no active Sparks."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _seed(sessions)
    with sessions.begin() as session:
        node = session.get(AgentNode, _node_id(1))
        assert node is not None
        node.state = "retired"
        node.revoked_at = NOW
    service = _service(sessions)
    profile = service.create(
        FleetProfileInput(name="Idle fleet", assignments=[]), actor="admin"
    )

    preview = service.preview(profile.id)
    assert preview.allowed is True
    assert preview.scope.node_ids == []
    application = service.apply(
        profile.id,
        request_key=_uuid(888),
        actor="admin",
    )

    assert application.state == "succeeded"
    assert application.progress.intended_profile is not None
    assert application.progress.intended_profile.scope.node_ids == []


def test_failed_newer_load_stays_selected_instead_of_falling_back_to_success(
    postgres_engine: Engine,
) -> None:
    """A failed accepted load remains the current desired profile after restart."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _recipe_id, recipe_revision_id = _seed(sessions)
    service = _service(sessions)
    profile = service.create(
        FleetProfileInput(name="Idle then running", assignments=[]), actor="admin"
    )
    first = service.apply(
        profile.id,
        request_key=_uuid(881),
        actor="admin",
    )
    assert first.state == "succeeded"

    service.update(
        profile.id,
        _input(recipe_revision_id).model_copy(update={"expected_revision": 1}),
        actor="admin",
    )
    newer = service.apply(
        profile.id,
        request_key=_uuid(882),
        actor="admin",
    )
    assert newer.id != first.id

    # Model an execution failure after acceptance. Selection is durable desired
    # state; successful history must not silently become the active choice.
    with sessions.begin() as session:
        failed = session.get(FleetProfileApplication, newer.id)
        assert failed is not None
        failed.state = "failed"
        failed.status_reason = "test failure after acceptance"

    restarted_engine = create_engine(postgres_engine.url)
    try:
        restarted_sessions = sessionmaker(restarted_engine, expire_on_commit=False)
        restarted = _service(restarted_sessions)
        assert restarted.tick() is True
        selected = _selected(restarted_engine)
        assert selected["generation"] == 2
        assert selected["application_id"] == newer.id
        assert restarted.application(first.id).state == "succeeded"
        assert restarted.application(newer.id).state == "queued"  # retried, not failed
        with restarted_sessions() as session:
            intent = restarted.endpoint_intent(session, profile.number)
        assert intent.application_id == newer.id
        assert intent.application_state == "queued"
    finally:
        restarted_engine.dispose()


def test_pending_profile_edit_is_rejected_before_selection_after_restart(
    postgres_engine: Engine,
) -> None:
    """A parked load is not selected and cannot survive a changed review."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _recipe_id, recipe_revision_id = _seed(sessions)
    service = _service(sessions, _BusyOnceAdapter())
    profile = service.create(_input(recipe_revision_id), actor="admin")
    preview = service.preview(profile.id)
    assert preview.allowed is True
    accepted = service.apply(
        profile.id,
        request_key=_uuid(883),
        actor="admin",
    )
    assert accepted.state == "queued"
    assert accepted.progress.admission_pending is True

    # The request is still pending admission, so the saved draft can make its
    # reviewed plan stale before it becomes selected.
    service.update(
        profile.id,
        FleetProfileInput(
            name="Now idle",
            expected_revision=1,
            assignments=[],
        ),
        actor="admin",
    )

    restarted_engine = create_engine(postgres_engine.url)
    try:
        restarted_sessions = sessionmaker(restarted_engine, expire_on_commit=False)
        restarted = _service(restarted_sessions)
        assert restarted.tick() is True

        resumed = restarted.application(accepted.id)
        assert resumed.state == "superseded", resumed.status_reason
        assert resumed.progress.admission_pending is False
        assert "profile changed" in (resumed.status_reason or "").lower()
        with restarted_sessions() as session:
            assert session.get(FleetProfileSelection, 1) is None
    finally:
        restarted_engine.dispose()


def test_pending_recipe_head_change_is_followed_before_workload_fencing(
    postgres_engine: Engine,
) -> None:
    """A pending load follows a newer recipe head before it fences any workload."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _seed_canonical(sessions)
    recipe_revision_id = RECIPE_REVISION_ID
    adapter = _BusyOnceAdapter()
    service = _service(sessions, adapter)
    profile_input = _input(recipe_revision_id)
    profile_input = profile_input.model_copy(
        update={
            "assignments": [
                profile_input.assignments[0].model_copy(update={"spark_ids": [NODE_1]})
            ]
        }
    )
    profile = service.create(profile_input, actor="test")
    preview = service.preview(profile.id)
    assert preview.allowed is True, preview.reasons
    pending = service._create_pending_application(
        preview,
        request_key=_uuid(884),
        actor="test",
        operation_kind="fleet-profile.apply",
        select_profile=True,
    )
    pending = service._defer_pending_application(
        pending.id,
        "temporary test admission block",
        retry_delay=timedelta(0),
    )
    assert pending.progress.admission_pending is True

    with sessions.begin() as session:
        previous = session.get(CatalogDocumentRevision, recipe_revision_id)
        assert previous is not None
        document = RecipeDefinition.model_validate_json(json.dumps(previous.document))
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
                content_digest=document_sha256(document.model_dump(mode="json")),
                execution_key="d" * 64,
                created_by="test",
                created_at=NOW,
            )
        )

    # Acceptance is durable: maintenance still follows the newest recipe
    # revision when the author disappears before deferred admission resumes.
    with sessions.begin() as session:
        author = session.scalar(select(User).where(User.subject == "test"))
        assert author is not None
        session.delete(author)

    assert service.tick() is True

    resumed = service.application(pending.id)
    assert resumed.state == "queued", resumed.status_reason
    assert "re-planned" in (resumed.status_reason or "").lower()
    assert adapter.cancellations == []
    with sessions() as session:
        assert session.get(FleetProfileSelection, 1) is None
        node = session.get(AgentNode, NODE_1)
        assert node is not None
        assert node.workload_intent_ordinal == 0


def test_retry_uses_selected_snapshot_after_saved_profile_edit(
    postgres_engine: Engine,
) -> None:
    """Recovery applies the selected snapshot even after authoring has changed."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _recipe_id, recipe_revision_id = _seed(sessions)
    service = _service(sessions)
    profile = service.create(_input(recipe_revision_id), actor="admin")
    accepted = service.apply(
        profile.id,
        request_key=_uuid(885),
        actor="admin",
    )
    for _ in range(8):
        if accepted.state == "succeeded":
            break
        _tick_selected_when_due(service, accepted.id)
        accepted = service.application(accepted.id)
    assert accepted.state == "succeeded"
    assert accepted.progress.intended_profile is not None
    expected_assignments = accepted.progress.intended_profile.assignments

    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, accepted.id)
        assert row is not None
        row.state = "failed"
        row.status_reason = "exercise selected recovery"
    service.update(
        profile.id,
        FleetProfileInput(
            name="Now idle",
            expected_revision=1,
            assignments=[],
        ),
        actor="admin",
    )

    retried = service.retry(
        accepted.id,
        request_key=_uuid(886),
        actor="admin",
    )
    for _ in range(8):
        if retried.state == "succeeded":
            break
        _tick_selected_when_due(service, retried.id)
        retried = service.application(retried.id)

    assert retried.state == "succeeded"
    assert retried.progress.intended_profile is not None
    assert retried.progress.intended_profile.assignments == expected_assignments
    selected = _selected(postgres_engine)
    assert selected["generation"] == 1
    assert selected["application_id"] == retried.id
    assert service.retry_eligible(accepted.id) is False
