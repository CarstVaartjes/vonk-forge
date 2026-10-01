"""The layered store keeps what the Controller names and reclaims the rest."""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.image_store_collection import GRACE, ImageStoreCollector
from vonk_control.models import (
    AgentNode,
    ArtifactDistributionAssignment,
    Base,
    RecipeBuild,
)
from vonk_control.runtime_image_preparation import FilesystemRuntimeImageStorage

from .runtime_image_fixtures import place_test_image

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)
NODE = "spk_" + "1" * 32


@pytest.fixture
def system(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'controller.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(AgentNode(node_id=NODE, state="active", workload_intent_ordinal=1))
    storage = FilesystemRuntimeImageStorage(tmp_path / "artifacts")
    collector = ImageStoreCollector(
        sessions, tmp_path / "artifacts", clock=lambda: NOW, store=storage.layout
    )
    return sessions, storage, collector


def _age(*paths: Path) -> None:
    old = time.time() - GRACE.total_seconds() - 60
    for path in paths:
        os.utime(path, (old, old))


def _image(storage: FilesystemRuntimeImageStorage, address: str) -> set[Path]:
    place_test_image(storage, address, 1024)
    image = storage.layout.read(f"sha256:{address}")
    assert image is not None
    return {storage.layout.blob_path(digest) for digest in image.blob_digests}


def _blobs(storage: FilesystemRuntimeImageStorage) -> set[Path]:
    return set((storage.layout.root / "blobs" / "sha256").iterdir())


def _build(sessions, address: str, *, state: str, updated_at: datetime) -> None:
    with sessions.begin() as session:
        session.add(
            RecipeBuild(
                id=str(uuid.uuid4()),
                recipe_revision_id=str(uuid.uuid4()),
                builder_node_id=NODE,
                source_bundle_sha256="c" * 64,
                build_input_sha256="e" * 64,
                state=state,
                policy_report={},
                plan={},
                image_digest=f"sha256:{address}",
                oci_layout_sha256=address,
                image_bytes=1024,
                created_at=updated_at,
                updated_at=updated_at,
            )
        )


def test_a_retired_image_goes_and_a_published_one_keeps_its_shared_blobs(
    system,
) -> None:
    _sessions, storage, collector = system
    published = _image(storage, "a" * 64)
    (storage.root / f"{'a' * 64}.receipt.json").write_text("{}")
    retired = _image(storage, "b" * 64)
    _age(*_blobs(storage))

    result = collector.collect()

    assert result is not None
    # Both images share their config; the published image keeps it.
    assert _blobs(storage) == published
    assert result.blobs_removed == len(retired - published)
    assert result.bytes_reclaimed > 1024


def test_fresh_pulling_and_waiting_images_are_kept(system) -> None:
    sessions, storage, collector = system
    fresh = _image(storage, "1" * 64)
    pulling = _image(storage, "2" * 64)
    waiting = _image(storage, "3" * 64)
    forgotten = _image(storage, "4" * 64)
    _age(*(_blobs(storage) - fresh))
    with sessions.begin() as session:
        session.add(
            ArtifactDistributionAssignment(
                plan_digest="f" * 64,
                node_id=NODE,
                generation=1,
                expires_at=NOW + timedelta(hours=1),
                model_artifact_set_sha256="d" * 64,
                objects=[],
                oci_image_digest="sha256:" + "2" * 64,
                oci_image_config_digest="sha256:" + "9" * 64,
                oci_archive_sha256="2" * 64,
                state="active",
                created_at=NOW,
                updated_at=NOW,
            )
        )
    _build(sessions, "3" * 64, state="succeeded", updated_at=NOW - timedelta(hours=1))
    _build(sessions, "4" * 64, state="succeeded", updated_at=NOW - timedelta(days=3))

    assert collector.collect() is not None

    remaining = _blobs(storage)
    assert fresh | pulling | waiting <= remaining
    assert not (forgotten - fresh - pulling - waiting) & remaining


def test_a_busy_store_is_left_for_the_next_round(system) -> None:
    _sessions, storage, collector = system
    blobs = _image(storage, "b" * 64)
    _age(*blobs)

    with storage.layout._lock():
        assert collector.collect() is None
    assert _blobs(storage) == blobs


def test_an_upload_no_build_waits_on_is_reclaimed(system) -> None:
    sessions, storage, collector = system
    storage.root.mkdir(parents=True, exist_ok=True)
    waiting = storage.root / ("5" * 64)
    abandoned = storage.root / ("6" * 64)
    for upload in (waiting, abandoned):
        upload.write_bytes(b"docker archive")
    _age(waiting, abandoned)
    _build(sessions, "5" * 64, state="succeeded", updated_at=NOW - timedelta(days=3))
    _build(sessions, "6" * 64, state="failed", updated_at=NOW - timedelta(days=3))

    collector.collect()

    assert waiting.exists()
    assert not abandoned.exists()


def test_collection_runs_once_per_interval(system) -> None:
    _sessions, storage, collector = system
    _age(*_image(storage, "b" * 64))
    assert collector.tick() is True
    _age(*_image(storage, "c" * 64))
    assert collector.tick() is False
    assert len(_blobs(storage)) > 0
