"""Find code that checks an endpoint alias with its own rule.

``vonk_forge_contracts.endpoint_alias`` owns the alias rule. A profile accepted
128-character aliases while the run plan, endpoint contract, discovery query
and route candidate each kept a 63-character copy: the profile saved, then the
load could not be planned and its endpoint never published.

Model fields and HTTP parameters are checked by behaviour in
``test_endpoint_alias_rule``. This scan covers code: a regex match on an
alias-named value whose regex is not the contract pattern (however its
constant is named), a ``len(alias)`` compared with a number, and a
``pattern=`` / ``max_length=`` on an alias-named parameter that does not name
the contract constant.
"""

from __future__ import annotations

import ast
import re

KIND = "alias checked with its own rule; use vonk_forge_contracts EndpointAlias"
ALIAS_NAME = re.compile(r"(?:^|_)alias(?:es)?$")

RUN_ALIAS_KIND = (
    "run alias compared by hand; use run_route_name.carries_alias for the name "
    "a run is routed under"
)
RUN_ALIAS_WRITE_KIND = (
    "run alias written outside the rename module; a live run's route name "
    "changes only through run_route_name.rename_run"
)
#: The name is routing metadata. The rename module owns reading and changing it
#: on a live run; a run is created with its name only in run admission.
RUN_ALIAS_SITES = frozenset({"control/src/vonk_control/run_route_name.py"})
RUN_CREATION_SITES = frozenset({"control/src/vonk_control/run_admission.py"})
#: The run named by a comparison side: ``run.alias``, ``RecipeRun.alias``.
_RUN_NAME = re.compile(r"(?i)run$")
#: Receivers whose ``.alias`` is the requested name of a start or an assignment,
#: not the name a live run is routed under.
_REQUEST_NAME = re.compile(
    r"(?i)(assignment|plan|request|effect|stop|expected|child|intent|revision|"
    r"endpoint|payload|body|lane|self|cls)s?$"
)


def in_scope(path: str) -> bool:
    return path.endswith(".py") and path.startswith(("control/src/", "src/"))


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _name(node.value)
    return ""


def _contract_regexes(tree: ast.Module) -> set[str]:
    """Module names bound to ``re.compile(ENDPOINT_ALIAS_PATTERN)``."""
    names: set[str] = set()
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and _name(node.value.func) == "compile"
            and node.value.args
            and _name(node.value.args[0]) == "ENDPOINT_ALIAS_PATTERN"
        ):
            names.update(_name(target) for target in node.targets)
    return names


def scan_source(source: str) -> list[int]:
    """Lines that check an alias in code with a rule other than the contract's."""
    tree = ast.parse(source)
    contract = _contract_regexes(tree)
    hits: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"fullmatch", "match", "search"}:
                module_call = _name(node.func.value) == "re"
                checked = node.args[1:] if module_call else node.args[:1]
                if any(ALIAS_NAME.search(_name(item)) for item in checked):
                    rule = node.args[0] if module_call else node.func.value
                    if _name(rule) not in contract | {"ENDPOINT_ALIAS_PATTERN"}:
                        hits.add(node.lineno)
        elif isinstance(node, ast.Compare):
            sides = [node.left, *node.comparators]
            lengths = [
                side
                for side in sides
                if isinstance(side, ast.Call)
                and _name(side.func) == "len"
                and side.args
                and ALIAS_NAME.search(_name(side.args[0]))
            ]
            if lengths and any(
                isinstance(side, ast.Constant) and isinstance(side.value, int)
                for side in sides
            ):
                hits.add(node.lineno)
        elif isinstance(node, ast.arguments):
            positional = [*node.posonlyargs, *node.args]
            defaults: dict[int, ast.expr | None] = dict(
                zip(
                    (
                        id(item)
                        for item in positional[len(positional) - len(node.defaults) :]
                    ),
                    node.defaults,
                    strict=True,
                )
            )
            defaults.update(
                zip(
                    (id(item) for item in node.kwonlyargs),
                    node.kw_defaults,
                    strict=True,
                )
            )
            for parameter in [*positional, *node.kwonlyargs]:
                if not ALIAS_NAME.search(parameter.arg):
                    continue
                for part in (parameter.annotation, defaults.get(id(parameter))):
                    if part is not None:
                        hits.update(_hand_rules(part))
    return sorted(hits)


def _hand_rules(node: ast.AST) -> list[int]:
    """``pattern=`` / ``max_length=`` keywords that do not name the contract."""
    return [
        keyword.value.lineno
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        for keyword in child.keywords
        if keyword.arg in {"pattern", "max_length"}
        and _name(keyword.value)
        not in {"ENDPOINT_ALIAS_PATTERN", "ENDPOINT_ALIAS_MAX_LENGTH"}
    ]


def run_alias_in_scope(path: str) -> bool:
    return (
        path.endswith(".py")
        and path.startswith("control/src/vonk_control/")
        and path not in RUN_ALIAS_SITES
    )


def scan_run_alias(source: str) -> list[int]:
    """Lines that compare a run's alias with ``==`` / ``!=``."""
    hits: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Compare) or not any(
            isinstance(op, ast.Eq | ast.NotEq) for op in node.ops
        ):
            continue
        for side in (node.left, *node.comparators):
            if (
                isinstance(side, ast.Attribute)
                and side.attr == "alias"
                and not _REQUEST_NAME.search(_name(side.value))
            ):
                hits.add(node.lineno)
        # ``RecipeRun.alias == x`` inside a query filter is a Compare as well.
    return sorted(hits)


def run_alias_write_in_scope(path: str) -> bool:
    return (
        path.endswith(".py")
        and path.startswith("control/src/vonk_control/")
        and path not in RUN_ALIAS_SITES
    )


def _writes_run(node: ast.AST) -> bool:
    """``update(RecipeRun)...`` or ``insert(RecipeRun)...`` down a call chain."""
    while isinstance(node, ast.Call | ast.Attribute):
        if (
            isinstance(node, ast.Call)
            and _name(node.func) in {"update", "insert"}
            and node.args
            and _name(node.args[0]) == "RecipeRun"
        ):
            return True
        node = node.func if isinstance(node, ast.Call) else node.value
    return False


def _names_alias(call: ast.Call) -> bool:
    """``alias=…`` or a ``{"alias": …}`` mapping among the call's arguments."""
    if any(keyword.arg == "alias" for keyword in call.keywords):
        return True
    return any(
        isinstance(key, ast.Constant) and key.value == "alias"
        for argument in call.args
        if isinstance(argument, ast.Dict)
        for key in argument.keys
    )


def scan_run_alias_write(source: str, path: str) -> list[int]:
    """Lines that write a run's alias: assignment, setattr, bulk write, construction.

    Any receiver counts: the name of the variable holding the run proves nothing.
    """
    hits: set[int] = set()
    creates = path in RUN_CREATION_SITES
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "alias":
                    hits.add(node.lineno)
        elif isinstance(node, ast.Call):
            if (
                _name(node.func) == "setattr"
                and len(node.args) > 1
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "alias"
            ):
                hits.add(node.lineno)
            elif not _names_alias(node):
                continue
            elif (
                _name(node.func) == "RecipeRun"
                and not creates
                or _name(node.func) == "values"
                and _writes_run(node.func)
            ):
                hits.add(node.lineno)
    return sorted(hits)
