from __future__ import annotations

import importlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OPENAPI = ROOT / "control/openapi.json"
PYTHON_CLIENT = ROOT / "src/cluster_profiles/generated_control"
TYPESCRIPT_CLIENT = ROOT / "control/web/src/api/generated.d.ts"


def _operations(schema: dict[str, object]) -> dict[str, dict[str, object]]:
    return {
        operation["operationId"]: operation
        for path in schema["paths"].values()
        for method, operation in path.items()
        if method in {"delete", "get", "patch", "post", "put"}
    }


def test_tracked_admin_contract_has_direct_enrollment_and_typed_errors() -> None:
    schema = json.loads(OPENAPI.read_text())
    operations = _operations(schema)
    successes = {
        "createEnrollmentGrant": ("201", "EnrollmentGrantResponse"),
        "listAgentEnrollments": ("200", "EnrollmentListResponse"),
    }
    for operation_id, (status_code, component) in successes.items():
        response_schema = operations[operation_id]["responses"][status_code]["content"][
            "application/json"
        ]["schema"]
        assert response_schema == {"$ref": f"#/components/schemas/{component}"}
        assert (
            schema["components"]["schemas"][component]["additionalProperties"] is False
        )
    assert "approveAgentEnrollment" not in operations
    assert "rejectAgentEnrollment" not in operations
    assert "EnrollmentDecisionResponse" not in schema["components"]["schemas"]

    expected_errors = {
        "getJobLog": {"401", "403", "404", "503"},
        "getPublishedEndpoint": {"401", "404", "503"},
        "resumeJob": {"401", "403", "404", "409", "503"},
    }
    for operation_id, statuses in expected_errors.items():
        for status_code in statuses:
            assert operations[operation_id]["responses"][status_code]["content"][
                "application/json"
            ]["schema"] == {"$ref": "#/components/schemas/BoundedErrorResponse"}
    bounded_error = schema["components"]["schemas"]["BoundedErrorResponse"]
    assert bounded_error["additionalProperties"] is False
    assert bounded_error["properties"]["detail"]["maxLength"] == 256

    progress = schema["components"]["schemas"]["JobOperationResponse"]["properties"][
        "progress"
    ]
    assert any(
        option.get("$ref") == "#/components/schemas/JobOperationProgress"
        for option in progress["anyOf"]
    )

    serialized = json.dumps(schema, sort_keys=True).lower()
    assert "certificate_pem" not in serialized
    assert "chain_pem" not in serialized


def test_library_contract_uses_direct_canonical_model_and_recipe_facts() -> None:
    schema = json.loads(OPENAPI.read_text())
    components = schema["components"]["schemas"]
    operations = _operations(schema)
    library_model = components["LibraryModel"]
    assert set(library_model["properties"]) == {
        "model",
        "model_document",
        "model_capabilities",
        "page_local",
        "recipes",
    }
    assert library_model["properties"]["model"] == {
        "$ref": "#/components/schemas/LibraryModelIdentity"
    }
    assert library_model["properties"]["model_document"] == {
        "$ref": "#/components/schemas/ModelDefinition"
    }
    assert library_model["properties"]["model_capabilities"] == {
        "$ref": "#/components/schemas/LibraryCapabilityInventory"
    }
    assert components["LibraryRecipeSummary"]["properties"][
        "recipe_capabilities"
    ] == {"$ref": "#/components/schemas/LibraryCapabilityInventory"}
    assert components["LibraryRecipeSummary"]["properties"]["recipe_document"] == {
        "$ref": "#/components/schemas/RecipeDefinition"
    }
    assert components["LibraryRecipeSummary"]["properties"]["recipe_revision_id"] == {
        "pattern": (
            "^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            "[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        ),
        "title": "Recipe Revision Id",
        "type": "string",
    }
    assert components["LibraryRecipeDetail"]["properties"][
        "model_capabilities"
    ] == {"$ref": "#/components/schemas/LibraryCapabilityInventory"}
    assert components["LibraryRecipeDetail"]["properties"][
        "recipe_capabilities"
    ] == {"$ref": "#/components/schemas/LibraryCapabilityInventory"}
    assert components["LibraryRecipeDetail"]["properties"]["definition"] == {
        "$ref": "#/components/schemas/RecipeDefinition"
    }
    assert components["LibraryCapabilityInventory"]["properties"]["schema_version"][
        "const"
    ] == 2
    model_identity = components["LibraryModelIdentity"]
    assert model_identity["properties"]["kind"]["const"] == "model"
    assert set(model_identity["required"]) == {
        "publisher",
        "slug",
        "content_sha256",
    }
    recipe_identity = components["LibraryRecipeIdentity"]
    assert "source_kind" not in recipe_identity["properties"]
    assert "content_sha256" in recipe_identity["properties"]
    assert "selected_revision" not in components["LibraryRecipeSummary"]["properties"]
    assert library_model["properties"]["recipes"]["minItems"] == 0
    assert library_model["properties"]["recipes"]["maxItems"] == 512
    recipe_list = components["LibraryRecipeList"]
    assert "minItems" not in recipe_list["properties"]["recipes"]
    assert recipe_list["properties"]["recipes"]["maxItems"] == 512
    assert operations["listLibraryRecipes"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/LibraryRecipeList"}

    assert "ModelVersionIdentity" not in components
    assert "LibraryModelVersionFacts" not in components
    assert "RecipeRevisionSummary" not in components
    assert "LibraryRecipeDefinition" not in components


def test_repair_manifest_is_v2_while_upgrade_package_remains_v1() -> None:
    schema = json.loads(OPENAPI.read_text())
    components = schema["components"]["schemas"]
    assert (
        components["AgentRepairManifestRequest"]["properties"]["schema_version"][
            "const"
        ]
        == 2
    )
    assert (
        components["AgentUpgradePackageRequest"]["properties"]["schema_version"][
            "const"
        ]
        == 1
    )

    typescript = TYPESCRIPT_CLIENT.read_text()
    assert "AgentRepairManifestRequest" in typescript
    assert "schema_version: 2;" in typescript
    python_client = (
        PYTHON_CLIENT / "models/agent_repair_manifest_request.py"
    ).read_text()
    assert "schema_version: Literal[2]" in python_client


def test_generated_library_placement_is_digest_bound_and_transport_neutral() -> None:
    schema = json.loads(OPENAPI.read_text())
    operations = _operations(schema)
    components = schema["components"]["schemas"]

    assert {
        "previewLibraryPlacement",
        "applyLibraryPlacement",
        "getLibraryPlacement",
    } <= set(operations)
    assert operations["applyLibraryPlacement"]["responses"]["202"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/LibraryPlacementApplication"}
    apply = components["LibraryPlacementApplyRequest"]
    assert {"recipe_id", "node_ids", "plan_digest", "request_key"} <= set(
        apply["required"]
    )
    assert set(apply["properties"]["invocation"]["enum"]) == {
        "drag-drop",
        "keyboard",
        "button",
    }
    preview = components["LibraryPlacementPreview"]
    assert {"selected_node_ids", "selected_nodes", "blockers", "locations"} <= set(
        preview["required"]
    )

    from cluster_profiles.generated_control.models.library_placement_apply_request import (
        LibraryPlacementApplyRequest,
    )

    request = LibraryPlacementApplyRequest(
        recipe_id="00000000-0000-4000-8000-000000000001",
        node_ids=["spk_" + "1" * 32],
        desired_state="installed",
        alias=None,
        invocation="keyboard",
        plan_digest="a" * 64,
        request_key="00000000-0000-4000-8000-000000000002",
    )
    assert request.to_dict()["invocation"] == "keyboard"


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
        "application/octet-stream": {"schema": {"format": "binary", "type": "string"}}
    }

    typescript = TYPESCRIPT_CLIENT.read_text()
    assert "uploadArtifactJobInput" not in typescript
    assert "downloadArtifactJobResult" not in typescript
    assert not (PYTHON_CLIENT / "api/default/upload_artifact_job_input.py").exists()
    assert not (PYTHON_CLIENT / "api/default/download_artifact_job_result.py").exists()


def test_admin_schema_is_secret_free() -> None:
    schema = json.loads(OPENAPI.read_text())
    assert set(schema["paths"]) >= {
        "/api/v1/agents",
        "/api/v1/endpoints/{alias}",
        "/api/v1/fleet",
        "/api/v1/fleet/stream",
        "/api/v1/jobs/{job_id}",
        "/api/v1/jobs/{job_id}/logs",
        "/api/v1/jobs/{job_id}/resume",
        "/api/v1/nodes/status",
        "/api/v1/nodes/{node_id}/telemetry",
    }
    assert all(path.startswith("/api/v1/") for path in schema["paths"])
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
    assert operations["streamFleetEvents"]["security"] == [{"BrowserSession": []}]
    assert all(
        operation["security"] == [{"BearerAuth": []}]
        for operation_id, operation in operations.items()
        if operation_id
        not in {
            "getBrowserSession",
            "loginBrowser",
            "logoutBrowser",
            "streamFleetEvents",
        }
    )

    retired_prefixes = ("/api/v1/" + "packages", "/api/v1/" + "deployments")
    assert not any(
        path.startswith(prefix)
        for path in schema["paths"]
        for prefix in retired_prefixes
    )

    by_id = {operation["operationId"]: operation for operation in operation_list}
    for operation_id in (
        "getFleetStatus",
        "getJob",
        "getNodeStatuses",
        "getNodeTelemetryHistory",
        "getPublishedEndpoint",
        "listAgents",
        "listJobLogs",
        "listJobs",
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

    assert by_id["getFleetStatus"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/FleetSnapshot"}
    assert by_id["getNodeStatuses"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/FleetStatusResponse"}
    node_status = schema["components"]["schemas"]["NodeStatus"]
    assert "health_probe_stale" in node_status["required"]
    assert (
        "not aggregate node readiness"
        in node_status["properties"]["health_probe_stale"]["description"]
    )
    assert node_status["properties"]["stale"]["deprecated"] is True
    python_node_status = (PYTHON_CLIENT / "models/node_status.py").read_text()
    assert "health_probe_stale: bool" in python_node_status
    assert 'health_probe_stale = d.pop("health_probe_stale")' in python_node_status
    typescript = TYPESCRIPT_CLIENT.read_text()
    assert "health_probe_stale: boolean;" in typescript

    serialized = json.dumps(schema, sort_keys=True).lower()
    for forbidden in (
        "/agent/v1/",
        "certificate_pem",
        "chain_pem",
        "csr_pem",
        "grant_token",
        "management_address",
        "operation_payload",
        "token_digest",
    ):
        assert forbidden not in serialized


def test_browser_auth_contract_declares_cookie_security_and_fixed_validation() -> None:
    schema = json.loads(OPENAPI.read_text())
    operations = _operations(schema)

    assert operations["loginBrowser"]["security"] == []
    assert operations["getBrowserSession"]["security"] == [{"BrowserSession": []}]
    assert operations["logoutBrowser"]["security"] == [{"BrowserSession": []}]
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


def test_generated_run_preview_contracts_require_digest_bound_alias() -> None:
    schema = json.loads(OPENAPI.read_text())["components"]["schemas"]
    assert "alias" in schema["RunPreviewRequest"]["required"]
    assert "alias" in schema["RunPlanResponse"]["required"]

    from cluster_profiles.generated_control.models.run_preview_request import (
        RunPreviewRequest,
    )

    request = RunPreviewRequest(
        installation_id="00000000-0000-4000-8000-000000000001",
        alias="qwen",
    )
    assert request.to_dict()["alias"] == "qwen"

    typescript = TYPESCRIPT_CLIENT.read_text()
    request_contract = typescript.split("RunPreviewRequest: {", 1)[1].split("};", 1)[0]
    response_contract = typescript.split("RunPlanResponse: {", 1)[1].split("};", 1)[0]
    assert "alias: string;" in request_contract
    assert "alias: string;" in response_contract


def test_generated_library_contract_has_one_recipe_topology_and_strict_identities() -> (
    None
):
    schema = json.loads(OPENAPI.read_text())["components"]["schemas"]
    detail = schema["LibraryRecipeDetail"]

    assert set(detail["properties"]) >= {
        "topology",
        "placement",
        "definition",
        "model_documents",
    }
    assert "model" not in detail["properties"]
    assert "model_document" not in detail["properties"]
    model_documents = detail["properties"]["model_documents"]
    assert model_documents["type"] == "array"
    assert model_documents["items"] == {
        "$ref": "#/components/schemas/LibraryRecipeModel"
    }
    assert model_documents["maxItems"] == 32
    assert set(schema["LibraryRecipeModel"]["properties"]) == {
        "selection",
        "model_document",
    }
    assert schema["LibraryRecipeModel"]["properties"]["selection"] == {
        "$ref": "#/components/schemas/RecipeModelSelection"
    }
    assert schema["LibraryRecipeModel"]["properties"]["model_document"] == {
        "$ref": "#/components/schemas/ModelDefinition"
    }
    assert "profiles" not in detail["properties"]
    assert detail["properties"]["definition"] == {
        "$ref": "#/components/schemas/RecipeDefinition"
    }
    definition = schema["RecipeDefinition"]
    assert set(definition["properties"]) >= {
        "identity",
        "metadata",
        "models",
        "execution",
        "runtime",
        "interfaces",
        "settings",
        "validation",
        "release",
        "provenance",
    }
    assert "VisualRecipeDocument" not in schema


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
    assert "/api/v1/catalog/public-recipes" not in paths
    assert "/api/v1/catalog/imports/public" not in paths
    assert "/api/v1/catalog/imports/recipe-library" not in paths
    assert "listPublicRecipes" not in operations
    assert "previewPublicRecipeImport" not in operations
    assert "importPublicRecipe" not in operations


def test_generated_library_schema_uses_shared_authority_documents() -> None:
    components = json.loads(OPENAPI.read_text())["components"]["schemas"]
    forbidden = (
        "PublicRecipe",
        "LibraryRecipeDefinition",
        "ModelVersion",
        "Qualification",
        "Readiness",
        "RuntimeDistribution",
    )
    assert not any(
        any(token in name for token in forbidden) for name in components
    )
    assert components["LibraryModel"]["properties"]["model_document"] == {
        "$ref": "#/components/schemas/ModelDefinition"
    }
    assert components["LibraryRecipeSummary"]["properties"]["recipe_document"] == {
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


def test_stream_resume_header_is_in_openapi_python_and_typescript_clients() -> None:
    schema = json.loads(OPENAPI.read_text())
    operation = schema["paths"]["/api/v1/fleet/stream"]["get"]
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

    from cluster_profiles.generated_control.api.default import stream_fleet_events

    assert stream_fleet_events._get_kwargs(last_event_id="17")["headers"] == {
        "Last-Event-ID": "17"
    }
    typescript = TYPESCRIPT_CLIENT.read_text()
    assert '"Last-Event-ID"?: string | null;' in typescript


def test_generated_fleet_projection_vocabulary_is_finite() -> None:
    schema = json.loads(OPENAPI.read_text())["components"]["schemas"]
    assert schema["NodeConnection"]["properties"]["certificate_state"]["enum"] == [
        "valid",
        "missing",
        "not-yet-valid",
        "expired",
        "revoked",
        "inactive",
    ]
    offline = schema["NodeConnection"]["properties"]["offline_reason"]
    assert offline["anyOf"][0]["enum"] == [
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
    assert 'certificate_state: "valid" | "missing" | "not-yet-valid"' in typescript
    assert 'degraded_reason?: ("external-member" | "mapping-incomplete"' in typescript


def test_generated_telemetry_contracts_are_concrete_and_versioned() -> None:
    schema = json.loads(OPENAPI.read_text())["components"]["schemas"]

    for name in (
        "TelemetryCapability",
        "TelemetryDetails",
        "TelemetryMetricSummary",
        "TelemetryMetrics",
        "TelemetryPoint",
        "TelemetryProvenance",
        "TelemetryRollupPoint",
        "TelemetryRuntime",
        "TelemetrySeries",
        "TelemetryHistoryMetadata",
        "TelemetryHistoryResponse",
        "TelemetryWorkload",
        "TelemetryCurrentResponse",
        "TelemetryCapabilitiesResponse",
        "TelemetryWorkloadsResponse",
    ):
        assert schema[name]["type"] == "object"
        assert schema[name]["additionalProperties"] is False

    point = schema["TelemetryPoint"]
    assert point["properties"]["details"] == {
        "$ref": "#/components/schemas/TelemetryDetails"
    }
    assert point["properties"]["metrics"]["anyOf"] == [
        {"$ref": "#/components/schemas/TelemetryMetrics"},
        {"type": "null"},
    ]
    history = schema["TelemetryHistoryResponse"]
    assert history["properties"]["points"]["items"]["anyOf"] == [
        {"$ref": "#/components/schemas/TelemetryPoint"},
        {"$ref": "#/components/schemas/TelemetryRollupPoint"},
    ]
    assert history["properties"]["metadata"]["anyOf"] == [
        {"$ref": "#/components/schemas/TelemetryHistoryMetadata"},
        {"type": "null"},
    ]
    assert schema["TelemetryCurrentResponse"]["properties"]["schema_version"][
        "const"
    ] == 2
    assert schema["TelemetryCapabilitiesResponse"]["properties"]["schema_version"][
        "const"
    ] == 2
    assert schema["TelemetryWorkloadsResponse"]["properties"]["schema_version"][
        "const"
    ] == 2

    typescript = TYPESCRIPT_CLIENT.read_text()
    assert 'TelemetryPoint: {' in typescript
    assert 'TelemetryHistoryResponse: {' in typescript
    assert 'TelemetryPoint: {[key: string]: unknown};' not in typescript
    assert 'TelemetryHistoryResponse: {[key: string]: unknown};' not in typescript


def test_generated_telemetry_models_parse_legacy_and_rich_documents() -> None:
    from cluster_profiles.generated_control.models.telemetry_history_response import (
        TelemetryHistoryResponse,
    )
    from cluster_profiles.generated_control.models.telemetry_point import TelemetryPoint

    legacy_document = {
        "id": "00000000-0000-4000-8000-000000000001",
        "node_id": "spk_" + "1" * 32,
        "boot_id": "00000000-0000-4000-8000-000000000002",
        "sequence": 4,
        "observed_at": "2026-09-05T00:00:00Z",
        "received_at": "2026-09-05T00:00:01Z",
        "gap_samples": 0,
        "details": {},
    }
    legacy = TelemetryPoint.from_dict(legacy_document)
    assert legacy.details.to_dict() == {}
    assert "metrics" not in legacy.to_dict()

    rich = TelemetryPoint.from_dict(
        {
            **legacy_document,
            "metrics": {
                "schema_version": 2,
                "series": [
                    {
                        "aggregation": "instant",
                        "freshness_threshold_seconds": 30,
                        "key": "gpu.utilization_percent",
                        "measurement_kind": "measured",
                        "observed_at": "2026-09-05T00:00:00Z",
                        "scope": "accelerator",
                        "source": "fixture",
                        "support_status": "available",
                        "unit": "percent",
                        "value": 75.0,
                    }
                ],
                "capabilities": [],
                "runtimes": [],
                "workloads": [],
                "provenance": {
                    "collector": "fixture",
                    "collector_version": "1",
                },
            },
        }
    )
    assert rich.metrics is not None
    assert rich.metrics.schema_version == 2
    assert rich.metrics.series[0].key == "gpu.utilization_percent"

    history = TelemetryHistoryResponse.from_dict(
        {
            "schema_version": 1,
            "node_id": legacy.node_id,
            "start": "2026-09-05T00:00:00Z",
            "end": "2026-09-05T00:01:00Z",
            "resolution": "raw",
            "maximum_points": 2,
            "points": [rich.to_dict()],
        }
    )
    assert history.schema_version == 1
    assert isinstance(history.points[0], TelemetryPoint)
    assert history.points[0].metrics is not None


def test_generated_python_client_parses_documented_operation_errors() -> None:
    import httpx

    from cluster_profiles.generated_control.client import Client
    from cluster_profiles.generated_control.models.bounded_error_response import (
        BoundedErrorResponse,
    )

    client = Client(base_url="https://control.invalid")
    expected = {
        "get_job_log": (401, 403, 404, 503),
        "get_published_endpoint": (401, 404, 503),
        "resume_job": (401, 403, 404, 409, 503),
    }
    for module_name, status_codes in expected.items():
        module = importlib.import_module(
            "cluster_profiles.generated_control.api.default." + module_name
        )
        for status_code in status_codes:
            parsed = module._parse_response(
                client=client,
                response=httpx.Response(
                    status_code,
                    json={"detail": f"bounded-{status_code}"},
                ),
            )
            assert isinstance(parsed, BoundedErrorResponse)
            assert parsed.detail == f"bounded-{status_code}"


def test_generated_python_list_jobs_preserves_cursor_and_typed_rejection() -> None:
    import httpx

    from cluster_profiles.generated_control.api.default import list_jobs
    from cluster_profiles.generated_control.client import Client
    from cluster_profiles.generated_control.models.bounded_error_response import (
        BoundedErrorResponse,
    )

    cursor = "v1.authenticated.boundary"
    kwargs = list_jobs._get_kwargs(
        cursor=cursor,
        limit=20,
        status="queued",
        target="spk_" + "1" * 32,
    )
    assert kwargs["params"] == {
        "cursor": cursor,
        "limit": 20,
        "status": "queued",
        "target": "spk_" + "1" * 32,
    }

    parsed = list_jobs._parse_response(
        client=Client(base_url="https://control.invalid"),
        response=httpx.Response(
            422,
            json={"detail": "job cursor is invalid"},
        ),
    )
    assert isinstance(parsed, BoundedErrorResponse)
    assert parsed.detail == "job cursor is invalid"
