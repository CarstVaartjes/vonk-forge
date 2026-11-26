"""Generated consumer inputs match their canonical Pydantic producers.

Test setup generates the inputs before collection. Determinism is checked by
scripts/check-generated-determinism; producer/consumer contracts stay covered
here and in the Python, browser, and Rust round-trip suites.
"""

from __future__ import annotations

import ast
import importlib.machinery
import importlib.util
import json
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

from cluster_profiles.control_client.shown_words import (
    DECIDED_BY,
    open_shown_words,
    received_schemas,
    response_word_sets,
)

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "src/cluster_profiles"


def _script(name: str) -> ModuleType:
    path = ROOT / "scripts" / name
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _stale(path: Path, content: str) -> bool:
    return not path.is_file() or path.read_text(encoding="utf-8") != content


def test_agent_wire_schema_is_current() -> None:
    rendered = _script("export-agent-wire-schema").rendered()
    wire = ROOT / "rust/crates/vonk-agent-protocol/schema/wire.json"
    assert not _stale(wire, rendered), (
        "stale wire.json; run scripts/generate-agent-wire"
    )


def test_installer_release_schema_is_current() -> None:
    module = _script("export-installer-release-schema")
    assert not _stale(module.OUTPUT, module.rendered()), (
        "stale installer release schema; run scripts/generate-agent-wire"
    )


def test_cli_release_projection_schema_is_current() -> None:
    module = _script("export-installer-release-schema")
    assert not _stale(module.CLI_OUTPUT, module.rendered_cli_projection())


def test_qualification_campaign_schemas_are_current() -> None:
    stale = [
        str(path.relative_to(ROOT))
        for path, content in _script("export-qualification-campaign-schemas")
        .rendered()
        .items()
        if _stale(path, content)
    ]
    assert stale == [], "run scripts/export-qualification-campaign-schemas"


def test_control_openapi_documents_are_current() -> None:
    module = _script("generate-control-clients")

    def document(schema: dict[str, object]) -> str:
        return json.dumps(schema, sort_keys=True, indent=2) + "\n"

    expected = {
        module.OPENAPI: document(module._schema(include_browser_auth=True)),
        module.CLI_OPENAPI_OUTPUT: document(module.cli_schema()),
    }
    stale = [
        str(path.relative_to(ROOT))
        for path, content in expected.items()
        if _stale(path, content)
    ]
    assert stale == [], "run scripts/generate-control-clients"


def test_typescript_vocabulary_is_current() -> None:
    module = _script("generate-control-clients")
    target = module.TYPESCRIPT_VOCABULARY
    assert not _stale(target, module.typescript_vocabulary()), (
        "stale vocabulary.generated.ts; run scripts/generate-control-clients"
    )


def test_python_vocabulary_modules_are_current() -> None:
    stale = [
        str(path.relative_to(ROOT))
        for path, content in _script("generate-python-vocabulary").rendered().items()
        if _stale(path, content)
    ]
    assert stale == [], "run scripts/generate-python-vocabulary"


def _held_alone(node: object) -> Iterator[str]:
    """Components a schema holds in a field of their own (alone or beside null).

    A member of a union of several components is held only with the union.
    """

    if isinstance(node, list):
        for item in node:
            yield from _held_alone(item)
        return
    if not isinstance(node, dict):
        return
    reference = node.get("$ref")
    if isinstance(reference, str):
        yield reference.rsplit("/", 1)[-1]
    alternatives = node.get("anyOf")
    if isinstance(alternatives, list):
        named = [item for item in alternatives if item != {"type": "null"}]
        if len(named) == 1:
            yield from _held_alone(named[0])
        else:
            yield from _held_alone([item for item in named if "$ref" not in item])
    for key, value in node.items():
        if key not in {"$ref", "anyOf"}:
            yield from _held_alone(value)


def _decided_by_cli() -> set[str]:
    """Vocabularies whose words a vonkctl module outside its renderers imports."""

    sources = _script("generate-python-vocabulary").cli_word_sources()
    decided: set[str] = set()
    for path in sorted(CLI.rglob("*.py")):
        if path.relative_to(CLI).parts[0] in {"cli_render", "generated_control"}:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "cluster_profiles.cli_states_generated"
            ):
                for alias in node.names:
                    decided |= sources[alias.name]
    return decided


def test_the_cli_closes_exactly_the_response_words_it_decides_by() -> None:
    exact = _script("generate-control-clients")._schema(include_browser_auth=False)
    schemas = exact["components"]["schemas"]
    roots = [
        operation.get("responses")
        for item in exact["paths"].values()
        for operation in item.values()
        if isinstance(operation, dict)
    ]
    held = {
        *_held_alone(roots),
        *(
            name
            for parent in received_schemas(exact)
            for name in _held_alone(schemas[parent])
        ),
    }

    # A set named here that vonkctl only shows must open; a set it decides by
    # that a response field holds must be named.
    assert _decided_by_cli() & held & response_word_sets(exact) == DECIDED_BY


def test_only_shown_response_words_open() -> None:
    def reference(name: str) -> dict[str, str]:
        return {"$ref": f"#/components/schemas/{name}"}

    def body(name: str) -> dict[str, object]:
        return {"content": {"application/json": {"schema": reference(name)}}}

    document: dict[str, Any] = {
        "paths": {
            "/api/thing": {
                "get": {"responses": {"200": body("Thing")}},
                "post": {"requestBody": body("Order"), "responses": {}},
            }
        },
        "components": {
            "schemas": {
                "Thing": {
                    "type": "object",
                    "properties": {
                        "reason": reference("ShownCode"),
                        "state": reference("LifecycleState"),
                        "mode": reference("OrderMode"),
                    },
                },
                "Order": {
                    "type": "object",
                    "properties": {"mode": reference("OrderMode")},
                },
                "ShownCode": {"enum": ["a.b"], "title": "ShownCode", "type": "string"},
                "LifecycleState": {"enum": ["queued"], "type": "string"},
                "OrderMode": {"enum": ["fast"], "type": "string"},
            }
        },
    }

    open_shown_words(document)
    schemas = document["components"]["schemas"]

    assert schemas["ShownCode"] == {
        "title": "ShownCode",
        "type": "string",
        "minLength": 1,
    }
    # Decided by vonkctl, and authored by it in a request: both stay closed.
    assert schemas["LifecycleState"]["enum"] == ["queued"]
    assert schemas["OrderMode"]["enum"] == ["fast"]
