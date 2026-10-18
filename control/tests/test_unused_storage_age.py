# ruff: noqa: F811 - pytest fixture imports are intentionally reused as arguments
"""Age expiry is independent of pressure and never expires model files."""

from datetime import timedelta
from pathlib import Path

import pytest
from vonk_agent_protocol import InstallationState, RunState

from .test_catalog_revision_collection import NOW, Catalog
from .test_unused_storage_collection import (
    AN_IMAGE,
    TOTAL,
    FakeLifecycle,
    _collector,
    _receipt,
    _size,
    cached,  # noqa: F401 - shared model cache fixture
    world,  # noqa: F401 - shared database fixture
)


def test_non_model_expiry_survives_restart_and_retries_failed_unlink(
    world: Catalog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches pressure-only GC, premature expiry, and losing failed deletions."""
    revision = world.revision("glm", 1, head="active")
    idle, _ = world.workload(revision, run=RunState.STOPPED, touched=NOW)
    live, _ = world.workload(revision, run=RunState.RUNNING, touched=NOW)
    _size(world, idle, 1024)
    _size(world, live, 1024)
    receipt = _receipt(tmp_path, AN_IMAGE, age=timedelta(0))
    model = tmp_path / "model-cache" / "shared-weights"
    model.parent.mkdir()
    model.write_bytes(b"model stays without pressure")
    lifecycle = FakeLifecycle(world.sessions, apply=True)

    def restarted(age: timedelta):
        return _collector(
            world, lifecycle, now=NOW + age, free=TOTAL // 2, image_cache_root=tmp_path
        )

    result = restarted(timedelta(hours=23, minutes=59)).collect()
    assert result.installations == result.images == 0
    assert receipt.exists() and model.exists()

    collector = restarted(timedelta(hours=24, seconds=1))
    assert collector._images is not None
    remove = collector._images.remove_published

    def fail_unlink(_archive: str) -> int:
        raise OSError("injected storage fault")

    monkeypatch.setattr(collector._images, "remove_published", fail_unlink)
    result = collector.collect()
    assert result.installations == 1 and result.images == 0
    assert lifecycle.removed == [idle]
    assert receipt.exists() and model.exists()
    with world.sessions() as session:
        from vonk_control.models import RecipeInstallation

        row = session.get(RecipeInstallation, live)
        assert row is not None and row.state == InstallationState.INSTALLED

    # A fresh collector has no retry memory; disk and SQL remain the authority.
    monkeypatch.setattr(collector._images, "remove_published", remove)
    result = restarted(timedelta(hours=24, minutes=1)).collect()
    assert result.images == 1 and result.models == 0
    assert not receipt.exists() and model.exists()
    assert lifecycle.removed == [idle]


def test_age_collection_keeps_the_real_nas_model_cache(world: Catalog, cached) -> None:
    """Catches accidentally applying runtime expiry to model cache objects."""
    from .test_unused_storage_collection import _models, _set_exists

    service, digest = cached
    result = _models(world, service, free=TOTAL // 2).collect()
    assert result.models == 0 and _set_exists(world, digest)


def test_age_uninstall_binds_model_retention_in_real_queue(
    tmp_path: Path, postgres_engine
) -> None:
    """Catches preview retaining models while acceptance still asks the agent to erase them."""
    from sqlalchemy import select
    from vonk_agent_protocol import AgentOperation as Operation
    from vonk_control.models import AgentOperation
    from vonk_control.unused_storage_collection import UnusedStorageCollector

    from .test_unused_storage_collection import (
        _REAL_CLOCK,
        _real_lifecycle,
        _report_disk,
    )

    sessions, service, installation_id, nodes = _real_lifecycle(
        tmp_path, engine=postgres_engine
    )
    for node in nodes:
        _report_disk(sessions, TOTAL // 2, at=_REAL_CLOCK, node=node)
    collector = UnusedStorageCollector(
        sessions, clock=lambda: _REAL_CLOCK, lifecycle=service
    )
    assert collector.collect().installations == 1
    with sessions() as session:
        operations = session.scalars(
            select(AgentOperation).where(
                AgentOperation.kind == Operation.RECIPE_UNINSTALL
            )
        ).all()
        assert operations
        for operation in operations:
            assert operation.payload["installation_id"] == installation_id
            assert operation.payload.get("cleanup_model_content_sha256") is None
