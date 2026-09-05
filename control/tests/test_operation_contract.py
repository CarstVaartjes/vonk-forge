from __future__ import annotations

import pytest
from pydantic import ValidationError
from vonk_agent_protocol import canonical_message
from vonk_control.operation_contract import (
    OperationPhase,
    OperationProgress,
    OperationRecoveryAction,
    is_secret_field,
    normalize_operation_progress,
    recovery_for_operation,
    recovery_for_state,
    sanitize_failure_evidence,
    validate_progress_update,
)


def test_operation_phase_contract_is_stable_and_complete() -> None:
    assert [phase.value for phase in OperationPhase] == [
        "download",
        "verify",
        "transfer",
        "prepare",
        "cleanup",
        "stop",
        "start",
        "final_verify",
    ]


def test_progress_makes_unknown_totals_explicit_and_accepts_wire_aliases() -> None:
    unknown = OperationProgress.model_validate(
        {"phase": "download", "bytes_done": 10, "total_unknown": True}
    )
    assert unknown.completed_bytes == 10
    assert unknown.total_bytes is None
    assert unknown.total_bytes_known is False
    assert (
        normalize_operation_progress({"phase": "download", "completed_bytes": 10})[
            "total_bytes_known"
        ]
        is False
    )

    with pytest.raises(ValidationError, match="total_bytes_known"):
        OperationProgress(phase="download", total_bytes=100, total_bytes_known=False)

    known = OperationProgress(
        phase="download", completed_bytes=10, total_bytes=100, total_bytes_known=True
    )
    assert known.model_dump(mode="json")["total_bytes"] == 100


def test_checkpoint_and_bytes_updates_are_monotonic() -> None:
    previous = {
        "phase": "transfer",
        "completed_bytes": 50,
        "total_bytes": 100,
        "checkpoint": {"key": "shard", "sequence": 2, "cursor": "50"},
    }
    updated = validate_progress_update(
        previous,
        {
            "phase": "transfer",
            "completed_bytes": 75,
            "total_bytes": 100,
            "total_bytes_known": True,
            "checkpoint": {"key": "shard", "sequence": 3, "cursor": "75"},
        },
    )
    assert updated["completed_bytes"] == 75
    retained = validate_progress_update(previous, {"phase": "verify"})
    assert retained["completed_bytes"] == 50
    assert retained["checkpoint"] == previous["checkpoint"]
    with pytest.raises(ValueError, match="cannot move backwards"):
        validate_progress_update(previous, {"phase": "transfer", "completed_bytes": 49})
    with pytest.raises(ValueError, match="reused"):
        validate_progress_update(
            previous,
            {
                "phase": "transfer",
                "completed_bytes": 50,
                "checkpoint": {
                    "key": "shard",
                    "sequence": 2,
                    "cursor": "different",
                },
            },
        )


def test_failure_evidence_is_bounded_and_secret_free() -> None:
    safe = sanitize_failure_evidence(
        {
            "error_code": "transfer_failed",
            "summary": "download failed",
            "token": "do-not-persist",
            "HF_TOKEN": "hf-secret",
            "apiKey": "api-secret",
            "max_tokens": 512,
            "token_budget": 1_024,
            "tokenizer": "custom",
            "unfamiliar_token_parameter": "opaque",
            "detail": "x" * 5000,
            "nested": {"authorization": "hidden", "reason": "network"},
        }
    )
    assert "token" not in safe
    assert "HF_TOKEN" not in safe
    assert "apiKey" not in safe
    assert "authorization" not in str(safe)
    assert safe["max_tokens"] == 512
    assert safe["token_budget"] == 1_024
    assert safe["tokenizer"] == "custom"
    assert safe["unfamiliar_token_parameter"] == "opaque"
    assert len(safe["detail"]) == 1024
    assert len(canonical_message(safe)) <= 8192


@pytest.mark.parametrize(
    "name",
    [
        "apiKey",
        "APIKey",
        "accessToken",
        "privateKey",
        "passwordHash",
        "hf_token",
        "HF-TOKEN",
        "github_token",
    ],
)
def test_secret_field_detection_normalizes_credential_spellings(name: str) -> None:
    assert is_secret_field(name)


@pytest.mark.parametrize(
    "name", ["max_tokens", "token_budget", "tokenizer", "tokens_per_minute"]
)
def test_secret_field_detection_keeps_token_count_parameters_opaque(name: str) -> None:
    assert not is_secret_field(name)


def test_uncertain_operations_require_inspection_before_resume() -> None:
    recovery = recovery_for_state("waiting-for-operator", uncertain=True)
    assert recovery.uncertain is True
    assert recovery.actions == [OperationRecoveryAction.INSPECT]
    advertised = recovery_for_operation(
        "waiting-for-operator",
        uncertain=True,
        supported_actions=["resume", "cancel", "unknown"],
    )
    assert advertised.actions == [
        OperationRecoveryAction.INSPECT,
        OperationRecoveryAction.RESUME,
        OperationRecoveryAction.CANCEL,
    ]
