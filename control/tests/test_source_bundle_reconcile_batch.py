"""Source bundle reconcile cost does not grow with the number of revisions.

Every rotation checks each active recipe revision's source. One query names the
digests, one asks the store which are missing, and only a missing digest is healed,
once per run, however many revisions share it.
"""

from __future__ import annotations

import io
from pathlib import Path

from sqlalchemy import event
from vonk_control.models import CatalogDocumentRevision
from vonk_control.recipe_builds import RecipeBuildService

from .test_recipe_builds import setup


def _clone_revisions(sessions, template_id: str, start: int, stop: int) -> None:
    with sessions.begin() as session:
        template = session.get(CatalogDocumentRevision, template_id)
        assert template is not None
        for number in range(start, stop):
            clone = CatalogDocumentRevision(
                **{
                    column.key: getattr(template, column.key)
                    for column in CatalogDocumentRevision.__table__.columns
                    if column.key not in {"id", "revision_number", "content_digest"}
                },
                revision_number=100 + number,
                content_digest=f"{number:064x}",
            )
            session.add(clone)


def _count_queries(engine, call) -> int:
    statements: list[str] = []

    def record(_c, _cur, statement, *_rest) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        call()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return len(statements)


def _service(sessions, bundles, rederive) -> RecipeBuildService:
    service = RecipeBuildService(sessions, bundles=bundles, source_rederiver=rederive)
    service._source_check_at = 0.0
    return service


def test_present_sources_cost_one_query_regardless_of_revision_count(
    tmp_path: Path,
) -> None:
    """Catches a per-revision document load, projection read or manifest lookup."""
    sessions, bundles, _now, _node, revision = setup(tmp_path)
    service = _service(sessions, bundles, lambda *_: None)
    costs = []
    for start, stop in ((0, 2), (2, 10)):
        _clone_revisions(sessions, revision.id, start, stop)
        service._source_check_at = 0.0
        costs.append(
            _count_queries(sessions.kw["bind"], service.reconcile_source_bundles)
        )
    assert costs == [1, 1]


def test_a_failing_heal_is_attempted_once_per_digest(tmp_path: Path) -> None:
    """Catches a failed digest retried for every revision sharing it."""
    sessions, bundles, _now, _node, revision = setup(tmp_path)
    sha = str(revision.projected["source_bundle_sha256"])
    bundles.put(sha, io.BytesIO(bundles.get(sha).archive)).path.unlink()
    _clone_revisions(sessions, revision.id, 0, 10)
    attempts: list[str] = []

    def rederive(_projected, _path, digest):
        attempts.append(digest)

    service = _service(sessions, bundles, rederive)
    queries = _count_queries(sessions.kw["bind"], service.reconcile_source_bundles)
    assert attempts == [sha]
    assert queries == 2


def test_database_store_cost_is_independent_of_distinct_digests(
    postgres_engine,
) -> None:
    """Catches one session and one join query per digest against the bundle store."""
    from datetime import UTC, datetime

    from sqlalchemy.orm import sessionmaker
    from vonk_control.models import Base, CatalogDocument
    from vonk_control.source_bundles import (
        DatabaseSourceBundleStore,
        generate_source_bundle,
    )

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    store = DatabaseSourceBundleStore(sessions)
    document_id = "d" * 36
    with sessions.begin() as session:
        session.add(
            CatalogDocument(
                id=document_id,
                kind="recipe",
                publisher="pp",
                slug="ss",
                title="t",
                created_by="test",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )

    def seed(start: int, stop: int) -> None:
        for number in range(start, stop):
            bundle = generate_source_bundle({"f": str(number).encode()})
            store.put(bundle.sha256, io.BytesIO(bundle.archive))
            with sessions.begin() as session:
                session.add(
                    CatalogDocumentRevision(
                        document_id=document_id,
                        kind="recipe",
                        publisher="pp",
                        slug="ss",
                        revision_number=number + 1,
                        state="active",
                        document={},
                        content_digest=f"{number + 1:064x}",
                        projected={"source_bundle_sha256": bundle.sha256},
                        created_by="test",
                        created_at=datetime.now(UTC),
                    )
                )

    service = _service(sessions, store, lambda *_: None)
    counts = []
    for start, stop in ((0, 5), (2, 10)):
        seed(start, stop)
        service._source_check_at = 0.0
        counts.append(_count_queries(postgres_engine, service.reconcile_source_bundles))
    assert counts == [2, 2]
