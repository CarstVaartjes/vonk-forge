"""Keep the published nested JSON graph concrete at every fixed boundary."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

from sqlalchemy.orm import sessionmaker
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import TokenCodec
from vonk_control.browser_auth import BrowserAuthService

# These values are intentionally defined by the selected engine or authority
# document. Their surrounding request, response, and receipt remain typed.
EXTENSION_OBJECTS = {
    "ArtifactJobCreate.properties.parameters": "Engine-defined parameter values",
    "ArtifactJobResultEvidence": "Engine-specific output measurements",
    "CompiledArtifactContract.properties.engine.anyOf.0": "Engine keyword arguments",
    "EffectiveSettingsSelection.properties.knobs": "Recipe-declared settings values",
    "MappingSelection.properties.parameters": "Recipe-declared parameter values",
    "ProposalChangeRequest.properties.document": "Authority document selected by path",
    "RecipeJobRunRequest.properties.parameters": "Engine-defined parameter values",
}


def _open_objects(value: object, path: tuple[str, ...] = ()) -> Iterator[str]:
    if isinstance(value, dict):
        if value.get("type") == "object" and value.get("additionalProperties") is True:
            yield ".".join(path)
        for key, child in value.items():
            yield from _open_objects(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _open_objects(child, (*path, str(index)))


def test_published_contract_graph_only_leaves_engine_and_document_values_open() -> None:
    # Inspect FastAPI's actual serialization schema, including agent routes.
    # Validation-mode Pydantic schemas alone miss custom serializer regressions.
    key = b"schema-test-signing-key-32-bytes!"
    app = create_app(
        jobs=object(),
        tokens=TokenCodec(key),
        audits=MemoryAuditStore(),
        browser_auth=BrowserAuthService(
            sessionmaker(),
            token_signing_key=key,
            clock=lambda: datetime(2026, 9, 7, tzinfo=UTC),
        ),
    )
    schemas = app.openapi()["components"]["schemas"]
    actual = set(_open_objects(schemas))
    unexpected = actual - EXTENSION_OBJECTS.keys()
    stale_exceptions = EXTENSION_OBJECTS.keys() - actual
    assert not unexpected, f"Fixed documents became untyped: {sorted(unexpected)}"
    assert not stale_exceptions, f"Remove unused exceptions: {sorted(stale_exceptions)}"


def _structural_schema(value: object, definitions: dict[str, object]) -> object:
    if isinstance(value, list):
        return [_structural_schema(child, definitions) for child in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        return _structural_schema(
            definitions[value["$ref"].rsplit("/", 1)[-1]], definitions
        )
    return {
        key: _structural_schema(child, definitions)
        for key, child in value.items()
        if key != "$defs"
        and not (key in {"title", "description"} and isinstance(child, str))
    }


def test_authored_job_input_and_compiled_wire_input_have_identical_structure() -> None:
    from vonk_agent_protocol.compiled_execution_plan import CompiledJobInput
    from vonk_forge_contracts.recipe import RecipeJobInput

    # The standalone agent wheel does not install the catalog authoring wheel.
    # Prove this intentionally mirrored wire structure follows its source,
    # including every nested field, required marker, and schema constraint.
    authored = RecipeJobInput.model_json_schema()
    compiled = CompiledJobInput.model_json_schema()
    assert _structural_schema(authored, authored.get("$defs", {})) == (
        _structural_schema(compiled, compiled.get("$defs", {}))
    )
