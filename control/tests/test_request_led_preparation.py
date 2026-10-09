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


def test_exhausted_chain_is_typed_and_scoped_to_one_application(monkeypatch) -> None:
    """Old terminal preparations cannot exhaust a fresh application's retry chain."""
    from datetime import UTC, datetime, timedelta
    from uuid import NAMESPACE_URL, uuid5

    from vonk_agent_protocol import OperationProgress, RecipeImageCode
    from vonk_control import recipe_image_availability as ria
    from vonk_control.operation_contract import AvailabilityOperationFailure
    from vonk_control.recipe_image_availability_view_contract import (
        RecipeImageAvailabilityView,
    )

    now = datetime(2026, 10, 6, tzinfo=UTC)
    old = (now - timedelta(days=1)).isoformat()
    exhausted_root = str(uuid5(NAMESPACE_URL, "vonk-forge:profile-preparation:r1:old"))
    requests: list[str] = []
    service = object.__new__(ria.RecipeImageAvailabilityService)
    service._clock = lambda: now

    def start(revision, *, actor, request_id):
        requests.append(request_id)
        state = (
            "queued"
            if request_id
            == str(uuid5(NAMESPACE_URL, "vonk-forge:profile-preparation:r1:new"))
            else "failed"
        )
        return RecipeImageAvailabilityView(
            id=request_id,
            request_id=request_id,
            request=None,
            kind="recipe.image.ensure",
            state=state,
            attempt=1,
            recipe_revision_id=revision,
            recipe_content_sha256=None,
            model_digest=None,
            build_input_sha256=None,
            progress=OperationProgress(phase="queued") if state == "queued" else None,
            image_progress=None,
            result=None,
            failure=AvailabilityOperationFailure(
                code=RecipeImageCode.PREPARATION_FAILED,
                detail="The prior preparation attempt failed.",
                retryable=True,
            )
            if state == "failed"
            else None,
            supported_actions=(),
            created_at=old,
            updated_at=old,
            blockers=(),
        )

    monkeypatch.setattr(service, "start", start)
    monkeypatch.setattr(ria, "_PREPARATION_CHAIN_LIMIT", 2)
    blockers = service.ensure_preparation("r1", actor="admin", application_id="old")
    assert requests[0] == exhausted_root
    assert blockers
    old_requests = tuple(requests)
    assert len(old_requests) <= 3
    blockers = service.ensure_preparation("r1", actor="admin", application_id="new")
    assert blockers
    assert len(requests) == len(old_requests) + 1
    assert requests[-1] not in old_requests


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
