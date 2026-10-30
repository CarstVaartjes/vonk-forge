"""A stored value is parsed once per function (P18, validate once).

Parsing the same row twice in one function validates trusted internal data
again. Added lines may not call one stored-row parser twice on the same
expression in the same function, or parse a column that ``read_row_column``
just parsed.
"""

from __future__ import annotations

import ast

PARSERS = frozenset(
    {
        "read_recipe",
        "read_model",
        "_stored_job_plan",
        "_run_switch_payload",
        "_persisted_profile_plan",
        "resolve_recipe_entities",
    }
)
STORED_COLUMNS = frozenset({"plan", "payload", "document"})
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
# A new binding of the loop or comprehension variable is a new value.
_REBINDING = (
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.Lambda,
    *_FUNCTIONS,
)
_EXCLUSIVE = (ast.If,)


def _name(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _parser(call: ast.Call) -> str | None:
    """The parser a call is, with the part of the column it reads."""
    name = _name(call)
    if name in PARSERS or name.startswith("parse_stored_"):
        return name
    if name == "read_row_column" and len(call.args) >= 2:
        column = call.args[1]
        if isinstance(column, ast.Constant) and column.value in STORED_COLUMNS:
            return name
    return None


def _calls(function: ast.AST) -> list[ast.Call]:
    """Parser calls of this function, not of the functions defined inside it."""
    found: list[ast.Call] = []
    pending = list(ast.iter_child_nodes(function))
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Lambda | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if isinstance(node, ast.Call) and _parser(node):
            found.append(node)
        pending.extend(ast.iter_child_nodes(node))
    return sorted(found, key=lambda call: (call.lineno, call.col_offset))


def _path(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> list[tuple[ast.AST, str]]:
    """The (ancestor, field) steps from the function down to ``node``."""
    steps: list[tuple[ast.AST, str]] = []
    while node in parents:
        parent = parents[node]
        for field, value in ast.iter_fields(parent):
            if value is node or (isinstance(value, list) and node in value):
                steps.append((parent, field))
                break
        node = parent
    return steps[::-1]


def _same_execution(
    first: ast.Call, second: ast.Call, parents: dict[ast.AST, ast.AST]
) -> bool:
    """Both calls can run for one value: one iteration, and not exclusive arms."""
    one, other = _path(first, parents), _path(second, parents)
    scope = [step[0] for step in one if isinstance(step[0], _REBINDING)]
    if scope != [step[0] for step in other if isinstance(step[0], _REBINDING)]:
        return False
    for left, right in zip(one, other, strict=False):
        if left == right:
            continue
        return not (left[0] is right[0] and isinstance(left[0], _EXCLUSIVE))
    return True


def scan_source(source: str) -> list[int]:
    """Lines of the calls that parse one stored value twice in one function."""
    tree = ast.parse(source)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    lines: set[int] = set()
    for function in ast.walk(tree):
        if not isinstance(function, _FUNCTIONS):
            continue
        calls = _calls(function)
        for index, call in enumerate(calls):
            inner = call.args[0] if call.args else None
            if (
                _name(call).startswith("parse_stored_")
                and isinstance(inner, ast.Call)
                and _name(inner) == "read_row_column"
            ):
                lines.add(call.lineno)
            for other in calls[index + 1 :]:
                if (
                    _parser(call) == _parser(other)
                    and call.args
                    and list(map(ast.dump, call.args))
                    == list(map(ast.dump, other.args))
                    and _same_execution(call, other, parents)
                ):
                    lines.update({call.lineno, other.lineno})
    return sorted(lines)
