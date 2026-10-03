"""A recipe's images are derived from its builds, never from a per-revision grant.

An editorial successor with the same executable inputs runs its predecessor's
build and has no build row of its own. Retention, presence and removal readers
ask ``revision_images`` which archives a revision can run, and the answer comes
from the recipe document's builds: nothing records that a revision "may" use an
image.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.artifact_reference_scan import runtime_image_reference_findings
from vonk_control.models import (
    Base,
    FleetProfile,
    RecipeBuild,
)
from vonk_control.revision_images import (
    revision_archives,
    revision_images,
    revisions_running_archives,
)

from .test_recipe_image_availability import (
    ARCHIVE,
    ARCHIVE_SHA,
    IMAGE_DIGEST,
    _add_head,
    _add_revision,
    _recipe,
    _recipe_projection,
    _successor,
)

NOW = datetime(2026, 10, 3, tzinfo=UTC)


@pytest.fixture
def sessions() -> sessionmaker[Session]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(engine)


def _build(
    session: Session,
    revision_id: str,
    *,
    archive: str,
    source: str = "b" * 64,
    updated: datetime = NOW,
    state: str = "succeeded",
) -> str:
    build_id = str(uuid.uuid4())
    built = state == "succeeded"
    session.add(
        RecipeBuild(
            id=build_id,
            recipe_revision_id=revision_id,
            builder_node_id="spark-builder",
            source_bundle_sha256=source,
            build_input_sha256=hashlib.sha256(build_id.encode()).hexdigest(),
            state=state,
            policy_report={},
            plan={},
            image_digest=IMAGE_DIGEST if built else None,
            oci_layout_sha256=archive if built else None,
            image_bytes=len(ARCHIVE) if built else None,
            created_at=updated,
            updated_at=updated,
        )
    )
    return build_id


@dataclass(frozen=True)
class _Lineage:
    first: str
    second: str
    selector: str


def _lineage(session: Session, *, successor_source: str = "b" * 64):
    """A recipe whose head revision is an editorial successor with no build."""

    recipe = _recipe("recipe-source-build.json")
    first = _add_revision(session, "revision-first", recipe, built=False)
    second = _add_revision(
        session, "revision-second", _successor(recipe, "Edited title"), built=False
    )
    second.document_id = first.document_id
    second.revision_number = 2
    second.projected = _recipe_projection(
        _successor(recipe, "Edited title"), successor_source
    )
    _add_head(session, second)
    return _Lineage(
        first=first.id,
        second=second.id,
        selector=f"{second.publisher}/{second.slug}",
    )


def test_a_successor_runs_the_image_its_predecessor_built(sessions) -> None:
    with sessions.begin() as session:
        lineage = _lineage(session)
        _build(session, lineage.first, archive=ARCHIVE_SHA)
    with sessions() as session:
        images = revision_images(session, [lineage.second], same_source=True)
        assert [image.archive_sha256 for image in images[lineage.second]] == [
            ARCHIVE_SHA
        ]
        assert revision_archives(session, [lineage.second]) == {ARCHIVE_SHA}


def test_a_changed_source_is_not_the_image_the_successor_would_run(sessions) -> None:
    with sessions.begin() as session:
        lineage = _lineage(session, successor_source="9" * 64)
        _build(session, lineage.first, archive=ARCHIVE_SHA)
    with sessions() as session:
        # Presence asks about the successor's own source: nothing is built for it.
        assert (
            revision_images(session, [lineage.second], same_source=True)[lineage.second]
            == ()
        )
        # Retention keeps too much rather than too little: unknown means keep.
        assert revision_archives(session, [lineage.second]) == {ARCHIVE_SHA}


def test_the_newest_build_of_the_same_source_comes_first(sessions) -> None:
    with sessions.begin() as session:
        lineage = _lineage(session)
        _build(
            session, lineage.first, archive="1" * 64, updated=NOW - timedelta(days=1)
        )
        _build(session, lineage.second, archive="2" * 64, updated=NOW)
        _build(session, lineage.first, archive="3" * 64, state="failed")
    with sessions() as session:
        images = revision_images(session, [lineage.second], same_source=True)[
            lineage.second
        ]
        assert [image.archive_sha256 for image in images] == ["2" * 64, "1" * 64]


def test_another_recipes_builds_are_never_this_recipes_images(sessions) -> None:
    other = _recipe("recipe-job.json")
    with sessions.begin() as session:
        lineage = _lineage(session)
        mine = _add_revision(session, "revision-other", other, built=False).id
        _build(session, lineage.first, archive="1" * 64)
        _build(session, mine, archive="2" * 64)
    with sessions() as session:
        assert revision_archives(session, [lineage.second]) == {"1" * 64}
        assert revision_archives(session, [mine]) == {"2" * 64}
        assert revision_archives(session, []) == frozenset()


def test_the_revisions_that_run_an_archive_are_every_revision_of_its_recipes(
    sessions,
) -> None:
    with sessions.begin() as session:
        lineage = _lineage(session)
        mine = _add_revision(
            session, "revision-other", _recipe("recipe-job.json"), built=False
        ).id
        _build(session, lineage.first, archive="1" * 64)
    with sessions() as session:
        running = revisions_running_archives(session, ["1" * 64, "2" * 64])
        assert running["1" * 64] == {lineage.first, lineage.second}
        assert mine not in running["1" * 64]
        assert running["2" * 64] == frozenset()


def test_a_saved_profile_keeps_the_image_its_recipes_successor_reuses(
    sessions,
) -> None:
    """The reference scan finds the saved profile's image without a grant."""

    with sessions.begin() as session:
        lineage = _lineage(session)
        _build(session, lineage.first, archive=ARCHIVE_SHA)
        session.add(
            FleetProfile(
                id=str(uuid.uuid4()),
                number=1,
                revision=1,
                name="saved",
                description="",
                installation_policy="keep-cached",
                labels={},
                favorite=False,
                assignments=[
                    {
                        "recipe_selector": lineage.selector,
                        "spark_ids": ["spk_" + "a" * 32],
                        "option_choices": {},
                    }
                ],
                created_by="operator",
                created_at=NOW,
                updated_at=NOW,
            )
        )
    with sessions() as session:
        findings = runtime_image_reference_findings(session, [ARCHIVE_SHA, "9" * 64])
    assert [item.classification for item in findings[ARCHIVE_SHA]] == [
        "saved-reference"
    ]
    assert findings["9" * 64] == ()


def test_a_new_saved_profile_selector_waits_for_removal_of_the_image_it_resolves_to(
    sessions,
) -> None:
    """Saving a selector serializes with removal of the recipe's own images."""

    from vonk_control.artifact_lifecycle import ArtifactIdentity, reserve_removal
    from vonk_control.fleet_profile_contract import FleetProfileAssignmentInput
    from vonk_control.fleet_profiles import FleetProfileConflict, FleetProfileService

    with sessions.begin() as session:
        lineage = _lineage(session)
        _build(session, lineage.first, archive=ARCHIVE_SHA)
    assignment = FleetProfileAssignmentInput.model_validate(
        {
            "recipe_selector": lineage.selector,
            "spark_ids": ["spk_" + "a" * 32],
            "option_choices": {},
        }
    )
    with sessions.begin() as session:
        # Nothing is being removed: the selector can be saved.
        FleetProfileService._reserve_saved_profile_references(
            session, [assignment], now=NOW
        )
        reserve_removal(
            session,
            [ArtifactIdentity("runtime-image", ARCHIVE_SHA)],
            owner_kind="recipe-image-job",
            owner_id=str(uuid.uuid4()),
            fence=str(uuid.uuid4()),
            now=NOW,
        )
    with sessions.begin() as session, pytest.raises(FleetProfileConflict):
        FleetProfileService._reserve_saved_profile_references(
            session, [assignment], now=NOW
        )
