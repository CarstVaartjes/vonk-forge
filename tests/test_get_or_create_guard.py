"""Identity arbitration uses unique indexes, never explicit read locks."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from control.tests.parsed_sources import parsed_tree


def expanded(
    node: ast.AST,
    aliases: dict[str, list[ast.expr]],
    seen: frozenset[str] = frozenset(),
) -> list[ast.AST]:
    result = list(ast.walk(node))
    for child in tuple(result):
        if isinstance(child, ast.Name) and child.id in aliases and child.id not in seen:
            for value in aliases[child.id]:
                result.extend(expanded(value, aliases, seen | {child.id}))
    return result


def identity_locks(tree: ast.Module) -> list[int]:
    """Detect locked identity lookups in insert/replay helpers, without a ledger.

    State transitions, work claims and capacity admission are separate owners.
    The scan follows local query aliases; it does not prove arbitrary indirect
    SQL safe. Runtime namespace validation also rejects dynamic advisory keys.
    """
    errors = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
        inserts = any(
            isinstance(call.func, ast.Name) and call.func.id == "insert_identity"
            for call in calls
        )
        creates_job = any(
            (
                isinstance(call.func, ast.Attribute)
                and call.func.attr in {"new_job", "_insert_job"}
            )
            or (isinstance(call.func, ast.Name) and call.func.id == "new_recipe_job")
            for call in calls
        )
        constructed = {
            call.func.id
            for call in calls
            if isinstance(call.func, ast.Name) and call.func.id[:1].isupper()
        }
        aliases: dict[str, list[ast.expr]] = {}
        for node in ast.walk(function):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        aliases.setdefault(target.id, []).append(node.value)

        for call in calls:
            if not isinstance(call.func, ast.Attribute):
                continue
            row_lock = call.func.attr == "with_for_update" or any(
                item.arg == "with_for_update"
                and not (
                    isinstance(item.value, ast.Constant) and item.value.value is False
                )
                for item in call.keywords
            )
            if not row_lock:
                continue
            nodes = expanded(call, aliases)
            columns = {
                (node.value.id, node.attr)
                for node in nodes
                if isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id[:1].isupper()
            }
            models = {name for name, _ in columns}
            key_lookup = any(
                field in {"request_id", "request_key", "content_digest", "sha256"}
                for _, field in columns
            ) or {"publisher", "slug"} <= {field for _, field in columns}
            if (
                key_lookup
                and (creates_job or bool(models & constructed))
                or inserts
                and models & constructed
                or inserts
                and call.func.attr == "get"
                and any(
                    isinstance(arg, ast.Name) and arg.id in constructed
                    for arg in call.args
                )
            ):
                errors.append(call.lineno)
        if function.name == "insert_identity":
            errors.extend(
                call.lineno
                for call in calls
                if isinstance(call.func, ast.Attribute)
                and (
                    call.func.attr == "with_for_update" or "advisory" in call.func.attr
                )
            )
    return sorted(set(errors))


@pytest.mark.parametrize(
    "query",
    [
        "select(Job).where(Job.request_id == key).with_for_update()",
        "query = select(Job).where(Job.request_id == key)\n    query = query.with_for_update()",
        "select(Document).where(Document.publisher == publisher, Document.slug == slug).with_for_update()",
        "session.get(Document, identity, with_for_update=True)",
    ],
)
def test_identity_lock_regressions_are_rejected(query: str):
    source = f"""def create(session, key, publisher, slug, identity):
    {query}
    candidate = Document()
    new_recipe_job()
    insert_identity(session, candidate, key=("id",))
"""
    assert identity_locks(ast.parse(source))


def test_mutation_and_capacity_locks_are_not_identity_arbitration():
    assert not identity_locks(
        ast.parse("""
def create(session):
    session.scalar(select(Node).where(Node.node_id == node).with_for_update())
    job = new_recipe_job()
    insert_identity(session, job, key=("request_id",))
def revoke(session):
    session.scalar(select(Assignment).where(Assignment.plan_digest == plan).with_for_update())
""")
    )


def test_get_or_create_identity_reads_never_lock():
    root = Path(__file__).resolve().parents[1] / "control/src/vonk_control"
    failures = [
        f"{path.relative_to(root)}:{line}"
        for path, parsed in parsed_tree(root)
        for line in identity_locks(parsed.tree)
    ]
    assert not failures, "\n".join(failures)
