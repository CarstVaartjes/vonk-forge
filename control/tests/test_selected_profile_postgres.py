from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import (
    FleetProfileAdmissionEffectBusy,
    FleetProfileService,
)
from vonk_control.models import AgentNode, Base, FleetProfileApplication

from .test_fleet_profiles import (
    _assessment,
    _exact_preparation,
    _input,
    _node_id,
    _seed,
    _SwitchAdapter,
    _uuid,
)

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


class _BusyOnceAdapter(_SwitchAdapter):
    def __init__(self) -> None:
        super().__init__()
        self._busy = True

    def validate_resources_in_session(self, session, assignments, reviewed) -> None:
        if self._busy:
            self._busy = False
            raise FleetProfileAdmissionEffectBusy("temporary test admission block")


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
    assert preview.allowed is True
    accepted = service.apply(
        profile.id,
        plan_digest=preview.plan_digest,
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
                capabilities=[],
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
        joined = next(item for item in view.fleet if item["selector"] == joined_node)
        assert joined["state"] == "Idle"
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
    accepted_preview = service.preview(profile.id)
    accepted = service.apply(
        profile.id,
        plan_digest=accepted_preview.plan_digest,
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
                capabilities=[],
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
        plan_digest=preview.plan_digest,
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
    first_preview = service.preview(profile.id)
    first = service.apply(
        profile.id,
        plan_digest=first_preview.plan_digest,
        request_key=_uuid(881),
        actor="admin",
    )
    assert first.state == "succeeded"

    service.update(
        profile.id,
        _input(recipe_revision_id).model_copy(update={"expected_revision": 1}),
        actor="admin",
    )
    newer_preview = service.preview(profile.id)
    newer = service.apply(
        profile.id,
        plan_digest=newer_preview.plan_digest,
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
        assert restarted.tick() is False
        selected = _selected(restarted_engine)
        assert selected["generation"] == 2
        assert selected["application_id"] == newer.id
        assert restarted.application(first.id).state == "succeeded"
        assert restarted.application(newer.id).state == "failed"
        with restarted_sessions() as session:
            intent = restarted.endpoint_intent(session, profile.number)
        assert intent.application_id == newer.id
        assert intent.application_state == "failed"
    finally:
        restarted_engine.dispose()


def test_pending_selected_profile_resumes_from_snapshot_after_restart_and_edit(
    postgres_engine: Engine,
) -> None:
    """A restart retries the accepted assignment even if its saved draft changes."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _recipe_id, recipe_revision_id = _seed(sessions)
    service = _service(sessions, _BusyOnceAdapter())
    profile = service.create(_input(recipe_revision_id), actor="admin")
    preview = service.preview(profile.id)
    assert preview.allowed is True
    accepted = service.apply(
        profile.id,
        plan_digest=preview.plan_digest,
        request_key=_uuid(883),
        actor="admin",
    )
    assert accepted.state == "waiting-for-operator"
    assert accepted.progress.admission_pending is True

    # The accepted load is the desired fact. Later autosaves do not replace it.
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

        selected = _selected(restarted_engine)
        assert selected["application_id"] == accepted.id
        assert selected["profile_revision"] == 1
        resumed = restarted.application(accepted.id)
        assert resumed.state in {"queued", "running"}, resumed.status_reason
        assert resumed.progress.admission_pending is False
        assert resumed.progress.intended_profile is not None
        assert [item.id for item in resumed.progress.intended_profile.assignments] == [
            item.id for item in preview.resolved_assignments
        ]
        assert restarted.get(profile.id).loaded_revision == 1
    finally:
        restarted_engine.dispose()


def test_retry_uses_selected_snapshot_after_saved_profile_edit(
    postgres_engine: Engine,
) -> None:
    """Recovery applies the selected snapshot even after authoring has changed."""

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _recipe_id, recipe_revision_id = _seed(sessions)
    service = _service(sessions)
    profile = service.create(_input(recipe_revision_id), actor="admin")
    preview = service.preview(profile.id)
    accepted = service.apply(
        profile.id,
        plan_digest=preview.plan_digest,
        request_key=_uuid(885),
        actor="admin",
    )
    for _ in range(8):
        if accepted.state == "succeeded":
            break
        service.tick()
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
        service.tick()
        retried = service.application(retried.id)

    assert retried.state == "succeeded"
    assert retried.progress.intended_profile is not None
    assert retried.progress.intended_profile.assignments == expected_assignments
    selected = _selected(postgres_engine)
    assert selected["generation"] == 1
    assert selected["application_id"] == retried.id
    assert service.retry_eligible(accepted.id) is False
