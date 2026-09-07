from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from vonk_agent_protocol import RecipeRunObservationsWire

from agent_protocol.tests.test_recipe_observations import _observation


@pytest.fixture(scope="session")
def recipe_observation_wire_probe() -> Path:
    configured = os.environ.get("VONK_RECIPE_OBSERVATION_WIRE_PROBE")
    repository = Path(__file__).resolve().parents[2]
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = repository / path
        path = path.resolve()
    else:
        subprocess.run(
            [
                "cargo",
                "build",
                "--locked",
                "--package",
                "vonk-agent",
                "--example",
                "recipe_observation_wire_probe",
            ],
            cwd=repository,
            check=True,
        )
        target_root = Path(os.environ.get("CARGO_TARGET_DIR", repository / "target"))
        if not target_root.is_absolute():
            target_root = repository / target_root
        path = target_root / "debug" / "examples" / "recipe_observation_wire_probe"
    if not path.is_file() or not os.access(path, os.X_OK):
        raise AssertionError(f"observation wire probe is not executable: {path}")
    return path


def test_rust_observation_json_is_consumed_by_the_controller_wire_model(
    recipe_observation_wire_probe: Path,
) -> None:
    completed = subprocess.run(
        [str(recipe_observation_wire_probe)],
        input=json.dumps(_observation(), separators=(",", ":")) + "\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    output = [line for line in completed.stdout.splitlines() if line.strip()]
    assert len(output) == 1
    parsed = RecipeRunObservationsWire.parse(json.loads(output[0]))
    assert parsed.schema_version == 2
    assert len(parsed.runs) == 1
    assert parsed.runs[0].helper_receipt.signature.algorithm == "ed25519"
