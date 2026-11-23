"""Lifecycle producers survive storage and the generated operator consumer."""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path

import pytest
from vonk_agent_protocol import (
    LifecycleVocabulary,
    OutcomeCatalog,
    OutcomeEvidence,
    OutcomeKind,
    OutcomeUnknown,
    WaitReason,
    canonical_message,
)

from cluster_profiles.generated_control.models.lifecycle_vocabulary import (
    LifecycleVocabulary as GeneratedVocabulary,
)


def test_lifecycle_producer_store_generated_consumer_roundtrip(tmp_path: Path) -> None:
    defaults = {}
    for name, field in LifecycleVocabulary.model_fields.items():
        enum = field.annotation
        assert isinstance(enum, type) and issubclass(enum, Enum)
        defaults[name] = next(iter(enum))
    record = tmp_path / "lifecycle.json"
    for name, field in LifecycleVocabulary.model_fields.items():
        enum = field.annotation
        assert isinstance(enum, type) and issubclass(enum, Enum)
        for member in enum:
            producer = LifecycleVocabulary.model_validate({**defaults, name: member})
            record.write_bytes(canonical_message(producer))
            returned = GeneratedVocabulary.from_dict(
                json.loads(record.read_bytes())
            ).to_dict()
            assert (
                LifecycleVocabulary.model_validate_json(json.dumps(returned))
                == producer
            )


@pytest.mark.parametrize("explicit_null", [False, True])
def test_unknown_outcome_preserves_zero_and_rejects_incomplete_tagged_arm(
    tmp_path: Path,
    explicit_null: bool,
) -> None:
    producer = OutcomeUnknown(
        kind=OutcomeKind.UNKNOWN,
        wait_reason=WaitReason.OBSERVATION_UNAVAILABLE,
        reason="peer response unavailable",
        retry_after_seconds=0,
        evidence=OutcomeEvidence(helper_exit_code=0),
    )
    document = json.loads(canonical_message(OutcomeCatalog(outcome=producer)))
    if explicit_null:
        document["outcome"]["receipt"] = None
    record = tmp_path / "outcome.json"
    record.write_text(json.dumps(document))
    restored = OutcomeCatalog.model_validate_json(record.read_bytes())
    assert restored.outcome == producer
    del document["outcome"]["wait_reason"]
    record.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        OutcomeCatalog.model_validate_json(record.read_bytes())
    record.write_bytes(canonical_message(restored))
    assert OutcomeCatalog.model_validate_json(record.read_bytes()) == restored
