from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "scripts/generate-control-clients"
OPENAPI = ROOT / "control/openapi.json"
PYTHON_CLIENT = ROOT / "src/cluster_profiles/generated_control"
TYPESCRIPT_CLIENT = ROOT / "control/web/src/api/generated.d.ts"


def _generate() -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "PYTHONHASHSEED": "0", "SOURCE_DATE_EPOCH": "0"}
    return subprocess.run(
        [os.fspath(GENERATOR)],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _digests() -> dict[str, str]:
    artifacts = [OPENAPI, TYPESCRIPT_CLIENT]
    artifacts.extend(
        path
        for path in PYTHON_CLIENT.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )
    return {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in artifacts
    }


def _operations(schema: dict[str, object]) -> dict[str, dict[str, object]]:
    return {
        operation["operationId"]: operation
        for path in schema["paths"].values()
        for method, operation in path.items()
        if method in {"delete", "get", "patch", "post", "put"}
    }


def test_tracked_admin_contract_has_secret_free_decisions_and_typed_errors() -> None:
    schema = json.loads(OPENAPI.read_text())
    operations = _operations(schema)
    successes = {
        "approveAgentEnrollment": ("200", "EnrollmentDecisionResponse"),
        "createEnrollmentGrant": ("201", "EnrollmentGrantResponse"),
        "listAgentEnrollments": ("200", "EnrollmentListResponse"),
        "rejectAgentEnrollment": ("200", "EnrollmentDecisionResponse"),
    }
    for operation_id, (status_code, component) in successes.items():
        response_schema = operations[operation_id]["responses"][status_code][
            "content"
        ]["application/json"]["schema"]
        assert response_schema == {
            "$ref": f"#/components/schemas/{component}"
        }
        assert schema["components"]["schemas"][component][
            "additionalProperties"
        ] is False
    decision = schema["components"]["schemas"]["EnrollmentDecisionResponse"]
    assert set(decision["properties"]) == {"id", "node_id", "state"}

    expected_errors = {
        "approveAgentEnrollment": {"401", "403", "409", "503"},
        "getJobLog": {"401", "403", "404", "503"},
        "getPublishedEndpoint": {"401", "404", "503"},
        "resumeJob": {"401", "403", "404", "409", "503"},
    }
    for operation_id, statuses in expected_errors.items():
        for status_code in statuses:
            assert operations[operation_id]["responses"][status_code]["content"][
                "application/json"
            ]["schema"] == {
                "$ref": "#/components/schemas/BoundedErrorResponse"
            }
    bounded_error = schema["components"]["schemas"]["BoundedErrorResponse"]
    assert bounded_error["additionalProperties"] is False
    assert bounded_error["properties"]["detail"]["maxLength"] == 256

    progress = schema["components"]["schemas"]["JobOperationResponse"][
        "properties"
    ]["progress"]
    assert any(
        option.get("$ref") == "#/components/schemas/JobOperationProgress"
        for option in progress["anyOf"]
    )

    serialized = json.dumps(schema, sort_keys=True).lower()
    assert "certificate_pem" not in serialized
    assert "chain_pem" not in serialized


def test_library_contract_uses_exact_model_versions_and_v1_revisions() -> None:
    schema = json.loads(OPENAPI.read_text())
    components = schema["components"]["schemas"]
    library_model = components["LibraryModel"]
    assert set(library_model["properties"]) == {"model", "page_local", "recipes"}
    assert library_model["properties"]["model"] == {
        "$ref": "#/components/schemas/ModelVersionIdentity"
    }
    model_identity = components["ModelVersionIdentity"]
    assert model_identity["properties"]["kind"]["const"] == "model-version"
    assert set(model_identity["required"]) == {
        "kind",
        "publisher",
        "slug",
        "content_sha256",
    }
    assert components["RecipeRevisionSummary"]["properties"]["schema_version"][
        "const"
    ] == 1

    typescript = TYPESCRIPT_CLIENT.read_text()
    assert 'model: components["schemas"]["ModelVersionIdentity"];' in typescript
    assert "schema_version: 1;" in typescript
    python_client = (PYTHON_CLIENT / "models/recipe_revision_summary.py").read_text()
    assert "schema_version: Union[Literal[1], Unset] = 1" in python_client


def test_generator_is_idempotent_and_admin_schema_is_secret_free() -> None:
    tracked_digests = _digests()
    first = _generate()
    assert first.returncode == 0, first.stderr
    first_digests = _digests()
    assert first_digests == tracked_digests, "generated clients or OpenAPI drifted"
    second = _generate()
    assert second.returncode == 0, second.stderr
    assert _digests() == first_digests

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
    assert operations["streamFleetEvents"]["security"] == [
        {"BrowserSession": []}
    ]
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

    legacy_prefixes = ("/api/v1/" + "packages", "/api/v1/" + "deployments")
    assert not any(
        path.startswith(prefix)
        for path in schema["paths"]
        for prefix in legacy_prefixes
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

    assert by_id["getFleetStatus"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/FleetSnapshot"}
    assert by_id["getNodeStatuses"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/FleetStatusResponse"}

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
    assert operations["getBrowserSession"]["security"] == [
        {"BrowserSession": []}
    ]
    assert operations["logoutBrowser"]["security"] == [{"BrowserSession": []}]
    assert operations["loginBrowser"]["responses"]["422"]["content"][
        "application/json"
    ]["schema"] == {
        "$ref": "#/components/schemas/LoginRequestInvalid"
    }
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
    generated = _generate()
    assert generated.returncode == 0, generated.stderr
    bytecode_before = set(PYTHON_CLIENT.rglob("*.pyc"))
    result = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            "control",
            "--frozen",
            "python",
            "-c",
            (
                "from pathlib import Path; root=Path('src/cluster_profiles/generated_control'); "
                "[(compile(path.read_text(), str(path), 'exec')) for path in root.rglob('*.py')]"
            ),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert set(PYTHON_CLIENT.rglob("*.pyc")) == bytecode_before


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


def test_generated_library_contract_has_one_recipe_topology_and_strict_identities() -> None:
    schema = json.loads(OPENAPI.read_text())["components"]["schemas"]
    detail = schema["LibraryRecipeDetail"]
    visual = schema["VisualRecipeDocument"]

    assert set(detail["properties"]) >= {"topology", "placement", "visual_recipe"}
    assert "profiles" not in detail["properties"]
    assert set(visual["properties"]) >= {"model", "execution", "runtime", "interfaces"}
    assert "workload" not in visual["properties"]
    assert "adapter" not in schema["VisualRuntime"]["properties"]

    typescript = TYPESCRIPT_CLIENT.read_text()
    mapping_contract = typescript.split("MappingPreviewInput: {", 1)[1].split("};", 1)[0]
    detail_contract = typescript.split("LibraryRecipeDetail: {", 1)[1].split("};", 1)[0]
    runtime_contract = typescript.split("VisualRuntime: {", 1)[1].split("};", 1)[0]
    assert "topology_name" not in mapping_contract
    assert "topology:" in detail_contract and "profiles:" not in detail_contract
    assert "distribution:" in runtime_contract and "adapter:" not in runtime_contract


def test_generated_python_client_imports_in_the_root_locked_environment() -> None:
    result = subprocess.run(
        [
            "uv",
            "run",
            "--frozen",
            "python",
            "-c",
            "from cluster_profiles.generated_control.client import AuthenticatedClient",
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


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
