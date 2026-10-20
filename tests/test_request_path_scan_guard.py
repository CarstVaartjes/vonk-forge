"""Class E scan guard fixtures, including indirect query construction."""

import ast

import pytest

from control.tests.request_path_scans import scan_tree


@pytest.mark.parametrize(
    "query",
    [
        "session.scalars(select(CatalogDocumentRevision))",
        "self._catalog_documents(kind=kind, local_digests=(), reader=reader)",
        "session.execute(select(ModelCacheOperation.id).where(ModelCacheOperation.state == state))",
        "query = select(ModelCacheOperation)\n query = query.where(ModelCacheOperation.kind == kind)\n return session.scalars(query)",
    ],
)
def test_unbounded_request_history_scan_is_rejected(query):
    """A state/kind filter must not disguise a full history materialization."""
    assert scan_tree(
        ast.parse(f"def detail(session, state, kind):\n {query}\n"),
        path="control/src/vonk_control/library_api.py",
    )


@pytest.mark.parametrize(
    "query",
    [
        "session.scalars(select(CatalogDocumentRevision).where(CatalogDocumentRevision.content_digest == digest))",
        "session.execute(select(ModelCacheOperation).where(ModelCacheOperation.id.in_(ids)))",
        "session.scalars(select(CatalogDocumentRevision).limit(limit))",
        "query = select(CatalogDocumentRevision)\n query = query.where(CatalogDocumentRevision.slug == selector)\n return session.scalars(query)",
    ],
)
def test_scoped_or_bounded_request_read_is_permitted(query):
    assert not scan_tree(
        ast.parse(f"def detail(session, digest, ids, limit, selector):\n {query}\n"),
        path="control/src/vonk_control/library_api.py",
    )


def test_library_request_queries_are_scoped():
    """Whole-reader mutation guard: a new materialization needs a key or page."""
    from pathlib import Path

    from control.tests.parsed_sources import parse_file

    root = Path(__file__).resolve().parents[1]
    errors = []
    for path in (root / "control/src/vonk_control/library_projection").glob("*.py"):
        relative = path.relative_to(root).as_posix()
        errors.extend(
            f"{relative}:{line}"
            for line in scan_tree(parse_file(path).tree, path=relative)
        )
    assert not errors, "\n".join(errors)
