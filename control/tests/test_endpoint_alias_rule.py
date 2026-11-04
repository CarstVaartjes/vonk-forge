"""Every endpoint alias in the Controller and vonkctl follows the one contract rule.

``vonk_forge_contracts.endpoint_alias`` owns the rule. A profile accepted
128-character aliases while the run plan, endpoint contract, discovery query
and route candidate each kept a 63-character copy: the profile saved, then the
load could not be planned and its endpoint never published. These nets find
every alias slot by what it carries, not by a literal, and check how it behaves.
"""

from __future__ import annotations

import ast
import importlib
import json
import pkgutil
import re
import subprocess
import types
from pathlib import Path
from typing import Annotated, Any, Union, get_args, get_origin

import pytest
import vonk_control
from pydantic import BaseModel, TypeAdapter, ValidationError
from vonk_control.contract_graph import schema_application
from vonk_control.gateway_keys import GatewayKeyCreateRequest
from vonk_control.litellm import LiteLlmModelEntry
from vonk_control.route_bundle_contract import (
    RouteBundleDocument,
    RouteIdentityDocument,
)
from vonk_forge_contracts import ENDPOINT_ALIAS_PATTERN, EndpointAlias
from vonk_forge_contracts.endpoint_alias import ENDPOINT_ALIAS_MAX_LENGTH

import cluster_profiles
from cluster_profiles.qualification_fixtures import contracts as fixture_contracts

ROOT = Path(__file__).resolve().parents[2]
ACCEPTED = ("a", "chat-" + "x" * (ENDPOINT_ALIAS_MAX_LENGTH - len("chat-")))
REFUSED = (
    "",
    "Chat",
    "chat-",
    "-chat",
    "chat/x",
    "x" * (ENDPOINT_ALIAS_MAX_LENGTH + 1),
)
ALIAS_NAME = re.compile(r"(?:^|_)alias(?:es)?$")
# Alias slots whose field name does not say alias: (model, field, part).
CARRIERS: tuple[tuple[type[BaseModel], str, str], ...] = (
    (LiteLlmModelEntry, "model_name", "value"),
    (RouteBundleDocument, "routes", "key"),
    (RouteIdentityDocument, "aliases", "key"),
    (GatewayKeyCreateRequest, "models", "item"),
)


def _models() -> list[type[BaseModel]]:
    for package in (vonk_control, cluster_profiles):
        for module in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
            if not module.name.endswith(".__main__"):
                importlib.import_module(module.name)
    found: list[type[BaseModel]] = []
    pending = list(BaseModel.__subclasses__())
    while pending:
        model = pending.pop()
        pending.extend(model.__subclasses__())
        if model.__module__.startswith(("vonk_control.", "cluster_profiles.")):
            found.append(model)
    return found


def _without_none(annotation: Any) -> Any:
    if get_origin(annotation) in (Union, types.UnionType):
        members = [item for item in get_args(annotation) if item is not type(None)]
        if len(members) == 1:
            return members[0]
    return annotation


def _slot(model: type[BaseModel], name: str, part: str) -> TypeAdapter[Any]:
    field = model.model_fields[name]
    if part == "value":
        annotation = field.annotation
        if field.metadata:
            annotation = Annotated[(annotation, *field.metadata)]  # type: ignore[valid-type]
        return TypeAdapter(annotation)
    container = _without_none(field.annotation)
    return TypeAdapter(get_args(container)[0])


def _alias_slots() -> list[tuple[type[BaseModel], str, str]]:
    slots = list(CARRIERS)
    for model in _models():
        for name, field in model.model_fields.items():
            if name == "alias":
                slots.append((model, name, "value"))
            elif name == "aliases" and (model, name, "key") not in slots:
                container = get_origin(_without_none(field.annotation))
                slots.append((model, name, "key" if container is dict else "item"))
    return slots


def test_every_alias_slot_accepts_and_refuses_exactly_the_contract_aliases() -> None:
    # Catches any model field carrying an alias with its own rule: a shorter
    # cap, a looser pattern, or no pattern at all.
    wrong: list[str] = []
    for model, name, part in _alias_slots():
        slot = _slot(model, name, part)
        for value in ACCEPTED:
            try:
                slot.validate_python(value)
            except ValidationError:
                wrong.append(
                    f"{model.__module__}.{model.__qualname__}.{name} refuses {value!r}"
                )
        for value in REFUSED:
            try:
                slot.validate_python(value)
            except ValidationError:
                continue
            wrong.append(
                f"{model.__module__}.{model.__qualname__}.{name} accepts {value!r}"
            )
    assert not wrong, "\n".join(wrong)


def _openapi_alias_schemas(document: Any, path: str = "") -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if isinstance(document, dict):
        if document.get("name") == "alias" and document.get("in") in {"query", "path"}:
            found.append((path, document["schema"]))
        for key, value in document.get("properties", {}).items():
            if key == "alias":
                found.append((f"{path}.{key}", value))
        for key, value in document.items():
            found.extend(_openapi_alias_schemas(value, f"{path}/{key}"))
    elif isinstance(document, list):
        for index, value in enumerate(document):
            found.extend(_openapi_alias_schemas(value, f"{path}/{index}"))
    return found


def test_every_http_alias_parameter_publishes_the_contract_rule() -> None:
    # Catches a query or path parameter that caps aliases on its own, which
    # no model walk sees: the discovery query used to refuse 64+ characters.
    openapi = schema_application(browser_auth=False).openapi()
    found = _openapi_alias_schemas(openapi)
    assert any("/parameters/" in path for path, _ in found)
    wrong = []
    for path, schema in found:
        options = schema.get("anyOf", [schema])
        strings = [item for item in options if item.get("type") == "string"]
        if "$ref" in schema or "allOf" in schema:
            continue
        if not strings or any(
            item.get("pattern") != ENDPOINT_ALIAS_PATTERN
            or item.get("maxLength") != ENDPOINT_ALIAS_MAX_LENGTH
            for item in strings
        ):
            wrong.append(f"{path}: {schema}")
    assert not wrong, "\n".join(wrong)


def _source_files() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "control/src", "src"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.split()
    return [ROOT / name for name in listed if name.endswith(".py")]


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


def alias_rule_sites(source: str, shared: frozenset[str] = frozenset()) -> list[int]:
    """Lines that check an alias in code with a rule other than the contract's.

    ``shared`` names contract regexes another module compiled; importing one
    of them by name counts as using the contract.
    """
    tree = ast.parse(source)
    contract = _contract_regexes(tree) | {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name in shared
    }
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


def test_no_code_checks_an_alias_with_its_own_rule() -> None:
    # Catches a regex, a length comparison or a parameter constraint that
    # re-states the alias rule outside the contract type (in control/src or
    # src/), however its pattern constant is named.
    sources = {path: path.read_text(encoding="utf-8") for path in _source_files()}
    shared = frozenset(
        name
        for source in sources.values()
        for name in _contract_regexes(ast.parse(source))
    )
    wrong = [
        f"{path.relative_to(ROOT)}:{line}"
        for path, source in sources.items()
        for line in alias_rule_sites(source, shared)
    ]
    assert not wrong, "\n".join(wrong)


@pytest.mark.parametrize(
    "source",
    [
        "_RULE = re.compile(r'[a-z]{0,62}')\nok = _RULE.fullmatch(run.alias)\n",
        "ok = re.fullmatch(_IDENTIFIER, alias)\n",
        "if len(alias) > 63:\n    pass\n",
        "def f(alias: str | None = Query(pattern=_PATTERN, max_length=63)): ...\n",
        "def f(alias: Annotated[str, Field(max_length=LIMIT)]): ...\n",
    ],
)
def test_a_hand_written_alias_check_is_found(source: str) -> None:
    assert alias_rule_sites(source)


@pytest.mark.parametrize(
    "source",
    [
        "_ALIAS = re.compile(ENDPOINT_ALIAS_PATTERN)\nok = _ALIAS.fullmatch(alias)\n",
        "unique = len(aliases) == len(set(aliases))\n",
        "def f(alias: EndpointAlias | None = Query(default=None)): ...\n",
        "def f(alias=Query(pattern=ENDPOINT_ALIAS_PATTERN)): ...\n",
        "ok = _UPSTREAM.fullmatch(model)\n",
    ],
)
def test_a_contract_alias_check_passes(source: str) -> None:
    assert not alias_rule_sites(source)


def test_the_vonkctl_mirror_of_the_alias_rule_matches_its_owner() -> None:
    # The installed CLI cannot import the contracts package, so it mirrors the
    # pattern in its parser and manifest schema; this catches either drifting.
    assert fixture_contracts.ENDPOINT_ALIAS_PATTERN == ENDPOINT_ALIAS_PATTERN
    for name in (
        "schemas/qualification-manifest-v2.schema.json",
        "src/cluster_profiles/schemas/qualification-manifest-v2.schema.json",
    ):
        schema = json.loads((ROOT / name).read_text(encoding="utf-8"))
        assert schema["$defs"]["endpointAlias"]["pattern"] == ENDPOINT_ALIAS_PATTERN
        service = schema["$defs"]["serviceRecipe"]["properties"]["alias"]
        assert service == {"$ref": "#/$defs/endpointAlias"}
    assert TypeAdapter(EndpointAlias).validate_python(ACCEPTED[1]) == ACCEPTED[1]
