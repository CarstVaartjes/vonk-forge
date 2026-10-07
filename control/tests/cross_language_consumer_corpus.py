"""Raw documents shared by real consumers; schemas always come from canonical models.

Cases preserve numeric spelling as text. Acceptance is computed by the owning
Pydantic JSON consumer, never by a duplicated test parser or field list.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ValidationError
from vonk_agent_protocol import (
    AgentProgress,
    AgentResult,
    OperationProgress,
    RecipeStartPayload,
)
from vonk_agent_protocol.build_import import RecipeBuildEnvironmentArgument
from vonk_agent_protocol.failure_evidence import FailureDiagnostics
from vonk_control.fleet_profile_contract import (
    FleetProfileDefinitionView,
    FleetProfileInput,
)
from vonk_control.operation_api import (
    BoundedErrorResponse,
    JobDetailResponse,
    RequestValidationProblem,
)
from vonk_forge_contracts.recipe import RecipeRuntimeEnvironment, RecipeSetting

NODE = "spk_" + "1" * 32
FENCE = "11111111-1111-4111-8111-111111111111"
JOB = "22222222-2222-4222-8222-222222222222"
OPERATION = "33333333-3333-4333-8333-333333333333"
MODELS: dict[str, type[BaseModel]] = {
    "FailureDiagnostics": FailureDiagnostics,
    "FleetProfileInput": FleetProfileInput,
    "FleetProfileDefinitionView": FleetProfileDefinitionView,
    "RecipeStartPayload": RecipeStartPayload,
    "RecipeSetting": RecipeSetting,
    "RecipeRuntimeEnvironment": RecipeRuntimeEnvironment,
    "AgentResult": AgentResult,
    "AgentProgress": AgentProgress,
    "RecipeBuildEnvironmentArgument": RecipeBuildEnvironmentArgument,
    "OperationProgress": OperationProgress,
    "BoundedErrorResponse": BoundedErrorResponse,
    "RequestValidationProblem": RequestValidationProblem,
    "JobDetailResponse": JobDetailResponse,
}


def diagnostic() -> dict:
    return {
        "schema_version": 1,
        "collected_at": "2026-10-07T01:00:00+00:00",
        "phase": "stop",
        "category": "runtime",
        "stdout": {
            "text": "runtime exited",
            "truncated": False,
            "dropped_bytes": None,
            "dropped_lines": None,
        },
        "stderr": {
            "text": "",
            "truncated": False,
            "dropped_bytes": None,
            "dropped_lines": None,
        },
        "versions": [],
        "sandbox": [],
        "storage": [],
        "preflight": [],
        "collector_errors": [],
    }


def agent_envelope(leaf: str) -> str:
    return json.dumps(
        {
            "fence": FENCE,
            "state": "failed",
            "result": {
                "kind": "failed",
                "code": "recipe_stop_failed",
                "reason": "runtime stop failed",
                "evidence": {"diagnostics": "__LEAF__"},
            },
        },
        separators=(",", ":"),
    ).replace('"__LEAF__"', leaf)


def job_envelope(leaf: str) -> str:
    document = {
        "id": JOB,
        "state": "failed",
        "kind": "recipe.stop",
        "authority_revision": "a" * 64,
        "targets": [NODE],
        "target_total": 1,
        "current_attempt": 1,
        "operations": [
            {
                "id": OPERATION,
                "node_id": NODE,
                "kind": "recipe.stop",
                "state": "failed",
                "attempt": 1,
                "failure": {
                    "status": "failed",
                    "error_code": "recipe_stop_failed",
                    "reason": "runtime stop failed",
                    "diagnostics": "__LEAF__",
                },
            }
        ],
        "operation_total": 1,
        "progress": None,
        "projection_issue": None,
    }
    return json.dumps(document, separators=(",", ":")).replace('"__LEAF__"', leaf)


def corpus() -> dict:
    cases = []

    def add(
        name: str, component: str, text: str, *, consumers: list[str] | None = None
    ) -> None:
        model = MODELS[component]
        try:
            value = model.model_validate_json(text)
            normalized = value.model_dump_json()
            accepted = True
        except (ValueError, ValidationError):
            normalized, accepted = None, False
        cases.append(
            {
                "id": name,
                "component": component,
                "text": text,
                "accepted": accepted,
                "normalized_text": normalized,
                "consumers": consumers
                or (
                    ["python", "rust"]
                    if component == "AgentResult"
                    else ["python", "rust", "browser"]
                ),
            }
        )

    # Neighbors come from the canonical owner schema, not another bound table.
    profile_documents = (
        ("FleetProfileInput", "expected_revision", {}, ["python", "browser"]),
        (
            "FleetProfileDefinitionView",
            "number",
            {"id": None, "revision": 0, "definition": {}},
            ["python", "browser"],
        ),
    )
    start_document = {
        "run_id": JOB,
        "installation_id": OPERATION,
        "recipe_revision_id": FENCE,
        "mapping_id": JOB,
        "plan_digest": "a" * 64,
        "compiled_execution_plan": json.loads(
            (Path(__file__).parent / "fixtures/compiled_workload_v2.json").read_text()
        ),
    }
    for component, field, document, consumers in (
        *profile_documents,
        ("RecipeStartPayload", "run_generation", start_document, ["python", "rust"]),
    ):
        schema = MODELS[component].model_json_schema()["properties"][field]
        for edge in ("minimum", "maximum"):
            boundary = schema[edge]
            for offset in (-1, 0, 1):
                add(
                    f"owner-bound-{component}-{field}-{edge}-{offset:+d}",
                    component,
                    json.dumps({**document, field: boundary + offset}),
                    consumers=consumers,
                )

    add(
        "start-generation-above-u32",
        "RecipeStartPayload",
        json.dumps({**start_document, "run_generation": 2**32 + 1}),
        consumers=["python", "rust"],
    )

    for component, fields in (
        ("RecipeSetting", {"change_effect": "restart"}),
        ("RecipeRuntimeEnvironment", {"name": "VONK_SCALAR"}),
    ):
        for token in ("1000.0", "-0", "-0.0", "1.00000000000000001", "-1e-400"):
            add(
                f"float-kind-{component}-{token}",
                component,
                json.dumps({**fields, "value": "__NUMBER__"}).replace(
                    '"__NUMBER__"', token
                ),
                consumers=["python", "browser"],
            )
    spoof = {"$serde_json::private::Number": "2"}
    diagnostic_spoof = diagnostic()
    diagnostic_spoof["stdout"]["dropped_bytes"] = spoof
    add(
        "private-number-object-FailureDiagnostics",
        "FailureDiagnostics",
        json.dumps(diagnostic_spoof),
    )
    add(
        "private-number-object-OperationProgress",
        "OperationProgress",
        json.dumps({"phase": "transfer", "completed_bytes": spoof}),
    )

    base = diagnostic()
    add("diagnostics-producer", "FailureDiagnostics", json.dumps(base))
    for token in (
        "0",
        "-1",
        "9007199254740990",
        "9007199254740991",
        "9007199254740992",
        "9007199254740993",
        "18446744073709551614",
        "18446744073709551615",
        "18446744073709551616",
        "1.0",
        "1e0",
        "true",
        "null",
    ):
        document = diagnostic()
        document["stdout"]["dropped_bytes"] = "__NUMBER__"
        raw = json.dumps(document).replace('"__NUMBER__"', token)
        add("diagnostics-dropped-bytes-" + token, "FailureDiagnostics", raw)
    for mutation in (
        "missing-phase",
        "extra-field",
        "null-required",
        "missing-default",
        "bad-enum",
        "oversize-text",
        "oversize-document",
    ):
        document = diagnostic()
        if mutation == "missing-phase":
            document.pop("phase")
        elif mutation == "extra-field":
            document["unexpected"] = 1
        elif mutation == "null-required":
            document["phase"] = None
        elif mutation == "missing-default":
            document.pop("schema_version")
        elif mutation == "bad-enum":
            document["category"] = "made-up"
        elif mutation == "oversize-text":
            document["stdout"]["text"] = "x" * 2049
        else:
            for key, count in (
                ("versions", 8),
                ("sandbox", 12),
                ("storage", 8),
                ("preflight", 8),
            ):
                document[key] = [
                    {"name": "x" * 64, "value": "y" * 256} for _ in range(count)
                ]
            document["stdout"]["text"] = document["stderr"]["text"] = "z" * 2048
            document["collector_errors"] = ["e" * 256] * 8
        add(
            "diagnostics-" + mutation,
            "FailureDiagnostics",
            json.dumps(document),
            consumers=["python", "rust"] if mutation == "oversize-document" else None,
        )
    leaf = json.dumps(base, separators=(",", ":"))
    add("agent-result-producer", "AgentResult", agent_envelope(leaf))
    add(
        "agent-result-unknown-tag",
        "AgentResult",
        agent_envelope(leaf).replace('"kind":"failed"', '"kind":"invented"'),
    )
    add(
        "job-diagnostics-producer",
        "JobDetailResponse",
        job_envelope(leaf),
        consumers=["python", "cli", "generated-http", "browser"],
    )
    numeric_leaf = diagnostic()
    numeric_leaf["stdout"]["dropped_bytes"] = 9007199254740993
    leaf_text = json.dumps(numeric_leaf, separators=(",", ":"))
    add("agent-diagnostics-numeric-envelope", "AgentResult", agent_envelope(leaf_text))
    add(
        "job-diagnostics-numeric-envelope",
        "JobDetailResponse",
        job_envelope(leaf_text),
        consumers=["python", "cli", "generated-http", "browser"],
    )
    for token in (
        "0",
        "-0",
        "-1",
        "9007199254740993",
        "18446744073709551616",
        "1.0",
        "1e0",
        "true",
        "1e400",
        "9" * 200,
        "9" * 5001,
    ):
        raw = '{"phase":"transfer","completed_bytes":' + token + "}"
        add(
            "progress-completed-"
            + (token if len(token) < 30 else str(len(token)) + "-digits"),
            "OperationProgress",
            raw,
            consumers=["python"]
            if len(token) > 4096
            else ["python", "rust", "browser"],
        )
    for token in (
        "0",
        "-1",
        "1e3",
        "1e400",
        "-1e-400",
        "1.00000000000000001",
        "1000000000000000.01",
        "999999999999999",
        "1000000000000000",
        "1000000000000001",
    ):
        add(
            "progress-rate-" + token,
            "OperationProgress",
            '{"phase":"transfer","bytes_per_second":' + token + "}",
            consumers=["python", "rust", "browser"],
        )
    for token in ("18446744073709551617", "9" * 200):
        add(
            "heartbeat-progress-" + (token if len(token) < 30 else "200-digits"),
            "AgentProgress",
            '{"fence":"'
            + FENCE
            + '","progress":{"phase":"transfer","completed_bytes":'
            + token
            + "}}",
            consumers=["python", "rust"],
        )
    for token in (
        "9223372036854775808",
        "18446744073709551617",
        "9" * 200,
        "-" + "9" * 200,
        "1.0",
        "1e0",
        "null",
        "true",
    ):
        add(
            "build-environment-"
            + (
                token
                if len(token) < 30
                else ("negative-" if token.startswith("-") else "") + "200-digits"
            ),
            "RecipeBuildEnvironmentArgument",
            '{"name":"VONK_COUNT","value":' + token + "}",
            consumers=["python", "rust"],
        )
    for status, component in (
        (503, "BoundedErrorResponse"),
        (422, "RequestValidationProblem"),
    ):
        document = {
            "detail": "request refused",
            "context": {
                "operation": "agent.result",
                "http_status": status,
                "code": "controller.unavailable"
                if status == 503
                else "controller.invalid_request",
                "source": "remote_rejection",
                "decision": "defer",
            },
        }
        if status == 422:
            document["issues"] = [
                {
                    "type": "int_type",
                    "loc": ["body", "attempt", 9007199254740993],
                    "msg": "integer required",
                }
            ]
        add("error-" + str(status), component, json.dumps(document))
    for token in ("18446744073709551617", "9" * 200, "1.0", "1e0", "true", "null"):
        document = {
            "detail": "request refused",
            "context": {
                "operation": "agent.result",
                "http_status": 422,
                "code": "controller.invalid_request",
                "source": "remote_rejection",
                "decision": "defer",
            },
            "issues": [
                {
                    "type": "int_type",
                    "loc": ["body", "attempt", "__NUMBER__"],
                    "msg": "integer required",
                }
            ],
        }
        add(
            "error-422-loc-" + (token if len(token) < 30 else "200-digits"),
            "RequestValidationProblem",
            json.dumps(document).replace('"__NUMBER__"', token),
        )
    rejected_location = json.loads(
        next(case["text"] for case in cases if case["id"] == "error-422")
    )
    rejected_location["issues"][0]["loc"][2] = {"$serde_json::private::Number": "2"}
    add(
        "error-422-loc-private-number-object",
        "RequestValidationProblem",
        json.dumps(rejected_location),
    )
    for case in cases:
        if case["component"] == "JobDetailResponse":
            case["http"] = {
                "method": "GET",
                "path": f"/api/jobs/{JOB}",
                "status": 200,
                "text": case["text"],
            }
            case["browserGeneratedMethod"] = "job"
        elif case["id"].startswith("error-"):
            case["http"] = {
                "method": "GET",
                "path": f"/api/jobs/{JOB}",
                "status": int(case["id"].split("-")[1]),
                "text": case["text"],
            }
    return {
        "version": 1,
        "cases": cases,
        "nonoverlap": {
            "OperationProgress": "Rust agent protocol/heartbeat consumer covered; no Rust Controller API response parser is claimed.",
            "FleetProfileInput": "Canonical input component validation only; no browser HTTP write or Rust API parser is claimed.",
            "FleetProfileDefinitionView": "Canonical projection component validation only; no invented route or Rust parser is claimed.",
            "RecipeSetting": "Canonical scalar component; browser export returns to the actual Python recipe reader, no Rust or HTTP route is claimed.",
            "RecipeRuntimeEnvironment": "Canonical scalar component; browser export returns to the actual Python runtime-environment reader, no Rust or HTTP route is claimed.",
            "RecipeStartPayload": "Actual generated Rust Agent payload decoder; no browser route exists.",
            "CompiledExecutionPlan": "Agent/helper wire only; no browser route exists.",
            "RecipeStopPayload": "Agent/helper wire only; no browser route exists.",
            "RecipeBuildEnvironmentArgument": "Actual Rust generated request scalar decoder; real builder rendering covered by owning Rust tests, no browser route.",
        },
        "semantics": {
            "integer_tokens": "1.0/1e0 are mathematical JSON Schema integers but strict wire consumers reject them.",
            "float_tokens": "Owning Python float fields normalize finite tokens to IEEE754 doubles before bounds checks; structural mathematical schema mode is separate.",
            "python_digits": "Default canonical Pydantic JSON ingress limit applies; no process-global digit limit is disabled.",
            "resource_bounds": "Canonical validator byte/length limits remain stricter than structural schema when not expressible in JSON Schema.",
        },
    }


def write_corpus(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "corpus.json"
    destination.write_text(json.dumps(corpus(), indent=2) + "\n")
    schemas = {
        name: model.model_json_schema(mode="validation")
        for name, model in MODELS.items()
    }
    (directory / "schemas.json").write_text(json.dumps(schemas, indent=2) + "\n")
    return destination


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    print(write_corpus(arguments.output))
