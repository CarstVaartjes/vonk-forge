"""Missing assets are requested by the platform, never left to an operator."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from vonk_control import fleet_profiles as fp


def test_a_missing_image_is_prepared_by_its_request_and_fresh_request_reuses_it(
    tmp_path,
):
    """Catches a queued request that never drives its own missing-image preparation."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from vonk_agent_protocol import LifecycleState
    from vonk_control.models import Base
    from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

    from .test_recipe_image_availability import (
        ARCHIVE,
        ARCHIVE_SHA,
        Transport,
        _add_head,
        _add_revision,
        _recipe,
        _runtime,
        _service,
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'preparation.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    recipe = _recipe("recipe-source-build.json")
    with sessions.begin() as session:
        _add_head(session, _add_revision(session, "request-led-image", recipe))
    storage = FilesystemRuntimeImageStorage(tmp_path / "images")
    transport = Transport()
    service = _service(
        sessions,
        storage=storage,
        transport=transport,
        authority=lambda recipe_revision_id, *, force=False: (recipe, _runtime()),
        clock=lambda: datetime.now(UTC),
    )
    assert not storage.build_archive_available(ARCHIVE_SHA, len(ARCHIVE))
    original = service.start(
        "request-led-image", actor="operator", request_id=str(uuid4())
    )
    service.run_pending()
    completed = service.get(original.id)
    assert completed.id == original.id and completed.state == LifecycleState.SUCCEEDED
    assert storage.existing_archive(ARCHIVE_SHA, len(ARCHIVE)).is_file()
    fresh = service.start(
        "request-led-image", actor="operator", request_id=str(uuid4())
    )
    service.run_pending()
    assert service.get(fresh.id).state == LifecycleState.SUCCEEDED
    assert storage.build_archive_available(ARCHIVE_SHA, len(ARCHIVE))
    engine.dispose()


@pytest.mark.parametrize("code", sorted(fp._PREPARATION_RESOLVABLE_CODES, key=str))
def test_every_preparation_blocker_requests_the_preparation(code: str) -> None:
    assignment = SimpleNamespace(
        assignment_id="a1", actions=("switch",), reasons=(), recipe_revision_id="r1"
    )
    reason = SimpleNamespace(code=code, severity="error")
    by_assessment = SimpleNamespace(
        assignment_id="a1", assessment=SimpleNamespace(blockers=[reason])
    )
    needing: Any = fp._assignments_needing_preparation
    assert needing([assignment], {"a1"}, [], [by_assessment]) == [assignment]
    assert needing([assignment], set(), [reason], []) == [assignment]


def test_ended_preparation_does_not_block_a_fresh_application(tmp_path) -> None:
    """A cancelled application's preparation cannot gate a new request."""
    from uuid import uuid4

    from vonk_agent_protocol import LifecycleState

    from .test_recipe_image_availability_request_recovery import _owner

    engine, _sessions, service, _now, _transport = _owner(tmp_path)
    service.ensure_preparation(
        "request-revision", actor="operator", application_id="old"
    )
    cancelled = service.cancel_profile_preparation(
        "request-revision",
        actor="operator",
        reason="application ended",
        application_id="old",
    )
    assert len(cancelled) == 1
    assert service.get(cancelled[0]).state == LifecycleState.CANCELLED
    service.ensure_preparation(
        "request-revision", actor="operator", application_id="new"
    )
    assert service.run_pending() == 1
    fresh = service.start("request-revision", actor="operator", request_id=str(uuid4()))
    assert service.run_pending() == 1
    assert service.get(fresh.id).artifact is not None
    engine.dispose()


@pytest.mark.parametrize("history_depth", [3, 65])
def test_failed_chain_is_typed_and_scoped_to_one_application(tmp_path, history_depth):
    """Failed history has bounded observation and cannot exhaust a new application."""
    from datetime import timedelta
    from uuid import NAMESPACE_URL, uuid4, uuid5

    from sqlalchemy import select
    from vonk_agent_protocol import LifecycleState, RecipeImageCode
    from vonk_control.models import Job
    from vonk_control.operation_contract import AvailabilityOperationFailure
    from vonk_control.recipe_image_availability.contracts import OPERATION_KIND
    from vonk_control.strict_json import serialize_json_value

    from .test_recipe_image_availability import _availability_payload
    from .test_recipe_image_availability_request_recovery import _owner

    engine, sessions, service, now, _transport = _owner(tmp_path)
    revision = "request-revision"
    request_id = str(
        uuid5(NAMESPACE_URL, f"vonk-forge:profile-preparation:{revision}:old")
    )
    failure = AvailabilityOperationFailure(
        code=RecipeImageCode.PREPARATION_FAILED,
        detail="The prior preparation attempt failed.",
        retryable=True,
    )
    payload = _availability_payload(revision).model_copy(update={"failure": failure})
    old = now[0] - timedelta(days=1)
    with sessions.begin() as session:
        for index in range(history_depth):
            job_id = str(uuid4())
            session.add(
                Job(
                    id=job_id,
                    request_id=request_id,
                    kind=OPERATION_KIND,
                    state=LifecycleState.FAILED.value,
                    actor="operator",
                    authority_revision=revision,
                    targets=[revision],
                    payload_digest="a" * 64,
                    payload=serialize_json_value(payload),
                    current_attempt=0,
                    created_at=old + timedelta(microseconds=index),
                    updated_at=now[0] if index == history_depth - 1 else old,
                )
            )
            request_id = str(
                uuid5(
                    NAMESPACE_URL, f"vonk-forge:profile-preparation:{revision}:{job_id}"
                )
            )
    blockers = service.ensure_preparation(
        revision, actor="operator", application_id="old"
    )
    assert len(blockers) == 1
    assert blockers[0].code == failure.code
    assert failure.detail in blockers[0].detail
    with sessions() as session:
        old_ids = set(session.scalars(select(Job.id)))
    assert len(old_ids) == history_depth

    service.ensure_preparation(revision, actor="operator", application_id="new")
    with sessions() as session:
        fresh_ids = set(session.scalars(select(Job.id))) - old_ids
    assert len(fresh_ids) == 1
    fresh_id = fresh_ids.pop()
    assert service.get(fresh_id).state == LifecycleState.QUEUED
    assert service.run_pending() == 1
    assert service.get(fresh_id).artifact is not None
    engine.dispose()


def test_pending_profile_references_are_read_from_the_canonical_stored_plan() -> None:
    """A queued load retains its resolved revision after real plan serialization."""
    from vonk_control.fleet_profiles import FleetProfileService
    from vonk_control.unused_storage_collection import _applied_revision_ids

    from .test_fleet_profiles import NOW, _database, _input, _seed, _uuid

    sessions = _database()
    _, revision_id = _seed(sessions)

    def missing(_session, _assignment, _node_ids, **_kwargs):
        raise ValueError("no successful immutable runtime image build is available")

    service = FleetProfileService(
        sessions, clock=lambda: NOW, assessment_provider=missing
    )
    profile = service.create(_input(revision_id), actor="admin")
    application = service.apply(profile.id, request_key=_uuid(922), actor="admin")
    assert application.state == "queued"
    with sessions() as session:
        assert _applied_revision_ids(session) == frozenset({revision_id})
