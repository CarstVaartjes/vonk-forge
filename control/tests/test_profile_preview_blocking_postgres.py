"""Preview must not wait in a PostgreSQL transaction for source resolution."""

import io
import time
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
from vonk_control.models import (
    CatalogDocumentRevision,
    RecipeBuild,
    RecipeSourceBundle,
    SourceBundleArchive,
)
from vonk_control.recipe_builds import RecipeBuildService
from vonk_control.source_bundles import (
    DatabaseSourceBundleStore,
    generate_source_bundle,
)

from .test_profile_capacity_admission import _capacity_profile


@pytest.mark.parametrize("damage", ["unverified", "corrupt"])
def test_slow_ingress_finishes_once_and_dual_previews_become_loadable(
    tmp_path, postgres_engine, monkeypatch, damage
):
    """Catches a permanent per-review timeout and request-time rehashing.

    Verification exceeds the former two-second deadline on EVERY invocation.
    Worker completion must survive later reviews and store reconstruction.
    Same-length corruption after ingress must be a miss, not silent reuse.
    """
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
    lifecycle = planner._lifecycle
    assert lifecycle is not None
    builds = RecipeBuildService(
        sessions,
        bundles=bundles,
        source_rederiver=lambda _projection, _path, digest: (
            bundle.archive if digest == bundle.sha256 else None
        ),
    )
    resolution = builds.resolve(revision.id)
    with sessions.begin() as session:
        cached = session.scalar(select(RecipeBuild))
        assert cached is not None
        cached.source_bundle_sha256 = bundle.sha256
        build_input = resolution.build_input_for_builder("a" * 64)
        assert build_input is not None
        cached.build_input_sha256 = build_input
        cached.policy_report = {
            **cached.policy_report,
            "builder_binary_digest": "a" * 64,
        }
    lifecycle._builds = builds
    healthy = profiles.preview(profile.id)
    assert healthy.allowed, healthy.reasons
    entered, release = Event(), Event()
    calls = []
    inspect = source_bundles._inspect_archive

    def slow_every_time(archive, limits):
        calls.append(archive)
        entered.set()
        started = time.monotonic()
        assert release.wait(15), "test did not release ingress"
        time.sleep(max(0.0, 2.1 - (time.monotonic() - started)))
        return inspect(archive, limits)

    monkeypatch.setattr(source_bundles, "_inspect_archive", slow_every_time)
    # Two reviews after ingress never hash, even through independent store objects.
    for _ in range(2):
        builds._bundles = DatabaseSourceBundleStore(sessions)
        assert profiles.preview(profile.id).allowed
    assert not calls
    with sessions.begin() as session:
        if damage == "unverified":
            row = session.get(RecipeSourceBundle, bundle.sha256)
            assert row is not None
            row.storage_key = f"postgres:{bundle.sha256}"
        else:
            session.execute(
                update(SourceBundleArchive)
                .where(SourceBundleArchive.sha256 == bundle.sha256)
                .values(archive=bundle.archive.replace(b"scratch", b"changed"))
            )
    # This is a read-only cache miss. It cannot initiate source rederivation.
    pending = profiles.preview(profile.id)
    assert not pending.allowed
    assert not calls
    with ThreadPoolExecutor(max_workers=1) as pool:
        background = pool.submit(builds.reconcile_source_bundles)
        try:
            assert entered.wait(15)
            for _ in range(2):
                result = api.post(
                    f"/api/profile/{profile.number}/preview", headers=headers
                )
                assert result.status_code == 200, result.text
                preview = FleetProfilePreview.model_validate_json(result.content)
                assert not preview.allowed
                assert any(
                    item.code == ProfileReasonCode.PREPARATION_UNAVAILABLE
                    and "pending" in item.detail
                    for item in preview.reasons
                )
            with postgres_engine.connect() as connection:
                idle = connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() AND state = 'idle in transaction'"
                    )
                )
            assert idle == 0, "background ingress held a preview transaction idle"
            assert not background.done()
        finally:
            release.set()
        assert background.result(timeout=15)
    assert calls == [bundle.archive], "only fresh ingress may verify bytes"
    # This outlives every review's memoization and a new storage instance.
    for _ in range(2):
        builds._bundles = DatabaseSourceBundleStore(sessions)
        assert profiles.preview(profile.id).allowed
    assert calls == [bundle.archive]
    assert builds._bundles.get(bundle.sha256).archive == bundle.archive


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
