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


def _application():
    # Inspect FastAPI's actual serialization schema, including agent routes.
    # Validation-mode Pydantic schemas alone miss custom serializer regressions.
    key = b"schema-test-signing-key-32-bytes!"
    return create_app(
        jobs=object(),
        tokens=TokenCodec(key),
        audits=MemoryAuditStore(),
        browser_auth=BrowserAuthService(
            sessionmaker(),
            token_signing_key=key,
            clock=lambda: datetime(2026, 9, 7, tzinfo=UTC),
        ),
    )


def test_published_contract_graph_only_leaves_engine_and_document_values_open() -> None:
    schemas = _application().openapi()["components"]["schemas"]
    actual = set(_open_objects(schemas))
    unexpected = actual - EXTENSION_OBJECTS.keys()
    stale_exceptions = EXTENSION_OBJECTS.keys() - actual
    assert not unexpected, f"Fixed documents became untyped: {sorted(unexpected)}"
    assert not stale_exceptions, f"Remove unused exceptions: {sorted(stale_exceptions)}"


def test_download_responses_describe_actual_bytes_and_range_support() -> None:
    paths = _application().openapi()["paths"]
    downloads = {
        "/agent/source-bundles/{source_sha256}": (
            "application/vnd.vonk-forge.source-bundle.v1+tar",
            False,
        ),
        "/agent/artifacts/{sha256}": ("application/octet-stream", True),
        "/agent/distribution/objects/{sha256}": ("application/octet-stream", True),
        "/agent/workload-tuf/metadata/{name}": ("application/json", False),
        "/agent/workload-tuf/targets/{name}": ("application/octet-stream", False),
        "/api/jobs/{job_id}/logs/{digest}": ("text/plain", False),
    }
    for path, (media_type, partial) in downloads.items():
        operation = paths[path]["get"]
        assert operation["x-vonk-streaming-transport"] is True
        for code in ("200", "206") if partial else ("200",):
            assert operation["responses"][code]["content"] == {
                media_type: {"schema": {"type": "string", "format": "binary"}}
            }


def test_successful_response_content_has_a_declared_schema() -> None:
    # Components alone cannot detect an empty schema on a route response.
    for path, methods in _application().openapi()["paths"].items():
        for method, operation in methods.items():
            if not isinstance(operation, dict):
                continue
            for code, response in operation.get("responses", {}).items():
                if not code.startswith("2"):
                    continue
                for media_type, content in response.get("content", {}).items():
                    assert content.get("schema"), (method, path, code, media_type)


def test_fleet_stream_describes_canonical_frames_under_only_its_actual_media_type() -> (
    None
):
    schema = _application().openapi()
    operation = schema["paths"]["/api/fleet/stream"]["get"]
    assert operation["x-vonk-streaming-transport"] is True
    content = operation["responses"]["200"]["content"]
    assert set(content) == {"text/event-stream"}
    reference = content["text/event-stream"]["schema"]["$ref"]
    event = schema["components"]["schemas"][reference.rsplit("/", 1)[-1]]
    assert {item["$ref"].rsplit("/", 1)[-1] for item in event["anyOf"]} == {
        "FleetSnapshotEvent",
        "FleetTelemetryEvent",
        "FleetChangeEvent",
    }


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
