from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.artifact_lifecycle import (
    ArtifactIdentity,
    check_removal_fence_nowait,
    reference_gate_is_open_nowait,
    release_dead_removal_nowait,
    require_reference_open,
    reserve_removal,
)
from vonk_control.artifact_reference_scan import require_model_sets_open
from vonk_control.models import ArtifactLifecycleGate, Base, ModelCacheSet


@pytest.fixture
def artifact_engine(postgres_engine: Engine) -> Engine:
    Base.metadata.create_all(postgres_engine)
    return postgres_engine


@pytest.fixture
def artifact_sessions(artifact_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(artifact_engine, expire_on_commit=False)


def test_reference_admission_cannot_cross_uncommitted_deletion_reservation(
    artifact_sessions: sessionmaker,
) -> None:
    identity = ArtifactIdentity("runtime-image", "a" * 64)
    reserved = Event()
    release = Event()

    def reserve() -> None:
        with artifact_sessions.begin() as session:
            reserve_removal(
                session,
                (identity,),
                owner_kind="recipe-image-job",
                owner_id="8f19e31a-7155-45da-9f1b-c7da400180b7",
                fence="ac30cde4-34ee-4b4a-9ad0-e9da0ba2dffd",
                now=datetime.now(UTC),
            )
            reserved.set()
            assert release.wait(timeout=3)

    with ThreadPoolExecutor(max_workers=2) as pool:
        deletion = pool.submit(reserve)
        assert reserved.wait(timeout=2)
        with (
            artifact_sessions.begin() as session,
            pytest.raises(Exception),  # noqa: B017 -- ending witness; effects and fresh admission establish behaviour
        ):
            require_reference_open(session, (identity,), now=datetime.now(UTC))
        release.set()
        deletion.result(timeout=3)

    with (
        artifact_sessions.begin() as session,
        pytest.raises(Exception),  # noqa: B017 -- ending witness; effects and fresh admission establish behaviour
    ):
        require_reference_open(session, (identity,), now=datetime.now(UTC))

    with artifact_sessions.begin() as session:
        assert release_dead_removal_nowait(
            session, identity, owner_kind="recipe-image-job", now=datetime.now(UTC)
        )
        require_reference_open(session, (identity,), now=datetime.now(UTC))


def test_reused_session_refreshes_committed_deletion_fence(
    artifact_sessions: sessionmaker[Session],
) -> None:
    identity = ArtifactIdentity("runtime-image", "e" * 64)
    reused = artifact_sessions()
    try:
        with reused.begin():
            require_reference_open(reused, (identity,), now=datetime.now(UTC))
            preloaded = reused.get(
                ArtifactLifecycleGate,
                {
                    "artifact_kind": identity.kind,
                    "artifact_sha256": identity.sha256,
                },
            )
            assert preloaded is not None and preloaded.removal_owner_id is None

        with artifact_sessions.begin() as other:
            reserve_removal(
                other,
                (identity,),
                owner_kind="recipe-image-job",
                owner_id="8f19e31a-7155-45da-9f1b-c7da400180b7",
                fence="ac30cde4-34ee-4b4a-9ad0-e9da0ba2dffd",
                now=datetime.now(UTC),
            )
        assert preloaded.removal_owner_id is None

        with pytest.raises(Exception), reused.begin():  # noqa: B017 -- ending witness; effects and fresh admission establish behaviour
            require_reference_open(reused, (identity,), now=datetime.now(UTC))
    finally:
        reused.close()
    with artifact_sessions.begin() as session:
        assert release_dead_removal_nowait(
            session, identity, owner_kind="recipe-image-job", now=datetime.now(UTC)
        )
        require_reference_open(session, (identity,), now=datetime.now(UTC))


def test_removal_fence_nowait_reports_row_contention_then_recovers(
    artifact_sessions: sessionmaker[Session],
) -> None:
    identity = ArtifactIdentity("runtime-image", "b" * 64)
    owner_id = "8f19e31a-7155-45da-9f1b-c7da400180b7"
    fence = "ac30cde4-34ee-4b4a-9ad0-e9da0ba2dffd"
    with artifact_sessions.begin() as session:
        reserve_removal(
            session,
            (identity,),
            owner_kind="recipe-image-job",
            owner_id=owner_id,
            fence=fence,
            now=datetime.now(UTC),
        )

    locked = Event()
    release = Event()

    def hold_gate_row() -> None:
        with artifact_sessions.begin() as session:
            row = session.scalar(
                select(ArtifactLifecycleGate)
                .where(
                    ArtifactLifecycleGate.artifact_kind == identity.kind,
                    ArtifactLifecycleGate.artifact_sha256 == identity.sha256,
                )
                .with_for_update()
            )
            assert row is not None
            locked.set()
            assert release.wait(timeout=3)

    with ThreadPoolExecutor(max_workers=2) as pool:
        holder = pool.submit(hold_gate_row)
        assert locked.wait(timeout=2)
        with (
            artifact_sessions.begin() as session,
            pytest.raises(Exception),  # noqa: B017 -- ending witness; effects and fresh admission establish behaviour
        ):
            check_removal_fence_nowait(
                session,
                identity,
                owner_kind="recipe-image-job",
                owner_id=owner_id,
                fence=fence,
            )
        with (
            artifact_sessions.begin() as session,
            pytest.raises(Exception),  # noqa: B017 -- ending witness; effects and fresh admission establish behaviour
        ):
            reference_gate_is_open_nowait(session, identity)
        release.set()
        holder.result(timeout=3)

    with artifact_sessions.begin() as session:
        assert (
            check_removal_fence_nowait(
                session,
                identity,
                owner_kind="recipe-image-job",
                owner_id=owner_id,
                fence=fence,
            )
            is True
        )
        assert reference_gate_is_open_nowait(session, identity) is False


def test_reference_sql_error_is_not_misclassified_and_retry_recovers(
    artifact_engine: Engine,
    artifact_sessions: sessionmaker[Session],
) -> None:
    with artifact_engine.begin() as connection:
        connection.execute(text("DROP TABLE artifact_lifecycle_gates"))
    identity = ArtifactIdentity("runtime-image", "c" * 64)

    with pytest.raises(Exception), artifact_sessions.begin() as session:  # noqa: B017 -- ending witness; effects and fresh admission establish behaviour
        require_reference_open(session, (identity,), now=datetime.now(UTC))

    with pytest.raises(Exception), artifact_sessions.begin() as session:  # noqa: B017 -- ending witness; effects and fresh admission establish behaviour
        reference_gate_is_open_nowait(session, identity)

    Base.metadata.create_all(artifact_engine)
    with artifact_sessions.begin() as session:
        require_reference_open(session, (identity,), now=datetime.now(UTC))
        assert reference_gate_is_open_nowait(session, identity) is True


def test_valid_empty_cache_manifest_remains_an_open_exact_set(
    artifact_sessions: sessionmaker[Session],
) -> None:
    now = datetime.now(UTC)
    set_digest = "d" * 64
    with artifact_sessions.begin() as session:
        session.add(
            ModelCacheSet(
                artifact_set_sha256=set_digest,
                schema_version=2,
                model_content_sha256=None,
                recipe_revision_sha256=None,
                manifest={
                    "schema_version": 2,
                    "source_policy": "nas-first",
                    "model_content_sha256": None,
                    "recipe_revision_sha256": None,
                    "model_definition_ref": None,
                    "model_content_digests": [],
                    "artifacts": [],
                },
                expected_bytes=0,
                verified_bytes=0,
                state="incomplete",
                protected=False,
                protected_reasons=[],
                created_at=now,
                updated_at=now,
                verified_at=None,
                last_accessed_at=now,
                last_error=None,
            )
        )

    with artifact_sessions.begin() as session:
        assert require_model_sets_open(session, (set_digest,), now=now) == {
            set_digest: ()
        }


@pytest.mark.parametrize("owner_state", [None, "failed", "cancelled", "succeeded"])
def test_dead_removal_gate_reconciles_before_fresh_reference_admission(
    artifact_sessions: sessionmaker[Session],
    owner_state: str | None,
) -> None:
    from vonk_control.artifact_lifecycle import (
        dead_removal_identities,
        release_dead_removal_nowait,
    )
    from vonk_control.models import Job

    now = datetime.now(UTC)
    identity = ArtifactIdentity("runtime-image", "b" * 64)
    owner_id = "8f19e31a-7155-45da-9f1b-c7da400180b7"
    with artifact_sessions.begin() as session:
        if owner_state is not None:
            session.add(
                Job(
                    id=owner_id,
                    request_id="8f19e31a-7155-45da-9f1b-c7da400180b8",
                    kind="recipe.cache.remove.v2",
                    state=owner_state,
                    actor="operator",
                    authority_revision="revision",
                    targets=[],
                    payload_digest="c" * 64,
                    payload={},
                    current_attempt=0,
                    created_at=now,
                    updated_at=now,
                )
            )
        reserve_removal(
            session,
            (identity,),
            owner_kind="recipe-image-job",
            owner_id=owner_id,
            fence="8f19e31a-7155-45da-9f1b-c7da400180b9",
            now=now,
        )
    with artifact_sessions() as session:
        assert dead_removal_identities(
            session, owner_kind="recipe-image-job", limit=8
        ) == (identity,)
    # Production obtains the exact filesystem lock before this short transaction.
    with artifact_sessions.begin() as session:
        assert release_dead_removal_nowait(
            session, identity, owner_kind="recipe-image-job", now=now
        )
    with artifact_sessions.begin() as session:
        require_reference_open(session, (identity,), now=now)
        assert not check_removal_fence_nowait(
            session,
            identity,
            owner_kind="recipe-image-job",
            owner_id=owner_id,
            fence="8f19e31a-7155-45da-9f1b-c7da400180b9",
        )


def test_reaper_rechecks_owner_and_does_not_clear_a_restarted_removal(
    artifact_sessions: sessionmaker[Session],
) -> None:
    from vonk_control.artifact_lifecycle import (
        dead_removal_identities,
        release_dead_removal_nowait,
    )
    from vonk_control.models import Job

    now = datetime.now(UTC)
    identity = ArtifactIdentity("runtime-image", "c" * 64)
    owner_id = "8f19e31a-7155-45da-9f1b-c7da400180b7"
    with artifact_sessions.begin() as session:
        session.add(
            Job(
                id=owner_id,
                request_id="8f19e31a-7155-45da-9f1b-c7da400180b8",
                kind="recipe.cache.remove.v2",
                state="failed",
                actor="operator",
                authority_revision="revision",
                targets=[],
                payload_digest="c" * 64,
                payload={},
                current_attempt=0,
                created_at=now,
                updated_at=now,
            )
        )
        reserve_removal(
            session,
            (identity,),
            owner_kind="recipe-image-job",
            owner_id=owner_id,
            fence="8f19e31a-7155-45da-9f1b-c7da400180b9",
            now=now,
        )
    with artifact_sessions() as session:
        assert dead_removal_identities(
            session, owner_kind="recipe-image-job", limit=8
        ) == (identity,)
    with artifact_sessions.begin() as session:
        owner = session.get(Job, owner_id)
        assert owner is not None
        owner.state = "queued"
    with artifact_sessions.begin() as session:
        assert not release_dead_removal_nowait(
            session, identity, owner_kind="recipe-image-job", now=now
        )
        assert check_removal_fence_nowait(
            session,
            identity,
            owner_kind="recipe-image-job",
            owner_id=owner_id,
            fence="8f19e31a-7155-45da-9f1b-c7da400180b9",
        )
