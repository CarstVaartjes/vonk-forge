"""Stored canonical reasons survive the generated operator consumer.

Generated Rust/browser drift is covered by the generation and wire lanes.
This catches a consumer that loses a valid reason during a real JSON roundtrip.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path

from vonk_agent_protocol import ReasonCodeVocabulary, canonical_message

from cluster_profiles.generated_control.models.reason_code_vocabulary import (
    ReasonCodeVocabulary as GeneratedVocabulary,
)


def test_reason_producer_store_generated_consumer_roundtrip(tmp_path: Path) -> None:
    defaults = {}
    for name, field in ReasonCodeVocabulary.model_fields.items():
        enum = field.annotation
        assert isinstance(enum, type) and issubclass(enum, Enum)
        defaults[name] = next(iter(enum))
    record = tmp_path / "reasons.json"
    for name, field in ReasonCodeVocabulary.model_fields.items():
        enum = field.annotation
        assert isinstance(enum, type) and issubclass(enum, Enum)
        for member in enum:
            producer = ReasonCodeVocabulary.model_validate({**defaults, name: member})
            record.write_bytes(canonical_message(producer))
            stored = json.loads(record.read_bytes())
            returned = GeneratedVocabulary.from_dict(stored).to_dict()
            assert (
                ReasonCodeVocabulary.model_validate_json(json.dumps(returned))
                == producer
            )
