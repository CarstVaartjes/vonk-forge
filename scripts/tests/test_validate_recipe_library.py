from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "validate-recipe-library"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_platform_tree_is_a_valid_recipe_library_snapshot() -> None:
    result = _run("--library-root", str(ROOT), "--platform-root", str(ROOT), "--json")

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["schema_version"] == 1
    assert report["recipe_count"] == 2
    assert report["catalog_entity_count"] >= 10
    assert report["secret_scan"] == "passed"


def test_library_validation_rejects_a_missing_exact_recipe_dependency(tmp_path: Path) -> None:
    library = tmp_path / "library"
    for directory in (
        "model-groups",
        "models",
        "model-versions",
        "runtime-distributions",
        "patch-bundles",
        "recipes",
    ):
        (library / directory).mkdir(parents=True)
    for directory in (
        "model-groups",
        "models",
        "model-versions",
        "runtime-distributions",
        "patch-bundles",
    ):
        for source in (ROOT / "config" / directory).glob("*.json"):
            shutil.copy2(source, library / directory / source.name)
    shutil.copytree(ROOT / "adapters", library / "adapters")
    recipe = json.loads(
        (ROOT / "config/recipes/deepseek-v4-flash-0731-ds4-single.json").read_text()
    )
    recipe["model"]["content_sha256"] = "0" * 64
    (library / "recipes/broken.json").write_text(json.dumps(recipe))

    result = _run(
        "--library-root",
        str(library),
        "--platform-root",
        str(ROOT),
    )

    assert result.returncode != 0
    assert "missing exact catalog dependency" in result.stderr


def test_library_validation_rejects_a_missing_exact_model_parent(tmp_path: Path) -> None:
    library = tmp_path / "library"
    for directory in (
        "model-groups",
        "models",
        "model-versions",
        "runtime-distributions",
        "patch-bundles",
        "recipes",
    ):
        (library / directory).mkdir(parents=True)
    for directory in (
        "model-groups",
        "models",
        "model-versions",
        "runtime-distributions",
        "patch-bundles",
    ):
        for source in (ROOT / "config" / directory).glob("*.json"):
            shutil.copy2(source, library / directory / source.name)
    shutil.copytree(ROOT / "adapters", library / "adapters")

    model = json.loads(
        (ROOT / "config/models/deepseek-v4-flash-0731.json").read_text()
    )
    model["model_group"]["content_sha256"] = "0" * 64
    (library / "models/broken-parent.json").write_text(json.dumps(model))

    result = _run(
        "--library-root",
        str(library),
        "--platform-root",
        str(ROOT),
    )

    assert result.returncode != 0
    assert "missing exact catalog dependency" in result.stderr


def test_structural_qualification_accepts_a_recipe_checked_out_separately(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    for directory in (
        "model-groups",
        "models",
        "model-versions",
        "runtime-distributions",
        "patch-bundles",
        "recipes",
    ):
        shutil.copytree(ROOT / "config" / directory, library / directory)
    shutil.copytree(ROOT / "adapters", library / "adapters")

    result = subprocess.run(
        [
            str(ROOT / "scripts" / "qualify-recipe"),
            "--recipe",
            str(library / "recipes/deepseek-v4-flash-0731-ds4-single.json"),
            "--library-root",
            str(library),
            "--platform-root",
            str(ROOT),
            "--level",
            "structural",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["passed"] is True
