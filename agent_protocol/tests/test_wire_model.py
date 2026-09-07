from __future__ import annotations

import hashlib
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
    payload = {
        "schema_version": 1,
        "authority_revision": "a" * 64,
        "plan_digest": "a" * 64,
    }
    return {
        "schema_version": 1,
        "job_id": "00000000-0000-4000-8000-000000000001",
        "operation_id": "00000000-0000-4000-8000-000000000002",
        "attempt": 1,
        "fence": "00000000-0000-4000-8000-000000000003",
        "node_id": "spk_00000000000000000000000000000001",
        "operation": "artifact.distribution.v1",
        "authority_revision": "a" * 64,
        "payload_digest": hashlib.sha256(canonical_message(payload)).hexdigest(),
        "payload": payload,
        "deadline": "2026-08-03T12:00:00+00:00",
        **overrides,
    }


@pytest.mark.parametrize("value", [True, 1.0])
def test_schema_version_rejects_boolean_and_float_tags(value: object) -> None:
    with pytest.raises(AgentProtocolError):
        AgentClaim.parse(_claim(schema_version=value))


def test_claim_rejects_unknown_top_level_fields_and_roundtrips_json() -> None:
    raw = _claim()
    with pytest.raises(AgentProtocolError):
        AgentClaim.parse(raw | {"unexpected": 1})
    parsed = AgentClaim.parse(raw)
    assert json.loads(canonical_message(parsed)) == raw


def test_distribution_payload_is_typed_and_plan_bound() -> None:
    payload = {
        "schema_version": 1,
        "authority_revision": "a" * 64,
        "plan_digest": "a" * 64,
    }
    parsed = ArtifactDistributionPayload.model_validate(payload)
    assert parsed.plan_digest == parsed.authority_revision
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
