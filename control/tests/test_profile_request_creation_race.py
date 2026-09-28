"""Concurrent duplicate profile submissions replay one durable request."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from sqlalchemy import select
from vonk_agent_protocol import canonical_message
from vonk_control.fleet_profile_contract import (
    FleetProfileApplicationProgress,
    FleetProfileApplicationView,
)
from vonk_control.fleet_profiles import FleetProfileService
from vonk_control.models import (
    AgentOperation,
    FleetProfileApplication,
    Job,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
)

from .test_profile_load_submission import _profile_api

_EFFECT_MODELS = (
    AgentOperation,
    Job,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
)


def _effect_ids(sessions) -> tuple[tuple[str, ...], ...]:
    with sessions() as session:
        return tuple(
            tuple(session.scalars(select(model.id).order_by(model.id)))
            for model in _EFFECT_MODELS
        )


def test_concurrent_same_request_key_replays_one_typed_application(
    postgres_engine, monkeypatch
):
    """Both requests miss replay before the authority lock serializes acceptance."""
    sessions, api, _codec, headers, reviewed = _profile_api(postgres_engine)
    request_key = str(uuid4())
    body = {"request_key": request_key}
    effects_before = _effect_ids(sessions)
    both_reviewed = Barrier(2)
    original_preview = FleetProfileService.preview

    def synchronize_review(service, *args, **kwargs):
        result = original_preview(service, *args, **kwargs)
        # `apply` has already checked the request key for replay. Let both
        # callers reach acceptance before either can persist the receipt.
        both_reviewed.wait(timeout=10)
        return result

    monkeypatch.setattr(FleetProfileService, "preview", synchronize_review)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                api.post,
                "/api/profile/1/load",
                headers=headers,
                json=body,
            )
            for _ in range(2)
        ]
        responses = [future.result(timeout=20) for future in futures]

    assert [response.status_code for response in responses] == [202, 202], [
        response.text for response in responses
    ]
    typed = [
        FleetProfileApplicationView.model_validate_json(response.content, strict=True)
        for response in responses
    ]
    assert typed[0].id == typed[1].id
    assert typed[0].request_key == typed[1].request_key == request_key
    assert typed[0].profile_digest == typed[1].profile_digest
    assert typed[0].plan_digest == typed[1].plan_digest
    assert typed[0].progress.intended_profile is not None
    assert typed[1].progress.intended_profile is not None
    assert (
        typed[0].progress.intended_profile.reviewed_plan_digest
        == typed[1].progress.intended_profile.reviewed_plan_digest
        == reviewed["plan_digest"]
    )
    assert (
        typed[0].progress.intended_profile.reviewed_application_id
        == typed[1].progress.intended_profile.reviewed_application_id
        == typed[0].id
    )

    with sessions() as session:
        rows = list(
            session.scalars(
                select(FleetProfileApplication).where(
                    FleetProfileApplication.request_key == request_key
                )
            )
        )
    assert len(rows) == 1
    assert rows[0].id == typed[0].id
    persisted_progress = FleetProfileApplicationProgress.model_validate_json(
        canonical_message(rows[0].progress), strict=True
    )
    assert persisted_progress.intended_profile is not None
    assert (
        persisted_progress.intended_profile.reviewed_plan_digest
        == reviewed["plan_digest"]
    )
    # The idle profile has no admitted workload, so the duplicate request must
    # create no child, reservation, installation, run, or agent operation twice.
    assert _effect_ids(sessions) == effects_before
