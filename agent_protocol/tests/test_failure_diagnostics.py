import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from vonk_agent_protocol.agent_operations import AgentFailureResult
from vonk_agent_protocol.contracts import AgentResult
from vonk_agent_protocol.failure_evidence import (
    EXIT_CAUSE_TOKENS,
    LOG_TAIL_BYTES,
    FailureDiagnostics,
    FailureLogTail,
    decisive_first,
)
from vonk_agent_protocol.outcome import OutcomeFailed
from vonk_agent_protocol.recipe_jobs import RecipeJobRunResult
from vonk_agent_protocol.wire_model import canonical_message

VECTORS = Path(__file__).parents[1] / "src/vonk_agent_protocol/vectors"


def test_failure_diagnostics_current_wire_vector():
    document = json.loads((VECTORS / "failure-diagnostics-v1.json").read_text())
    assert (
        json.loads(canonical_message(FailureDiagnostics.model_validate(document)))
        == document
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
    assert result.result.diagnostics is not None
    assert result.result.diagnostics.category == "platform-policy"
    wire = canonical_message(result)
    round_trip = AgentResult.model_validate_json(wire).result
    assert isinstance(round_trip, RecipeJobRunResult)
    assert round_trip.diagnostics == result.result.diagnostics


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
    round_trip = AgentResult.model_validate_json(canonical_message(result)).result
    assert isinstance(round_trip, AgentFailureResult)
    assert round_trip.diagnostics == result.result.diagnostics
    # A path in prose is redacted; in the code the Controller acts on, refused.
    document["result"]["reason"] = "/etc/shadow"
    redacted = AgentResult.model_validate(document)
    assert isinstance(redacted.result, AgentFailureResult)
    assert redacted.result.reason == "<host-path>"
    document["result"]["error_code"] = "recipe/build_failed"
    with pytest.raises(ValidationError):
        AgentResult.model_validate(document)


def test_diagnostics_enforce_whole_utf8_byte_budget():
    document = json.loads((VECTORS / "failure-diagnostics-v1.json").read_text())
    document["stdout"]["text"] = "😀" * LOG_TAIL_BYTES
    document["stderr"]["text"] = "😀" * LOG_TAIL_BYTES
    with pytest.raises(ValidationError, match="document bound"):
        FailureDiagnostics.model_validate(document)


def test_a_log_tail_holds_exactly_the_policy_bytes():
    def tail(text: str) -> dict[str, object]:
        return {
            "text": text,
            "truncated": False,
            "dropped_bytes": 0,
            "dropped_lines": 0,
        }

    assert FailureLogTail.model_validate(tail("x" * LOG_TAIL_BYTES))
    with pytest.raises(ValidationError):
        FailureLogTail.model_validate(tail("x" * (LOG_TAIL_BYTES + 1)))


_KEY_ERROR = (
    '  File "/usr/local/lib/python3.12/dist-packages/vllm/model_executor/models/'
    'glm4_moe.py", line 512, in load_weights\n'
    "    param = params_dict[name]\n"
    "KeyError: 'layers.0.self_attn.conv1d.weight'"
)


def _failed_start_with_tail(length: int) -> dict[str, object]:
    document = json.loads(
        (VECTORS / "recipe-start-workload-exited-v1.json").read_text()
    )
    progress = "Loading safetensors checkpoint shards:   0% Completed | 0/48 [00:08<?, ?it/s]\r"
    diagnostics = document["result"]["evidence"]["diagnostics"]
    diagnostics["stderr"]["text"] = (progress * length)[: length - len(_KEY_ERROR)] + (
        _KEY_ERROR
    )
    diagnostics["stdout"]["text"] = "loading weights from /outputs/cache/home"
    return document


def test_a_traceback_tail_up_to_the_policy_bound_is_kept():
    """Wrong implementation caught: a path-rule copy of the tail bound (2048)
    dropped every traceback longer than 2048 characters after the policy
    became 16384."""

    kept = AgentResult.model_validate(_failed_start_with_tail(LOG_TAIL_BYTES))
    assert kept.evidence_drops == ()
    assert isinstance(kept.result, OutcomeFailed)
    assert kept.result.evidence is not None
    diagnostics = kept.result.evidence.diagnostics
    assert diagnostics is not None
    assert len(diagnostics.stderr.text) == LOG_TAIL_BYTES
    assert diagnostics.stderr.text.endswith(
        "KeyError: 'layers.0.self_attn.conv1d.weight'"
    )

    over = AgentResult.model_validate(_failed_start_with_tail(LOG_TAIL_BYTES + 1))
    assert [drop.location for drop in over.evidence_drops] == [
        "result.evidence.diagnostics.stderr.text"
    ]
    assert isinstance(over.result, OutcomeFailed)
    assert over.result.evidence is not None
    assert isinstance(over.result, OutcomeFailed)
    assert over.result.evidence is not None
    assert over.result.evidence.diagnostics is None


def _exited_with(cause: str) -> FailureDiagnostics:
    document = json.loads(
        (VECTORS / "recipe-start-workload-exited-v1.json").read_text()
    )["result"]["evidence"]["diagnostics"]
    return FailureDiagnostics.model_validate(
        {**document, "preflight": [{"name": "exit_cause", "value": cause}]}
    )


def test_a_killed_from_outside_exit_comes_first_and_the_rest_keep_their_order():
    ranks = {
        "entrypoint": _exited_with(EXIT_CAUSE_TOKENS["PythonException"]),
        "worker": _exited_with(EXIT_CAUSE_TOKENS["HostMemoryExhausted"]),
        "other": _exited_with("a_cause_a_newer_agent_adds"),
    }
    ordered = decisive_first(ranks, ranks.__getitem__)
    assert ordered == ["worker", "entrypoint", "other"]
    assert ranks["other"].exit_cause is None
    assert decisive_first(["a", "b"], lambda _: None) == ["a", "b"]
