"""The retained cutoff agrees with the real native restart reader."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from vonk_agent_protocol import RecipeRunObservationCheckpoint


def test_retained_cutoff_lexemes_match_native_restart_cases() -> None:
    cases = json.loads(
        (Path(__file__).parent / "fixtures/runtime_scan_cutoffs.json").read_text()
    )
    stamp = {
        "device": "1",
        "inode": "2",
        "modified_seconds": 0,
        "modified_nanoseconds": 0,
        "changed_seconds": 0,
        "changed_nanoseconds": 0,
    }
    record = {
        "root": "/managed/runs",
        "runs_stamp": stamp,
        "metadata_stamp": None,
        "witness": None,
        "had_plans": False,
        "had_failures": False,
    }
    for cutoff in cases["valid"]:
        model = RecipeRunObservationCheckpoint.model_validate_json(
            json.dumps({**record, "started_at": cutoff})
        )
        encoded = model.model_dump_json()
        assert json.loads(encoded)["started_at"] == cutoff
        assert (
            RecipeRunObservationCheckpoint.model_validate_json(encoded).started_at
            == cutoff
        )
    for cutoff in cases["invalid"]:
        with pytest.raises(ValidationError):
            RecipeRunObservationCheckpoint.model_validate_json(
                json.dumps({**record, "started_at": cutoff})
            )
