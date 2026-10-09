"""Keep the published nested JSON graph concrete at every fixed boundary."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime

from sqlalchemy.orm import sessionmaker
from vonk_control.api import create_app
from vonk_control.auth import TokenCodec
from vonk_control.browser_auth import BrowserAuthService
from vonk_control.distribution import DistributionService, MemoryObjectSource
from vonk_control.jobs import JobService

from .test_agent_api import NODE_A, agent_headers
from .test_agent_api import agent_system as _agent_system
from .test_distribution import _assignment

agent_system = _agent_system

# These values are intentionally defined by the selected engine or authority
# document. Their surrounding request, response, and receipt remain typed.
EXTENSION_OBJECTS: dict[str, str] = {}


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
        jobs=JobService(sessionmaker(), clock=lambda: datetime(2026, 9, 7, tzinfo=UTC)),
        tokens=TokenCodec(key),
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


def test_download_authority_binds_the_exact_bytes_and_rejects_unbound_ranges(
    agent_system,
) -> None:
    from pathlib import Path

    client, services, _sessions, clock = agent_system
    source = MemoryObjectSource()
    expected = b"model payload"
    digest = source.put(expected)
    config = source.put(b"config!")
    archive = source.put(b"oci archive")
    assignment = _assignment(NODE_A, digest, config, archive)
    source.register_artifact_set(
        assignment.model_artifact_set_sha256, assignment.objects
    )
    source.register_runtime_image(assignment.oci_image_digest, archive)
    distribution = DistributionService(source, clock=clock)
    distribution.register(assignment)
    object.__setattr__(services, "distribution", distribution)
    path = f"/agent/distribution/objects/{digest}?plan_digest={assignment.plan_digest}"
    headers = agent_headers(NODE_A, "serial-a")
    refused = client.get(
        path,
        headers={
            **headers,
            "Range": "bytes=0-1",
            "If-Range": '"sha256:' + "0" * 64 + '"',
        },
    )
    assert refused.status_code == 412
    assert "x-vonk-file" not in refused.headers
    accepted = client.get(path, headers={**headers, "Range": "bytes=0-1"})
    assert accepted.status_code == 200
    # Controller authorizes the exact immutable file; the byte-serving edge
    # consumes this path. No unverified or mismatched object is handed off.
    exposed = Path("/") / accepted.headers["x-vonk-file"]
    assert exposed.read_bytes() == expected
    assert exposed == source.root / digest
    assert accepted.headers["etag"] == f'"sha256:{digest}"'


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


def test_authored_job_input_is_consumed_without_losing_defaults_or_false() -> None:
    from vonk_agent_protocol.compiled_execution_plan import CompiledJobInput
    from vonk_forge_contracts.recipe import RecipeJobInput

    authored = RecipeJobInput(required=False, media_types=["image/png"], max_bytes=1024)
    compiled = CompiledJobInput.model_validate_json(authored.model_dump_json())
    returned = RecipeJobInput.model_validate_json(compiled.model_dump_json())
    assert returned == authored
    assert compiled.required is False
    assert compiled.slots is None
    explicit_null = authored.model_dump(mode="json") | {"slots": None}
    assert CompiledJobInput.model_validate_json(json.dumps(explicit_null)) == compiled
