"""Every endpoint alias slot in the Controller follows the one contract rule.

``vonk_forge_contracts.endpoint_alias`` owns the rule. A profile accepted
128-character aliases while the run plan, endpoint contract, discovery query
and route candidate each kept a 63-character copy: the profile saved, then the
load could not be planned and its endpoint never published. These nets find
every alias slot by what it carries, not by a literal, and check how it behaves.
Code that checks an alias by hand is the ``alias-rules`` added-line guard.
"""

from __future__ import annotations

import importlib
import pkgutil
import types
from typing import Annotated, Any, Union, get_args, get_origin

import vonk_control
from pydantic import BaseModel, TypeAdapter, ValidationError
from vonk_control.contract_graph import schema_application
from vonk_control.fleet_profile_contract.definitions import (
    FleetProfileAssignmentInput,
    FleetProfileAssignmentView,
)
from vonk_control.gateway_keys import GatewayKeyCreateRequest
from vonk_control.litellm import LiteLlmModelEntry
from vonk_control.route_bundle_contract import (
    RouteBundleDocument,
    RouteIdentityDocument,
)
from vonk_forge_contracts import ENDPOINT_ALIAS_PATTERN
from vonk_forge_contracts.endpoint_alias import ENDPOINT_ALIAS_MAX_LENGTH

import cluster_profiles

ACCEPTED = ("a", "chat-" + "x" * (ENDPOINT_ALIAS_MAX_LENGTH - len("chat-")))
REFUSED = (
    "",
    "Chat",
    "chat-",
    "-chat",
    "chat/x",
    "x" * (ENDPOINT_ALIAS_MAX_LENGTH + 1),
)
# Alias slots whose field name does not say alias: (model, field, part).
CARRIERS: tuple[tuple[type[BaseModel], str, str], ...] = (
    (LiteLlmModelEntry, "model_name", "value"),
    (RouteBundleDocument, "routes", "key"),
    (RouteIdentityDocument, "aliases", "key"),
    (GatewayKeyCreateRequest, "models", "item"),
    (FleetProfileAssignmentInput, "assignment_name", "value"),
    (FleetProfileAssignmentView, "selector", "value"),
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
