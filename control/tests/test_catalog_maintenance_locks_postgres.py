"""Catalog maintenance holds no catalog row lock while it checks revisions.

The automatic recipe sync re-checks every active recipe revision each minute.
Admission locks those rows ``FOR UPDATE``; a check that held them for its whole
pass kept admission busy.  Each check here is paused mid-pass while another
session locks the row it is about to write with ``NOWAIT``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest
import vonk_control.catalog_entities as entities
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import CursorCodec
from vonk_control.catalog_revision_contract import (
    PrebuiltImage,
    RecipeRevisionProjection,
    read_catalog_projection,
)
from vonk_control.catalog_service import CatalogService
from vonk_control.models import Base, CatalogDocumentRevision
from vonk_forge_contracts import RecipeDefinition

from .test_catalog_entities import _model, _recipe
from .test_catalog_import_recovery import NOW, _cores, _projection, _request_import

_WAIT_SECONDS = 30


@pytest.fixture
def catalog(postgres_engine: Engine) -> CatalogService:
    Base.metadata.create_all(postgres_engine)
    return CatalogService(
        sessionmaker(postgres_engine, expire_on_commit=False),
        clock=lambda: NOW,
        cursors=CursorCodec(b"c" * 32),
    )


@contextmanager
def _paused_check(
    monkeypatch: pytest.MonkeyPatch, name: str, run: Callable[[], object]
) -> Iterator[None]:
    """Run ``run`` in a thread, paused inside its first call of ``name``."""

    check = getattr(entities, name)
    paused, resume = threading.Event(), threading.Event()
    failures: list[BaseException] = []

    def paused_check(*args: object) -> object:
        if not paused.is_set():
            paused.set()
            assert resume.wait(_WAIT_SECONDS)
        return check(*args)

    def target() -> None:
        try:
            run()
        except BaseException as error:  # noqa: BLE001 - reported by the test
            failures.append(error)

    monkeypatch.setattr(entities, name, paused_check)
    worker = threading.Thread(target=target)
    worker.start()
    try:
        assert paused.wait(_WAIT_SECONDS)
        yield
    finally:
        resume.set()
        worker.join(_WAIT_SECONDS)
    assert not worker.is_alive()
    assert failures == []


def _lock_now(catalog: CatalogService, revision_id: str) -> None:
    with catalog._sessions.begin() as session:
        locked = session.scalar(
            select(CatalogDocumentRevision.id)
            .where(CatalogDocumentRevision.id == revision_id)
            .with_for_update(nowait=True)
        )
    assert locked == revision_id


def test_build_policy_refresh_checks_without_holding_row_locks(
    catalog: CatalogService, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = _request_import(catalog, _recipe(_model()))
    with catalog._sessions.begin() as session:
        projection = _projection(session, view.id)
        assert projection.build_resources is not None
        stale = projection.build_resources.model_copy(update={"cpu_cores": 1})
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == view.id)
            .values(
                projected=projection.model_copy(
                    update={"build_resources": stale}
                ).model_dump(mode="json")
            )
        )

    with _paused_check(
        monkeypatch, "build_policy_projection", catalog.refresh_build_policy
    ):
        _lock_now(catalog, view.id)

    with catalog._sessions() as session:
        assert _cores(session, view.id) == 8


def test_prebuilt_image_record_checks_without_holding_row_locks(
    catalog: CatalogService, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = _request_import(catalog, _recipe(_model()))
    assert view.content_sha256 is not None
    key = (
        str(RecipeDefinition.model_validate(view.document).identity.publisher),
        view.slug,
        view.content_sha256,
    )
    image = PrebuiltImage(
        reference="ghcr.io/vonk/image@sha256:" + "1" * 64, build_key="2" * 64
    )

    with _paused_check(
        monkeypatch,
        "read_catalog_projection",
        lambda: catalog.record_prebuilt_images({key: image}),
    ):
        _lock_now(catalog, view.id)

    with catalog._sessions() as session:
        row = session.get(CatalogDocumentRevision, view.id)
        assert row is not None
        projection = read_catalog_projection(row)
        assert isinstance(projection, RecipeRevisionProjection)
        assert projection.prebuilt_image == image
