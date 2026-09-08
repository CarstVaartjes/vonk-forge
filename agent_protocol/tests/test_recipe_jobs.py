from __future__ import annotations

import json
from pathlib import Path

import pytest
from vonk_agent_protocol import (
    AgentClaim,
    AgentProtocolError,
    AgentResult,
    RecipeJobRunRequest,
    RecipeJobRunResult,
    canonical_message,
    validate_result_for_operation,
    validate_schema_message,
)

VECTORS = Path(__file__).parents[1] / "src/vonk_agent_protocol/vectors"


def documents() -> tuple[dict[str, object], dict[str, object]]:
    return (
        json.loads((VECTORS / "recipe-job-run-claim-v1.json").read_text()),
        json.loads((VECTORS / "recipe-job-run-result-v1.json").read_text()),
    )


def test_recipe_job_vectors_are_canonical_typed_and_digest_bound() -> None:
    claim_document, result_document = documents()
    claim = AgentClaim.parse(claim_document)
    request = RecipeJobRunRequest.parse(claim.payload)
    result = AgentResult.parse(result_document)
    job_result = RecipeJobRunResult.parse(result.result)

    assert canonical_message(claim).decode() == json.dumps(
        claim_document, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    assert request.reserved_memory_bytes == 32 * 1024**3
    assert request.compiled_execution_plan.job.interface == request.interface
    assert request.output_mappings[0].to_mapping() == {
        "slot": "image",
        "media_type": "image/png",
        "extensions": [".png"],
    }
    assert job_result.output_manifest_sha256 == (
        "9f7781fb8415bc1cb9e835fe4bcc9c8dd8f45f6a6b333f0e0550e812db1da9cd"
    )
    assert (
        validate_schema_message("recipe-job-run.schema.json", claim_document["payload"])
        == request
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["inputs"][0].update(name="../escape"),
        lambda value: value.update(input_total_bytes=4),
        lambda value: value.update(reserved_memory_bytes=0),
        lambda value: value["compiled_execution_plan"]["runtime"].update(argv=["bad\x00value"]),
        lambda value: value["output_limits"].update(
            allowed_media_types=["image/png", "image/png"]
        ),
        lambda value: value["output_mappings"][0].update(extensions=[".PNG"]),
        lambda value: value["output_mappings"].append(
            {
                "slot": "receipt",
                "media_type": "application/json",
                "extensions": [".png"],
            }
        ),
        lambda value: value["output_limits"].update(
            allowed_media_types=["application/pdf"]
        ),
    ],
)
def test_recipe_job_request_rejects_traversal_digest_drift_and_unsafe_values(
    mutation,
) -> None:
    claim_document, _result_document = documents()
    payload = claim_document["payload"]
    mutation(payload)
    with pytest.raises(AgentProtocolError):
        RecipeJobRunRequest.parse(payload)


@pytest.mark.parametrize("field", ["compiled_execution_plan", "parameters"])
def test_recipe_job_requires_only_the_canonical_invocation(field: str) -> None:
    claim_document, _ = documents()
    payload = claim_document["payload"]
    if field == "compiled_execution_plan":
        payload.pop(field)
    else:
        payload[field] = {"seed": 999}
    with pytest.raises(AgentProtocolError):
        RecipeJobRunRequest.parse(payload)


@pytest.mark.parametrize(
    ("media_type", "extension"),
    [
        ("application/pdf", ".pdf"),
        ("image/avif", ".avif"),
        ("application/vnd.example.custom", ".vonk"),
    ],
)
def test_recipe_job_request_preserves_exact_custom_output_mapping(
    media_type: str, extension: str
) -> None:
    claim_document, _result_document = documents()
    payload = claim_document["payload"]
    payload["output_mappings"] = [
        {
            "slot": "artifact",
            "media_type": media_type,
            "extensions": [extension],
        }
    ]
    payload["output_limits"]["allowed_media_types"] = [media_type]

    parsed = RecipeJobRunRequest.parse(payload)

    assert parsed.output_mappings[0].to_mapping() == payload["output_mappings"][0]


def test_recipe_job_request_accepts_unambiguous_longest_suffix_mapping() -> None:
    claim_document, _result_document = documents()
    payload = claim_document["payload"]
    media_type = "application/vnd.example.custom"
    payload["output_mappings"] = [
        {"slot": "binary", "media_type": media_type, "extensions": [".bin"]},
        {
            "slot": "detailed",
            "media_type": media_type,
            "extensions": [".vonk.bin"],
        },
    ]
    payload["output_limits"]["allowed_media_types"] = [media_type]

    parsed = RecipeJobRunRequest.parse(payload)

    assert [item.slot for item in parsed.output_mappings] == ["binary", "detailed"]


def test_recipe_job_result_rejects_manifest_drift() -> None:
    _claim_document, result_document = documents()
    result = result_document["result"]
    result["output_manifest"]["files"][0]["size_bytes"] = 5
    with pytest.raises(AgentProtocolError, match="manifest"):
        RecipeJobRunResult.parse(result)


def test_failed_process_result_is_valid_only_for_its_current_operation() -> None:
    _, envelope = documents()
    result = dict(envelope["result"], exit_code=1, reason="runtime failed")
    assert validate_result_for_operation("recipe.job.run.v1", result, state="failed").exit_code == 1
    with pytest.raises(AgentProtocolError, match="typed model"):
        validate_result_for_operation("recipe.stop", result, state="failed")
    with pytest.raises(AgentProtocolError, match="typed model"):
        validate_result_for_operation("recipe.job.run.v1", envelope["result"], state="failed")
