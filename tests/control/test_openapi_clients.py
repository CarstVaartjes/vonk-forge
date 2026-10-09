from __future__ import annotations

import importlib
import json
from collections.abc import Iterable
from importlib.resources import files
from pathlib import Path
from typing import Literal, Protocol, overload

import pytest

from cluster_profiles.control_client import (
    ControlClientError,
    validate_control_document,
)

ROOT = Path(__file__).resolve().parents[2]
OPENAPI = ROOT / "control/openapi.json"
PYTHON_CLIENT = ROOT / "src/cluster_profiles/generated_control"
TYPESCRIPT_CLIENT = ROOT / "control/web/src/api/generated.d.ts"
PACKAGED_CLI_OPENAPI = files("cluster_profiles.schemas").joinpath(
    "control-openapi.json"
)


class _JSONObject(Protocol):
    """The decoded JSON object shape the operation walk reads."""

    @overload
    def __getitem__(self, key: Literal["operationId"]) -> str: ...
    @overload
    def __getitem__(self, key: str) -> _JSONObject: ...
    def values(self) -> Iterable[_JSONObject]: ...
    def items(self) -> Iterable[tuple[str, _JSONObject]]: ...


def _operations(schema: _JSONObject) -> dict[str, _JSONObject]:
    return {
        operation["operationId"]: operation
        for path in schema["paths"].values()
        for method, operation in path.items()
        if method in {"delete", "get", "patch", "post", "put"}
    }


def test_cli_packages_the_generated_admin_openapi_contract() -> None:
    schema = json.loads(PACKAGED_CLI_OPENAPI.read_text())
    assert "/api/artifact-jobs/{job_id}" in schema["paths"]
    assert "/api/auth/login" not in schema["paths"]
    assert schema["openapi"].startswith("3.1.")


def test_enrollment_and_error_producers_reach_generated_http_consumer() -> None:
    import httpx2
    from vonk_agent_protocol import LifecycleState
    from vonk_control.operation_api.contracts import BoundedErrorResponse
    from vonk_control.operator_projection_api import FleetActionResponse
    from vonk_control.strict_json import serialize_json_value

    from cluster_profiles.generated_control.api.default import (
        enroll_fleet_node,
        resume_job,
    )
    from cluster_profiles.generated_control.client import Client

    client = Client(base_url="https://control.invalid")
    producer = FleetActionResponse(action="enroll", state=LifecycleState.SUCCEEDED)
    payload = serialize_json_value(producer)
    received = enroll_fleet_node._parse_response(
        client=client, response=httpx2.Response(201, json=payload)
    )
    assert received is not None
    assert (
        validate_control_document("FleetActionResponse", received.to_dict()) == payload
    )
    for status in (401, 403, 404, 409, 503):
        error = BoundedErrorResponse(detail="bounded public cause")
        payload = serialize_json_value(error)
        received = resume_job._parse_response(
            client=client, response=httpx2.Response(status, json=payload)
        )
        assert received is not None
        assert received.to_dict() == payload


def test_generated_run_switch_clients_accept_auto_wait_and_preserve_manual_wait() -> (
    None
):
    from cluster_profiles.generated_control.models.run_switch_operation import (
        RunSwitchOperation,
    )

    node_id = f"spk_{'1' * 32}"
    progress = {
        "phase": "final_verify",
        "phase_count": 1,
        "phase_index": 0,
        "members": [{"node_id": node_id, "state": "pending"}],
        "state": "observing",
        "total_bytes_known": False,
    }
    payload = {
        "action": "run",
        "completed_phases": [],
        "kind": "recipe.run-switch.v2",
        "node_ids": [node_id],
        "operation_id": "11111111-1111-4111-8111-111111111111",
        "plan_digest": "a" * 64,
        "progress": progress,
        "request_key": "22222222-2222-4222-8222-222222222222",
        "state": "observing",
    }

    operation = RunSwitchOperation.from_dict(payload)
    assert operation.state == "observing"
    assert operation.progress.state == "observing"

    manual_wait = {
        **payload,
        "state": "needs-operator",
        "progress": {**progress, "state": "needs-operator"},
    }
    projected = RunSwitchOperation.from_dict(manual_wait)
    assert projected.state == "needs-operator"
    assert projected.progress.state == "needs-operator"


def test_library_contract_uses_direct_canonical_model_and_recipe_facts() -> None:
    schema = json.loads(OPENAPI.read_text())
    components = schema["components"]["schemas"]
    for projection, canonical in (
        ("LibraryModelProjection", "ModelDefinition"),
        ("LibraryRecipeProjection", "RecipeDefinition"),
        ("RecipeDetailResponse", "RecipeDefinition"),
    ):
        contract = components[projection]
        assert contract["additionalProperties"] is False
        assert contract["properties"]["document"] == {
            "$ref": f"#/components/schemas/{canonical}"
        }
        assert contract["properties"]["local"] == {
            "$ref": "#/components/schemas/LibraryLocalState"
        }
    for name, field in (
        ("ModelLibraryResponse", "models"),
        ("RecipeLibraryResponse", "recipes"),
    ):
        assert components[name]["properties"][field]["maxItems"] == 512
    assert "recipe_revision_id" in components["LibraryRecipeIdentity"]["required"]
    assert {
        "LibrarySnapshot",
        "LibraryRecipeList",
        "LibraryRecipeDetail",
        "VisualRecipeDocument",
        "LibraryRecipeDefinition",
    }.isdisjoint(components)


def test_operator_cache_and_profile_requests_have_current_document_schema() -> None:
    components = json.loads(OPENAPI.read_text())["components"]["schemas"]
    for name in (
        "ModelCacheOperatorRequest",
        "RecipeOperatorRequest",
        "FleetProfileInput",
    ):
        assert (
            components[name]["properties"].get("schema_version", {}).get("const", 2)
            == 2
        )


def test_generated_profile_authoring_is_logical_and_transport_neutral() -> None:
    schema = json.loads(OPENAPI.read_text())
    operations = _operations(schema)
    components = schema["components"]["schemas"]
    assert {
        "autosaveProfile",
        "previewProfile",
        "loadProfile",
        "getProfileProgress",
    } <= set(operations)
    assert {"applyLibraryPlacement", "previewLibraryPlacement"}.isdisjoint(operations)
    authored = components["FleetProfileAssignmentInput"]
    assert {"recipe_selector", "spark_ids"} <= set(authored["required"])
    assert {
        "recipe_revision_id",
        "plan_digest",
        "model_content_sha256",
        "invocation",
    }.isdisjoint(authored["properties"])
    assert "scope" not in components["FleetProfileInput"]["properties"]
    from cluster_profiles.generated_control.models.fleet_profile_assignment_input import (
        FleetProfileAssignmentInput,
    )

    assignment = FleetProfileAssignmentInput(
        recipe_selector="publisher/recipe", spark_ids=["Spark One"]
    )
    payload = assignment.to_dict()
    assert payload["recipe_selector"] == "publisher/recipe"
    assert payload["spark_ids"] == ["Spark One"]
    assert "model_variant" not in payload


def test_streaming_artifact_transfers_are_not_generated_as_typed_clients() -> None:
    schema = json.loads(OPENAPI.read_text())
    operations = _operations(schema)

    upload = operations["uploadArtifactJobInput"]
    assert upload["x-vonk-streaming-transport"] is True
    assert upload["requestBody"] == {
        "required": True,
        "content": {
            "application/octet-stream": {
                "schema": {"format": "binary", "type": "string"}
            }
        },
    }

    download = operations["downloadArtifactJobResult"]
    assert download["x-vonk-streaming-transport"] is True
    assert download["responses"]["200"]["content"] == {
        "*/*": {"schema": {"format": "binary", "type": "string"}}
    }

    # TypeScript declarations describe the complete server contract, including
    # native transports; they do not generate buffered request methods.
    assert not (PYTHON_CLIENT / "api/default/upload_artifact_job_input.py").exists()
    assert not (PYTHON_CLIENT / "api/default/download_artifact_job_result.py").exists()


def test_admin_schema_is_secret_free() -> None:
    schema = json.loads(OPENAPI.read_text())
    assert "/api/endpoints/{alias}" not in schema["paths"]
    assert "/api/jobs" not in schema["paths"]
    assert set(schema["paths"]) >= {
        "/api/fleet",
        "/api/fleet/stream",
        "/api/jobs/{job_id}",
        "/api/jobs/{job_id}/resume",
    }
    assert "/api/nodes/status" not in schema["paths"]
    assert all(path.startswith("/api/") for path in schema["paths"])
    operation_list = [
        operation
        for path in schema["paths"].values()
        for method, operation in path.items()
        if method in {"delete", "get", "patch", "post", "put"}
    ]
    operation_ids = [operation["operationId"] for operation in operation_list]
    assert len(operation_ids) == len(set(operation_ids))
    assert all("_api_v1_" not in operation_id for operation_id in operation_ids)
    assert schema["components"]["securitySchemes"] == {
        "BearerAuth": {"scheme": "bearer", "type": "http"},
        "BrowserSession": {
            "in": "cookie",
            "name": "vonk_session",
            "type": "apiKey",
        },
    }
    operations = _operations(schema)
    assert operations["streamFleetEvents"]["security"] == [
        {"BearerAuth": []},
        {"BrowserSession": []},
    ]
    assert operations["downloadCliToken"]["security"] == [{"BrowserSession": []}]
    assert all(
        operation["security"] == [{"BearerAuth": []}, {"BrowserSession": []}]
        for operation_id, operation in operations.items()
        if operation_id
        not in {
            "getBrowserSession",
            "loginBrowser",
            "logoutBrowser",
            "downloadCliToken",
            "streamFleetEvents",
        }
    )

    retired_prefixes = ("/api/" + "packages", "/api/" + "deployments")
    assert not any(
        path.startswith(prefix)
        for path in schema["paths"]
        for prefix in retired_prefixes
    )

    by_id = {operation["operationId"]: operation for operation in operation_list}
    for operation_id in (
        "getJob",
        "resumeJob",
    ):
        response_schema = next(
            response["content"]["application/json"]["schema"]
            for status, response in sorted(by_id[operation_id]["responses"].items())
            if status.startswith("2")
        )
        reference = response_schema["$ref"]
        component = schema["components"]["schemas"][reference.rsplit("/", 1)[-1]]
        assert component["additionalProperties"] is False

    fleet = by_id["getFleetStatus"]
    assert fleet["x-vonk-streaming-transport"] is True
    assert fleet["responses"]["200"]["content"] == {
        "application/x-vonk-observation+ndjson": {
            "schema": {"$ref": "#/components/schemas/ObservationTransferRecord"}
        }
    }
    assert fleet["x-vonk-observation-payload"] == {
        "$ref": "#/components/schemas/FleetSnapshot"
    }
    components = schema["components"]["schemas"]
    assert components["FleetSnapshot"]["additionalProperties"] is False
    records = components["ObservationTransferRecord"]
    assert records["discriminator"]["propertyName"] == "type"
    assert records["oneOf"] == [
        {"$ref": f"#/components/schemas/ObservationTransfer{kind}"}
        for kind in ("Start", "Chunk", "Complete", "Error")
    ]
    for variant in records["oneOf"]:
        assert (
            components[variant["$ref"].rsplit("/", 1)[-1]]["additionalProperties"]
            is False
        )

    serialized = json.dumps(schema, sort_keys=True).lower()
    for forbidden in (
        "/agent/",
        "certificate_pem",
        "chain_pem",
        "csr_pem",
        "grant_token",
        "management_address",
        '"operation_payload":',
        "token_digest",
    ):
        assert forbidden not in serialized


def test_browser_auth_contract_declares_cookie_security_and_fixed_validation() -> None:
    schema = json.loads(OPENAPI.read_text())
    operations = _operations(schema)

    assert operations["loginBrowser"]["security"] == []
    assert operations["getBrowserSession"]["security"] == [{"BrowserSession": []}]
    assert operations["logoutBrowser"]["security"] == [{"BrowserSession": []}]
    assert operations["downloadCliToken"]["security"] == [{"BrowserSession": []}]
    assert operations["loginBrowser"]["responses"]["422"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/LoginRequestInvalid"}
    assert schema["components"]["schemas"]["LoginRequestInvalid"] == {
        "additionalProperties": False,
        "properties": {
            "detail": {
                "const": "login request is invalid",
                "title": "Detail",
                "type": "string",
            }
        },
        "required": ["detail"],
        "title": "LoginRequestInvalid",
        "type": "object",
    }


def test_generated_python_models_compile() -> None:
    for path in PYTHON_CLIENT.rglob("*.py"):
        compile(path.read_text(), str(path), "exec")


def test_packaged_profile_load_requires_a_request_key_and_refuses_overrides() -> None:
    request = {
        "request_key": "00000000-0000-4000-8000-000000000001",
    }
    assert validate_control_document("FleetProfileLoadRequest", request) == request
    with pytest.raises(ControlClientError):
        validate_control_document("FleetProfileLoadRequest", {})
    with pytest.raises(ControlClientError):
        validate_control_document(
            "FleetProfileLoadRequest", {**request, "plan_digest": "a" * 64}
        )
    with pytest.raises(ControlClientError):
        validate_control_document(
            "FleetProfileLoadRequest",
            {**request, "recipe_revision_id": "00000000-0000-4000-8000-000000000002"},
        )


def test_generated_recipe_detail_has_one_canonical_topology() -> None:
    schema = json.loads(OPENAPI.read_text())["components"]["schemas"]
    detail = schema["RecipeDetailResponse"]
    assert detail["properties"]["document"] == {
        "$ref": "#/components/schemas/RecipeDefinition"
    }
    assert {"topology", "definition", "placement", "profiles"}.isdisjoint(
        detail["properties"]
    )
    model_documents = detail["properties"]["model_documents"]
    assert model_documents["items"] == {
        "$ref": "#/components/schemas/LibraryRecipeModel"
    }
    assert model_documents["maxItems"] == 32
    assert schema["LibraryRecipeModel"]["properties"]["selection"] == {
        "$ref": "#/components/schemas/RecipeModelSelection"
    }
    assert schema["LibraryRecipeModel"]["properties"]["model_document"] == {
        "$ref": "#/components/schemas/ModelDefinition"
    }
    assert "topology" in schema["RecipeDefinition"]["properties"]


def test_generated_openapi_removes_retired_catalog_recipe_operations() -> None:
    document = json.loads(OPENAPI.read_text())
    paths = document["paths"]
    operations = {
        operation.get("operationId")
        for methods in paths.values()
        if isinstance(methods, dict)
        for operation in methods.values()
        if isinstance(operation, dict)
    }
    assert "/api/catalog/public-recipes" not in paths
    assert "/api/catalog/imports/public" not in paths
    assert "/api/catalog/imports/recipe-library" not in paths
    assert "listPublicRecipes" not in operations
    assert "previewPublicRecipeImport" not in operations
    assert "importPublicRecipe" not in operations


def test_generated_library_schema_uses_shared_authority_documents() -> None:
    components = json.loads(OPENAPI.read_text())["components"]["schemas"]
    forbidden = {
        "PublicRecipe",
        "LibraryRecipeDefinition",
        "ModelVersion",
        "Qualification",
        "Readiness",
        "RuntimeDistribution",
    }
    assert forbidden.isdisjoint(components)
    assert components["LibraryModelProjection"]["properties"]["document"] == {
        "$ref": "#/components/schemas/ModelDefinition"
    }
    assert components["LibraryRecipeProjection"]["properties"]["document"] == {
        "$ref": "#/components/schemas/RecipeDefinition"
    }


def test_generated_library_contract_drops_legacy_visual_artifact_identity() -> None:
    schema = json.loads(OPENAPI.read_text())["components"]["schemas"]
    assert "VisualArtifact" not in schema
    assert "LibraryModelArtifact" not in schema


def test_generated_python_client_imports_in_the_root_locked_environment() -> None:
    from cluster_profiles.generated_control.client import AuthenticatedClient

    assert AuthenticatedClient.__module__.startswith(
        "cluster_profiles.generated_control"
    )


def test_stream_resume_header_is_in_openapi_custom_transport_contract() -> None:
    schema = json.loads(OPENAPI.read_text())
    operation = schema["paths"]["/api/fleet/stream"]["get"]
    assert operation["parameters"] == [
        {
            "description": (
                "Optional durable Fleet cursor; duplicate and numeric validity "
                "are checked from the raw header list."
            ),
            "in": "header",
            "name": "Last-Event-ID",
            "required": False,
            "schema": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "description": (
                    "Optional durable Fleet cursor; duplicate and numeric validity "
                    "are checked from the raw header list."
                ),
                "title": "Last-Event-Id",
            },
        }
    ]

    # The Python generator excludes buffered streaming methods. TypeScript
    # declares the full contract; FleetEventConnection owns authenticated fetch
    # streaming and reconnects from the cursor applied by the browser.
    assert not (PYTHON_CLIENT / "api/default/stream_fleet_events.py").exists()


def test_generated_fleet_projection_vocabulary_is_finite() -> None:
    schema = json.loads(OPENAPI.read_text())["components"]["schemas"]
    assert schema["NodeConnection"]["properties"]["certificate_state"] == {
        "$ref": "#/components/schemas/CertificateState"
    }
    assert schema["CertificateState"]["enum"] == [
        "valid",
        "missing",
        "not-yet-valid",
        "expired",
        "revoked",
        "inactive",
    ]
    offline = schema["NodeConnection"]["properties"]["offline_reason"]
    assert offline["anyOf"][0] == {"$ref": "#/components/schemas/NodeOfflineReason"}
    assert schema["NodeOfflineReason"]["enum"] == [
        "unregistered",
        "agent-inactive",
        "agent-revoked",
        "never-seen",
        "last-seen-in-future",
        "stale",
        "certificate-missing",
        "certificate-not-yet-valid",
        "certificate-expired",
        "certificate-revoked",
        "certificate-inactive",
    ]
    assert schema["TelemetryPoint"]["properties"]["boot_id"]["pattern"] == (
        "^(?!00000000-0000-0000-0000-000000000000$)"
        "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    typescript = TYPESCRIPT_CLIENT.read_text()
    assert 'certificate_state: components["schemas"]["CertificateState"]' in typescript
    assert (
        'degraded_reason?: components["schemas"]["InstallDegradedReason"]' in typescript
    )
    assert schema["InstallDegradedReason"]["enum"][:2] == [
        "external-member",
        "mapping-incomplete",
    ]


def test_generated_telemetry_contract_is_concrete() -> None:
    schema = json.loads(OPENAPI.read_text())["components"]["schemas"]
    assert schema["TelemetryPoint"]["type"] == "object"
    assert schema["TelemetryPoint"]["additionalProperties"] is False

    typescript = TYPESCRIPT_CLIENT.read_text()
    assert "TelemetryPoint: {" in typescript
    assert "TelemetryPoint: {[key: string]: unknown};" not in typescript


def test_generated_python_client_parses_documented_operation_errors() -> None:
    import httpx2

    from cluster_profiles.generated_control.client import Client
    from cluster_profiles.generated_control.models.bounded_error_response import (
        BoundedErrorResponse,
    )

    client = Client(base_url="https://control.invalid")
    expected = {
        "resume_job": (401, 403, 404, 409, 503),
    }
    for module_name, status_codes in expected.items():
        module = importlib.import_module(
            "cluster_profiles.generated_control.api.default." + module_name
        )
        for status_code in status_codes:
            parsed = module._parse_response(
                client=client,
                response=httpx2.Response(
                    status_code,
                    json={"detail": f"bounded-{status_code}"},
                ),
            )
            assert isinstance(parsed, BoundedErrorResponse)
            assert parsed.detail == f"bounded-{status_code}"
