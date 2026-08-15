from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SLICES = ROOT / "scripts/run-development-slices"
QUALIFIER = ROOT / "scripts/qualify-development-model"


def test_development_slices_resolve_only_native_v1_inputs() -> None:
    result = subprocess.run(
        [str(SLICES), "--print-native-inputs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )

    assert json.loads(result.stdout) == {
        "model-multinode": {
            "context": "adapters/deepseek/mia-vllm",
            "recipe": "config/recipes/deepseek-v4-flash-0731-mia-dual.json",
        },
        "model-single": {
            "context": "adapters/deepseek/ds4",
            "recipe": "config/recipes/deepseek-v4-flash-0731-ds4-single.json",
        },
    }


def test_development_model_qualifier_executes_native_structural_path(
    tmp_path: Path,
) -> None:
    output = tmp_path / "qualification.json"
    result = subprocess.run(
        [
            str(QUALIFIER),
            "--recipe",
            "config/recipes/deepseek-v4-flash-0731-ds4-single.json",
            "--level",
            "structural",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )

    assert result.returncode == 0, result.stderr
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence["passed"] is True
    assert evidence["status"] == "passed"
    assert evidence["recipe"] == "deepseek-v4-flash-0731-ds4-single.json"
