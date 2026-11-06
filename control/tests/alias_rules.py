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
    "run alias compared by hand; use profile_capacity.serving_run for the run "
    "serving an assignment"
)
#: The run named by a comparison side: ``run.alias``, ``RecipeRun.alias``.
_RUN_NAME = re.compile(r"(?i)run$")
#: The one predicate, and the sites that compare a run with the alias its own
#: stored plan, stop or request-key replay recorded (authority checks, not the
#: question "which run serves this assignment").
RUN_ALIAS_SITES = frozenset(
    {
        "control/src/vonk_control/profile_capacity.py",
        "control/src/vonk_control/profile_stop_authority.py",
        "control/src/vonk_control/fleet_profiles/switch_dispatch.py",
        "control/src/vonk_control/distributed_recovery/singleton.py",
        "control/src/vonk_control/distributed_recovery/authority.py",
        "control/src/vonk_control/recipe_operations/persistence.py",
        "control/src/vonk_control/recipe_operations/run_observation.py",
    }
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
                and _RUN_NAME.search(_name(side.value))
            ):
                hits.add(node.lineno)
        # ``RecipeRun.alias == x`` inside a query filter is a Compare as well.
    return sorted(hits)
