"""A load that starts an installed recipe is held to an exact accepted image."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import build_production_fleet_profile_service
from vonk_control.models import CatalogDocumentRevision
from vonk_control.operation_blockers import make_blocker
from vonk_control.run_switch_operations import (
    ArtifactInspection,
    RunSwitchOperationService,
)

from .test_recipe_operations import installed_recipe, setup_services
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
)


class _ModelSetSizeUntilPrepared(CompleteArtifactInspector):
    """The model set size is unknown until the Controller has prepared it."""

    def __init__(self) -> None:
        super().__init__()
        self.prepared = False

    def inspect(
        self,
        session: Session,
        *,
        model_content_sha256: str,
        recipe_revision_id: str,
        node_ids: tuple[str, ...],
        retention: str,
        now: datetime,
    ) -> ArtifactInspection:
        inspection = super().inspect(
            session,
            model_content_sha256=model_content_sha256,
            recipe_revision_id=recipe_revision_id,
            node_ids=node_ids,
            retention=retention,
            now=now,
        )
        if self.prepared:
            return inspection
        return dataclasses.replace(inspection, artifact_set_bytes=None)


def test_starting_an_installed_recipe_waits_for_preparation_then_starts(
    tmp_path: Path,
) -> None:
    """Catches an admitted start whose review recorded no runtime image.

    The recipe is installed on its Sparks but not running; the review cannot
    name the exact runtime image yet.  The load waits for preparation instead
    of being admitted without an image, and once preparation completes the
    same load issues its start, with no cancel and no new load.
    """
    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installed_recipe(lifecycle, mapping_id, build_id, nodes, request_id=str(uuid4()))
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
    assert revision is not None
    inspector = _ModelSetSizeUntilPrepared()
    run_switch = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lifecycle._clock,
        artifacts=inspector,
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=0,
    )
    service = build_production_fleet_profile_service(
        sessions, clock=lifecycle._clock, run_switch_operations=run_switch
    )
    requested: list[str] = []

    def prepare(
        recipe_revision_id: str, *, actor: str, application_id: str | None = None
    ):
        requested.append(recipe_revision_id)
        return [
            make_blocker(
                "recipe_image.preparing",
                "Preparing the model and runtime image (prepare).",
                severity="info",
            )
        ]

    service.bind_preparation_starter(prepare)
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Start installed",
                "assignments": [
                    {
                        "recipe_selector": f"vonk-forge/{revision.slug}",
                        "spark_ids": list(nodes),
                        "desired_state": "running",
                        "assignment_name": "installed-chat",
                    }
                ],
            }
        ),
        actor="admin",
    )

    review = service.preview(profile.id)
    assert [item.current_state for item in review.assignments] == ["installed"]
    assert not review.preparation_decisions
    assert not review.allowed
    assert review.waits_for_preparation

    application = service.apply(profile.id, request_key=str(uuid4()), actor="admin")
    assert application.state == "queued"
    assert requested == [revision.id]

    # Preparation completes; the same load continues by itself.
    inspector.prepared = True
    now = [lifecycle._clock()]
    service._clock = lambda: now[0]
    for _ in range(6):
        now[0] += timedelta(seconds=30)
        service.tick()
    loaded = service.application(application.id)
    assert loaded.state == "running", loaded.status_reason
    assert [effect.state for effect in loaded.progress.effects] == ["pending"]
    assert not loaded.blockers, loaded.blockers
