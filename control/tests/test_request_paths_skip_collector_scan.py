"""Request paths never pay for the collector's 24-hour scan."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from vonk_agent_protocol.inventory import ModelStoreObject
from vonk_control import catalog_revision_collection
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import Base
from vonk_control.spark_store_retention import store_retention_request
from vonk_control.unused_storage_collection import evidence as collector_evidence
from vonk_control.unused_storage_collection import spark_eviction_capacity

from .test_catalog_revision_collection import NODE, Catalog
from .test_fleet_model_removal_postgres import inventory
from .test_model_removal_worker_postgres import _prepare_removal_target


def test_fleet_review_accept_inventory_and_load_review_skip_the_24_hour_scan(
    postgres_engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches `_Evidence.read` replaying jobs, applications and operations for
    a request: fleet review/accept, the inventory POST and a profile-load
    review need the references only, never ``live``."""
    Base.metadata.create_all(postgres_engine)
    world = Catalog(postgres_engine)
    now = datetime.now(UTC)
    cache = ModelCacheService(
        world.sessions,
        tmp_path / "nas",
        free_fraction=0,
        fixture_sources=True,
        clock=lambda: now,
    )

    def scan(*_args: object, **_kwargs: object) -> frozenset[str]:
        raise AssertionError("a request path replayed the 24-hour live scan")

    try:
        selector, _digest, path = _prepare_removal_target(
            cache, world.sessions, tmp_path, slug="request-path", data=b"weights"
        )
        obj = ModelStoreObject(sha256=path.name, bytes=path.stat().st_size)
        inventory(world.sessions, NODE, now, [obj])
        monkeypatch.setattr(catalog_revision_collection, "live_tokens", scan)
        monkeypatch.setattr(collector_evidence, "live_tokens", scan)
        review = cache.review_fleet_model_removal(selector)
        assert review.fleet is not None and review.fleet.nas_remove
        accepted = cache.remove_fleet_model_selector(
            selector,
            actor="operator",
            request_key=str(uuid4()),
            review_digest=review.review_digest,
        )
        assert accepted.id
        with world.sessions() as session:
            request = store_retention_request(session, NODE, now)
            assert request is not None
            spark_eviction_capacity(session, NODE, now)
    finally:
        cache.close()
