"""Hermetic regressions for range recovery and package content admission."""

from dataclasses import replace
from threading import Event
from typing import Never

import httpx2
import pytest
from vonk_agent_protocol import InvalidRequestError
from vonk_control.catalog_sync_contract import (
    CatalogSyncTrigger,
    ManagedCatalogSyncRequest,
)
from vonk_control.model_cache_ranges import download_ranges, range_partial_bytes
from vonk_control.recipe_library_types import RecipeLibrarySnapshot
from vonk_control.recipe_packages import RecipePackageClient, RecipePackageError


def _unused_job_operations(*_args: object) -> Never:
    raise AssertionError("optional global projection must not read per-job operations")


def test_short_range_is_reobserved_without_a_new_download(tmp_path):
    target = tmp_path / "model.part"
    requests = []
    data = b"0123456789"

    def open_range(start, end):
        requests.append((start, end))
        body = data[:3] if len(requests) == 1 else data[start : end + 1]
        return httpx2.Response(
            206,
            content=body,
            headers={"content-range": f"bytes {start}-{end}/{len(data)}"},
            request=httpx2.Request("GET", "https://example.com/model"),
        )

    assert download_ranges(
        target, len(data), open_range, Event(), lambda _: None, workers=1
    )
    assert requests == [(0, 9), (3, 9)]
    assert target.read_bytes() == data
    assert range_partial_bytes(target, len(data), workers=1) == len(data)


def test_package_preparation_uses_content_not_publication_commit(tmp_path):
    client = RecipePackageClient(cache_root=tmp_path)
    snapshot = RecipeLibrarySnapshot(commit="a" * 40, items=())
    client._snapshot = snapshot
    try:
        client.prepare(replace(snapshot, commit="b" * 40))
    finally:
        client.close()


def test_package_preparation_rejects_changed_content_with_same_commit(tmp_path):
    client = RecipePackageClient(cache_root=tmp_path)
    snapshot = RecipeLibrarySnapshot(commit="a" * 40, items=())
    client._snapshot = snapshot
    try:
        with pytest.raises(RecipePackageError):
            client.prepare(replace(snapshot, catalog_entities=({"changed": True},)))
        client.prepare(snapshot)
    finally:
        client.close()


def test_activity_read_without_optional_projections_is_empty():
    from vonk_control.operation_api.contracts import OperationApiServices
    from vonk_control.operation_api.providers import _global_list_operations

    services = OperationApiServices(
        agents=lambda: (),
        job_operations=_unused_job_operations,
        resume_job=lambda _: None,
    )
    page = _global_list_operations(services, None, 10, None, None, None)
    assert page.items == ()
    assert page.total == 0
    assert page.next_cursor is None


def test_activity_absent_projection_is_not_a_read_failure():
    from vonk_control.operation_api.contracts import OperationApiServices
    from vonk_control.operation_api.providers import _global_get_operation

    services = OperationApiServices(
        agents=lambda: (),
        job_operations=_unused_job_operations,
        resume_job=lambda _: None,
    )
    with pytest.raises(KeyError):
        _global_get_operation(services, "missing")


def test_cache_pagination_uses_authenticated_observation_cursors():
    from unittest.mock import Mock

    from vonk_control.auth import CursorCodec, CursorError
    from vonk_control.model_cache import ModelCacheService
    from vonk_control.model_cache_api import (
        ModelCacheOperationProvider,
    )

    provider = ModelCacheOperationProvider(Mock(spec=ModelCacheService))
    assert (
        provider._next_cursor(None, state=None, node_id=None, request_id=None) is None
    )
    boundary = ("2026-10-08T00:00:00+00:00", "operation")
    assert (
        provider._next_cursor(boundary, state=None, node_id=None, request_id=None)
        is not None
    )
    assert (
        provider._next_cursor(("damaged",), state=None, node_id=None, request_id=None)
        is None
    )
    codec = CursorCodec(b"x" * 32)
    provider = ModelCacheOperationProvider(Mock(spec=ModelCacheService), codec)
    cursor = provider._next_cursor(boundary, state=None, node_id=None, request_id=None)
    assert cursor is not None
    assert codec.decode(
        cursor,
        resource="model-cache-operations",
        order="created-at-desc/id-desc/v1",
        context={"state": None, "node_id": None, "request_id": None},
    ) == list(boundary)
    replacement = "A" if cursor[-1] != "A" else "B"
    with pytest.raises(CursorError):
        codec.decode(
            cursor[:-1] + replacement,
            resource="model-cache-operations",
            order="created-at-desc/id-desc/v1",
            context={"state": None, "node_id": None, "request_id": None},
        )
    assert codec.decode(
        cursor,
        resource="model-cache-operations",
        order="created-at-desc/id-desc/v1",
        context={"state": None, "node_id": None, "request_id": None},
    ) == list(boundary)


def test_incomplete_cached_build_enters_normal_preparation():
    from vonk_control.availability_production import _cached_build_receipt
    from vonk_control.recipe_builds import RecipeBuildResolution

    resolution = RecipeBuildResolution(
        recipe_revision_id="revision",
        recipe_content_sha256="a" * 64,
        source_bundle_sha256="b" * 64,
        input_intent_sha256="c" * 64,
        input_intent={},
        build_id="damaged-receipt",
    )
    assert _cached_build_receipt(resolution) is None


def test_unmatched_active_handoff_is_named_and_preserves_owners(monkeypatch):
    from unittest.mock import Mock

    from sqlalchemy.orm import Session
    from vonk_agent_protocol import (
        ReservationState,
        UnknownOutcomeError,
    )
    from vonk_control import profile_capacity
    from vonk_control.models import ResourceReservation

    claims = {
        node: ResourceReservation(
            node_id=node,
            owner_kind="installation",
            owner_id=owner,
            state=ReservationState.ACTIVE,
            amount_bytes=10,
            plan_digest="a" * 64,
        )
        for node, owner in (("spark-1", "one"), ("spark-2", "two"))
    }
    monkeypatch.setattr(
        profile_capacity,
        "_profile_disk_binding",
        lambda *_args, **_kwargs: (
            None,
            "assignment",
            {node: 10 for node in claims},
            claims,
        ),
    )
    with pytest.raises(UnknownOutcomeError):
        profile_capacity.prepared_profile_installation(
            Mock(spec=Session),
            "profile",
            "revision",
            tuple(claims),
            workload_intent_ordinal=1,
        )
    assert {claim.owner_id for claim in claims.values()} == {"one", "two"}
    assert all(claim.state == ReservationState.ACTIVE for claim in claims.values())


def test_unknown_catalog_sync_ends_and_admits_a_fresh_request(tmp_path):
    from datetime import UTC, datetime
    from unittest.mock import Mock

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker
    from vonk_agent_protocol import LifecycleState, SourceBundleCode
    from vonk_control.auth import CursorCodec
    from vonk_control.catalog_service import CatalogService
    from vonk_control.catalog_sync import (
        ManagedRecipeCatalogSyncService,
        RecipeLibrarySyncReader,
    )
    from vonk_control.models import Base, RecipeLibrarySyncRun
    from vonk_control.source_bundles import SourceBundleUnknown

    from .non_blocking import assert_ended_without_blocking

    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    clock = lambda: datetime(2026, 10, 8, tzinfo=UTC)
    catalog = CatalogService(sessions, clock=clock, cursors=CursorCodec(b"x" * 32))
    reader = Mock(spec=RecipeLibrarySyncReader)
    reader.list.side_effect = [
        SourceBundleUnknown(
            SourceBundleCode.STORAGE_UNAVAILABLE, "source temporarily absent"
        ),
        RecipeLibrarySnapshot(commit="a" * 40, items=()),
    ]
    sync = ManagedRecipeCatalogSyncService(
        sessions, catalog=catalog, reader=reader, clock=clock
    )

    def request(key: str) -> RecipeLibrarySyncRun:
        result = sync.sync(
            ManagedCatalogSyncRequest(
                request_key=key, trigger=CatalogSyncTrigger.MANUAL, actor="test"
            )
        )
        with sessions() as session:
            row = session.get(RecipeLibrarySyncRun, result.id)
            assert row is not None
            return row

    def released():
        with sessions() as session:
            assert (
                session.scalar(
                    select(RecipeLibrarySyncRun).where(
                        RecipeLibrarySyncRun.active_slot.is_not(None)
                    )
                )
                is None
            )

    def reason(row: RecipeLibrarySyncRun):
        assert row.active_slot is None

    assert_ended_without_blocking(
        sessions,
        RecipeLibrarySyncRun(
            request_key="00000000-0000-4000-8000-000000000001",
            state=LifecycleState.RUNNING,
        ),
        end=lambda row: request(row.request_key),
        fresh=lambda _: request("00000000-0000-4000-8000-000000000002"),
        assert_released=released,
        assert_reason=reason,
    )


def test_unreadable_incoming_bundle_is_input_validation(monkeypatch):
    import tarfile

    from vonk_control.source_bundles import (
        generate_source_bundle,
        parse_source_bundle,
    )

    incoming = generate_source_bundle({"Dockerfile": b"FROM scratch\n"})
    monkeypatch.setattr(tarfile.TarFile, "extractfile", lambda *_: None)
    with pytest.raises(InvalidRequestError):
        parse_source_bundle(incoming.archive)


def test_catalog_source_storage_absence_recovers_on_the_next_request(tmp_path):
    import io
    from datetime import UTC, datetime

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from vonk_agent_protocol import UnknownOutcomeError
    from vonk_control.auth import CursorCodec
    from vonk_control.catalog_service import CatalogService
    from vonk_control.models import Base
    from vonk_control.source_bundles import SourceBundleStore, generate_source_bundle

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    catalog = CatalogService(
        sessions,
        clock=lambda: datetime(2026, 10, 8, tzinfo=UTC),
        cursors=CursorCodec(b"x" * 32),
    )
    incoming = generate_source_bundle({"Dockerfile": b"FROM scratch\n"})
    with pytest.raises(UnknownOutcomeError):
        catalog.store_source_bundle(
            incoming.sha256, io.BytesIO(incoming.archive), "test"
        )
    catalog._source_bundles = SourceBundleStore(tmp_path / "bundles")
    receipt = catalog.store_source_bundle(
        incoming.sha256, io.BytesIO(incoming.archive), "test"
    )
    assert receipt.sha256 == incoming.sha256


def test_unreadable_stored_bundle_remains_unknown(monkeypatch):
    import tarfile

    from vonk_control.source_bundles import (
        BundleLimits,
        SourceBundleUnknown,
        _generated_bundle,
        generate_source_bundle,
    )

    stored = generate_source_bundle({"Dockerfile": b"FROM scratch\n"})
    monkeypatch.setattr(tarfile.TarFile, "extractfile", lambda *_: None)
    with pytest.raises(SourceBundleUnknown):
        _generated_bundle(stored.archive, stored.manifest, BundleLimits())


@pytest.mark.parametrize(
    "payload",
    [
        {"runtime": None},
        {"runtime": {"builder_node_id": 1}, "build_input_sha256": "a" * 64},
    ],
)
def test_invalid_availability_dependency_is_pre_effect_input(payload):
    from unittest.mock import Mock

    from sqlalchemy.orm import Session
    from vonk_control.recipe_build_cancellation import (
        lock_availability_build_dependency,
    )

    session = Mock(spec=Session)
    with pytest.raises(InvalidRequestError):
        lock_availability_build_dependency(session, payload)
    assert session.mock_calls == []
