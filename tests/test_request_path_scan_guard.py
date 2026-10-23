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


def _server_cursors(tree):
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if (
            node.func.attr == "yield_per"
            or (
                node.func.attr == "cursor"
                and (node.args or any(key.arg == "name" for key in node.keywords))
            )
            or (
                node.func.attr == "execution_options"
                and any(
                    key.arg in {"yield_per", "stream_results"}
                    and not (
                        isinstance(key.value, ast.Constant)
                        and key.value.value in (None, False)
                    )
                    for key in node.keywords
                )
            )
        ):
            lines.append(node.lineno)
    return lines


@pytest.mark.parametrize(
    "query",
    [
        "query.yield_per(20)",
        "query.execution_options(yield_per=20)",
        "query.execution_options(stream_results=True)",
        "connection.cursor(name='reply')",
        "connection.cursor('reply')",
    ],
)
def test_api_cursor_guard_rejects_server_cursors(query):
    assert _server_cursors(ast.parse(query))
    assert not _server_cursors(
        ast.parse("query.execution_options(yield_per=None, stream_results=False)")
    )
    assert not _server_cursors(ast.parse("connection.cursor()"))


def test_controller_sessions_end_before_suspension_and_api_reads_buffer():
    """Guard explicit lifetimes; process/PG tests own indirect I/O boundaries."""
    from pathlib import Path

    from control.tests.coordination_boundaries import scan_coordination_sites
    from control.tests.parsed_sources import parse_file

    root = Path(__file__).resolve().parents[1]
    errors = []
    for path in (root / "control/src/vonk_control").rglob("*.py"):
        tree = parse_file(path).tree
        lines = []
        if {"api", "agent_api"} & set(path.parts) or path.name.endswith("_api.py"):
            lines.extend(_server_cursors(tree))
        errors.extend(f"{path.relative_to(root)}:{line}" for line in lines)
    errors.extend(
        site.render()
        for site in scan_coordination_sites()
        if site.detail.startswith("suspension ")
    )
    assert not errors, "SQL session/cursor survives a suspension:\n" + "\n".join(errors)


@pytest.mark.parametrize(
    "provider",
    [
        "_model_storage",
        "model_storage",
        "inventory",
        "reconcile_storage",
        "_stored_object",
        "lstat",
    ],
)
def test_library_storage_walks_are_rejected_on_added_lines(provider):
    """A new direct storage provider in a view must fail the patch gate."""
    from control.tests.added_line_guards import check_source

    path = "control/src/vonk_control/library_projection/views.py"
    source = f"def models(self):\n    return self.{provider}()\n"
    assert check_source(path, source, {2}, modes=("request-scans",))
    assert not check_source(path, source, {1}, modes=("request-scans",))
    assert not scan_tree(
        ast.parse(source),
        path="control/src/vonk_control/library_projection/catalog.py",
    )
