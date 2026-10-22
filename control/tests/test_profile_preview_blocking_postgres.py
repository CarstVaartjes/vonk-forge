"""Preview must not wait in a PostgreSQL transaction for source resolution."""

import io
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import select, text, update
from vonk_agent_protocol import ProfileReasonCode
from vonk_control import source_bundles
from vonk_control.catalog_revision_contract import (
    RecipeRevisionProjection,
    read_catalog_projection,
)
from vonk_control.fleet_profile_contract import FleetProfilePreview
from vonk_control.models import CatalogDocumentRevision
from vonk_control.recipe_builds import RecipeBuildService
from vonk_control.source_bundles import (
    DatabaseSourceBundleStore,
    generate_source_bundle,
)

from .test_profile_capacity_admission import _capacity_profile


def test_dual_preview_bounds_source_resolution_without_idle_transactions(
    tmp_path, postgres_engine, monkeypatch
):
    """Catches unbudgeted bundle reads inside the nested preview transactions."""
    sessions, profiles, planner, profile, api, headers, _, _ = _capacity_profile(
        tmp_path, postgres_engine, node_count=2
    )
    bundle = generate_source_bundle({"Dockerfile": b"FROM scratch\nUSER 10001:10001\n"})
    bundles = DatabaseSourceBundleStore(sessions)
    bundles.put(bundle.sha256, io.BytesIO(bundle.archive))
    with sessions.begin() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe"
            )
        )
        assert revision is not None
        projection = read_catalog_projection(revision)
        assert isinstance(projection, RecipeRevisionProjection)
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == revision.id)
            .values(
                projected=projection.model_copy(
                    update={"source_bundle_sha256": bundle.sha256}
                ).model_dump(mode="json", exclude_none=True)
            )
        )
    entered, release, completed = Event(), Event(), Event()
    calls = []

    observe_archive = source_bundles._observe_archive

    def blocked(archive, limits, digest):
        calls.append(digest)
        entered.set()
        try:
            assert release.wait(15), "test did not release its source fault"
            return observe_archive(archive, limits, digest)
        finally:
            completed.set()

    lifecycle = planner._lifecycle
    assert lifecycle is not None
    lifecycle._builds = RecipeBuildService(sessions, bundles=bundles)
    healthy = profiles.preview(profile.id)
    monkeypatch.setattr(source_bundles, "_observe_archive", blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(
            api.post, f"/api/profile/{profile.number}/preview", headers=headers
        )
        try:
            assert entered.wait(30), "preview did not reach source resolution"
            with postgres_engine.connect() as connection:
                idle = connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() "
                        "AND state = 'idle in transaction'"
                    )
                )
            print(f"Idle PostgreSQL transactions during source fault: {idle}")
            result = response.result(
                timeout=5
            )  # wall-clock: preview must answer while source is blocked
            assert idle == 0, "source I/O held a preview transaction idle"
            assert result.status_code == 200, result.text
            preview = FleetProfilePreview.model_validate_json(result.content)
            assert not preview.allowed
            assert any(
                item.code == ProfileReasonCode.PREPARATION_UNAVAILABLE
                and "pending" in item.detail
                for item in preview.reasons
            )
            assert len(calls) == 1
            assert not release.is_set()
        finally:
            release.set()
            if entered.is_set():
                assert completed.wait(30)
        # Fault clearance uses the normal read path; no poisoned request state.
        recovered = profiles.preview(profile.id)
        assert recovered == healthy


def test_profile_observation_cannot_turn_statement_reads_into_writes(postgres_engine):
    """Catches a new projection writer silently committing in autocommit mode."""
    from sqlalchemy.orm import sessionmaker
    from vonk_control.models import AgentNode, Base
    from vonk_control.profile_read_observation import profile_read

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine)
    node_id = "spk_" + "f" * 32

    @profile_read
    def read():
        with sessions() as session:
            assert session.get(AgentNode, node_id) is None
            with pytest.raises(RuntimeError, match="database write"):
                session.execute(update(AgentNode).values(last_seen_at=None))
            session.add(AgentNode(node_id=node_id))
            with pytest.raises(RuntimeError, match="database flush"):
                session.flush()

    read()
    with sessions() as session:
        assert session.get(AgentNode, node_id) is None


@pytest.mark.parametrize("overlap", [False, True])
def test_build_resolution_is_memoized_within_a_read_but_fresh_for_the_next(
    tmp_path, monkeypatch, overlap
):
    """Catches retaining a revision's old receipt after a new build completes."""
    from dataclasses import replace

    from vonk_control.profile_read_observation import profile_read
    from vonk_control.recipe_builds import RecipeBuildResolution
    from vonk_control.source_bundles import SourceBundleStore

    from .test_fleet_profiles_canonical import _sessions

    builds = RecipeBuildService(_sessions(), bundles=SourceBundleStore(tmp_path))
    resolution = RecipeBuildResolution(
        recipe_revision_id="00000000-0000-4000-8000-000000000001",
        recipe_content_sha256="a" * 64,
        source_bundle_sha256="b" * 64,
        input_intent_sha256="c" * 64,
        input_intent={},
        build_id="00000000-0000-4000-8000-000000000002",
    )
    calls = []
    initial = resolution
    entered, release = Event(), Event()

    def resolve(_revision: str) -> RecipeBuildResolution:
        snapshot = resolution
        calls.append(snapshot.build_id)
        if overlap and snapshot.build_id == initial.build_id:
            entered.set()
            assert release.wait(30), "test did not release the earlier selection"
        return snapshot

    monkeypatch.setattr(builds, "resolve", resolve)

    @profile_read
    def read() -> str | None:
        first = builds.reusable_build_id(resolution.recipe_revision_id)
        assert builds.reusable_build_id(resolution.recipe_revision_id) == first
        return first

    next_build_id = "00000000-0000-4000-8000-000000000003"
    if overlap:
        with ThreadPoolExecutor(max_workers=1) as pool:
            earlier = pool.submit(read)
            try:
                assert entered.wait(30)
                resolution = replace(initial, build_id=next_build_id)
                assert read() == next_build_id
            finally:
                release.set()
            assert earlier.result(timeout=30) == initial.build_id
    else:
        assert read() == initial.build_id
        resolution = replace(initial, build_id=next_build_id)
        assert read() == next_build_id
    assert len(calls) == 2
