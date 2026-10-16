"""A receipted ending's stage and cause are stored typed beside the receipt.

The agent reports them as the outcome's evidence next to the receipt (the
shared vector the Rust agent produces); the stored body stays the receipt and
carries them as ``ending``. Receipts without one, from older agents and older
rows, read as before.
"""

from __future__ import annotations

import json
from pathlib import Path

from vonk_agent_protocol import (
    AgentResult,
    OutcomeUnknown,
    RecipeJobEnding,
    RecipeJobRunResult,
    outcome_body,
    validate_result_for_operation,
)

VECTORS = Path(__file__).parents[1] / "src/vonk_agent_protocol/vectors"


def _vector(name: str) -> dict[str, object]:
    return json.loads((VECTORS / name).read_text())


def test_a_receipted_unknown_stores_its_stage_and_cause_typed() -> None:
    message = AgentResult.model_validate(
        _vector("recipe-job-run-unknown-receipt-v1.json")
    )
    assert isinstance(message.result, OutcomeUnknown)

    stored = outcome_body(message.result)

    assert isinstance(stored, RecipeJobRunResult)
    assert stored.reason == "job output delivery remains unobserved"
    assert stored.ending == RecipeJobEnding(
        stage="job-state", diagnostic="artifact output upload was refused: HTTP 409"
    )
    # The stored row reads back under its failed order, ending included.
    read = validate_result_for_operation(
        "recipe.job.run.v1", stored.to_mapping(), state="failed"
    )
    assert isinstance(read, RecipeJobRunResult)
    assert read.ending == stored.ending


def test_a_receipt_without_an_ending_reads_as_before() -> None:
    receipt = _vector("recipe-job-run-result-v1.json")["result"]
    assert isinstance(receipt, dict) and "ending" not in receipt
    read = validate_result_for_operation(
        "recipe.job.run.v1", receipt, state="succeeded"
    )
    assert isinstance(read, RecipeJobRunResult)
    assert read.ending is None
