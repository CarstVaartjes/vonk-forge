from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from vonk_agent_protocol import (
    AgentClaim,
    AgentProtocolError,
    ArtifactDistributionPayload,
    OperationProgress,
    RecipeJobEvidence,
    canonical_message,
)


def _claim(**overrides: object) -> dict[str, object]:
    return {
        "fence": "00000000-0000-4000-8000-000000000003",
        "operation": "artifact.distribution.v1",
        "payload": {"plan_digest": "a" * 64},
        "deadline": "2026-08-03T12:00:00+00:00",
        **overrides,
    }


def test_claim_rejects_unknown_top_level_fields_and_roundtrips_json() -> None:
    raw = _claim()
    with pytest.raises(AgentProtocolError):
        AgentClaim.parse(raw | {"unexpected": 1})
    parsed = AgentClaim.parse(raw)
    assert json.loads(canonical_message(parsed)) == raw | {
        "deadline": "2026-08-03T12:00:00Z"
    }


def test_distribution_payload_is_typed_and_plan_bound() -> None:
    payload = {"plan_digest": "a" * 64}
    parsed = ArtifactDistributionPayload.model_validate(payload)
    assert parsed.plan_digest == "a" * 64
    with pytest.raises(ValidationError):
        ArtifactDistributionPayload.model_validate(payload | {"extra": True})


def test_progress_requires_explicit_total_pair_and_has_phase_only_defaults() -> None:
    phase_only = OperationProgress.model_validate({"phase": "probe"})
    assert phase_only.completed_bytes == 0
    assert phase_only.total_bytes is None
    with pytest.raises(ValidationError):
        OperationProgress.model_validate({"phase": "copy", "total_bytes": 4})
    complete = OperationProgress.model_validate(
        {"phase": "copy", "total_bytes": 4, "total_bytes_known": True}
    )
    assert complete.total_bytes == 4


def test_required_nullable_evidence_field_is_not_optional() -> None:
    with pytest.raises(ValidationError):
        RecipeJobEvidence.model_validate({"elapsed_milliseconds": 1})
    evidence = RecipeJobEvidence.model_validate(
        {"elapsed_milliseconds": 1, "peak_memory_bytes": None}
    )
    assert evidence.peak_memory_bytes is None
