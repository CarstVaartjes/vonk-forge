from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIBRARY_ROOT = Path(os.environ.get("VONK_RECIPE_LIBRARY_ROOT", ROOT.parent / "vonk-forge-recipes"))
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
            "recipe": "recipes/deepseek-v4-flash-0731-mia-dual.json",
        },
        "model-single": {
            "context": "adapters/deepseek/ds4",
            "recipe": "recipes/deepseek-v4-flash-0731-ds4-single.json",
        },
    }
    assert not (ROOT / "config/recipes/deepseek-v4-flash-0731-ds4-single.json").exists()
    assert (LIBRARY_ROOT / "recipes/deepseek-v4-flash-0731-ds4-single.json").is_file()
    assert (LIBRARY_ROOT / "recipes/deepseek-v4-flash-0731-mia-dual.json").is_file()


def test_development_model_qualifier_executes_native_structural_path(
    tmp_path: Path,
) -> None:
    output = tmp_path / "qualification.json"
    result = subprocess.run(
        [
            str(QUALIFIER),
            "--recipe",
            str(LIBRARY_ROOT / "recipes/deepseek-v4-flash-0731-ds4-single.json"),
            "--library-root",
            str(LIBRARY_ROOT),
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
    assert evidence["recipe"] == "deepseek-v4-flash-0731-ds4-single"


def test_development_model_qualifier_refuses_a_symlink_output_before_resolution(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.json"
    target.write_text("do-not-replace\n", encoding="utf-8")
    output = tmp_path / "qualification.json"
    output.symlink_to(target)

    result = subprocess.run(
        [
            str(QUALIFIER),
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

    assert result.returncode == 1
    assert target.read_text(encoding="utf-8") == "do-not-replace\n"
    assert output.is_symlink()


def test_development_model_qualifier_refuses_a_symlinked_output_parent(
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    result = subprocess.run(
        [
            str(QUALIFIER),
            "--level",
            "structural",
            "--output",
            str(linked_parent / "qualification.json"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )

    assert result.returncode == 1
    assert not (real_parent / "qualification.json").exists()
