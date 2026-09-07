import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from vonk_agent_protocol import (
    AgentFailureResult,
    AgentResult,
    FailureDiagnostics,
    RecipeJobRunResult,
    canonical_message,
)

VECTORS = Path(__file__).parents[1] / "src/vonk_agent_protocol/vectors"


def test_failure_diagnostics_current_wire_vector():
    document = json.loads((VECTORS / "failure-diagnostics-v1.json").read_text())
    assert (
        FailureDiagnostics.model_validate(document).model_dump(mode="json") == document
    )
    for invalid in (
        dict(document, schema_version=True),
        dict(document, phase=8),
        dict(document, collected_at="yesterday"),
        dict(document, raw_environment={}),
    ):
        with pytest.raises(ValidationError):
            FailureDiagnostics.model_validate(invalid)


def test_artifact_job_failure_round_trip_retains_typed_diagnostics():
    document = json.loads((VECTORS / "recipe-job-run-result-v1.json").read_text())
    document["state"] = "failed"
    document["result"]["exit_code"] = 1
    document["result"]["reason"] = "runtime failed"
    document["result"]["diagnostics"] = json.loads(
        (VECTORS / "failure-diagnostics-v1.json").read_text()
    )
    result = AgentResult.model_validate(document)
    assert isinstance(result.result, RecipeJobRunResult)
    assert result.result.diagnostics.category == "platform-policy"
    wire = canonical_message(result)
    assert (
        AgentResult.model_validate_json(wire).result.diagnostics
        == result.result.diagnostics
    )


def test_generic_failure_round_trip_and_diagnostic_guard_are_narrow():
    document = json.loads((VECTORS / "recipe-job-run-result-v1.json").read_text())
    document["state"] = "failed"
    document["result"] = {
        "error_code": "recipe_build_failed",
        "status": "failed",
        "diagnostics": json.loads(
            (VECTORS / "failure-diagnostics-v1.json").read_text()
        ),
    }
    result = AgentResult.model_validate(document)
    assert isinstance(result.result, AgentFailureResult)
    assert (
        AgentResult.model_validate_json(canonical_message(result)).result.diagnostics
        == result.result.diagnostics
    )
    document["result"]["reason"] = "/etc/shadow"
    with pytest.raises(ValidationError):
        AgentResult.model_validate(document)


def test_diagnostics_enforce_whole_utf8_byte_budget():
    document = json.loads((VECTORS / "failure-diagnostics-v1.json").read_text())
    document["stdout"]["text"] = "😀" * 2048
    document["stderr"]["text"] = "😀" * 2048
    with pytest.raises(ValidationError, match="16 KiB"):
        FailureDiagnostics.model_validate(document)
