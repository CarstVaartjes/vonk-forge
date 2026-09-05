from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "qualify-recipe"


def _library_root() -> Path:
    candidates = []
    configured = os.environ.get("VONK_RECIPE_CANONICAL_ROOT")
    if configured:
        candidates.append(Path(configured))
    candidates.extend((Path("/opt/vonk-forge-recipes"), Path("/private/tmp/vonk-forge-recipes-canonical")))
    for candidate in candidates:
        if (candidate / "contracts" / "src" / "vonk_forge_contracts").is_dir() and (candidate / "recipes").is_dir():
            return candidate
    pytest.skip("a published schema-2 recipe checkout is required for qualifier integration tests")


def _recipes(root: Path) -> list[Path]:
    return sorted((root / "recipes").glob("*.json"))


def _run(recipe: Path, library_root: Path) -> dict[str, object]:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--recipe",
            str(recipe),
            "--library-root",
            str(library_root),
            "--platform-root",
            str(ROOT),
            "--level",
            "structural",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


def test_archive_safety_rejects_traversal_and_payloads() -> None:
    namespace = __import__("runpy").run_path(str(SCRIPT))
    safe_member = namespace["_safe_member"]
    safe_member("adapters/example/Dockerfile")
    for member in ("../escape", "/absolute", "weights/model.safetensors", "oci/image.tar"):
        with pytest.raises(namespace["QualificationError"]):
            safe_member(member)


def test_structural_qualification_uses_dynamic_published_catalog() -> None:
    root = _library_root()
    recipes = _recipes(root)
    assert recipes
    payload = _run(recipes[0], root)
    assert payload["status"] == "passed"
    assert payload["physical_claim"] is False
    validator = payload["independent_validator"]
    assert validator["status"] == "passed"
    assert validator["recipe_count"] == len(recipes)
    assert validator["recipe_count"] > 0


def test_structural_examples_cover_source_job_and_dual_contracts() -> None:
    root = _library_root()
    selected: dict[str, Path] = {}
    for path in _recipes(root):
        document = json.loads(path.read_text(encoding="utf-8"))
        mode = document["execution"]["mode"]
        adapter = document["interfaces"][0]["adapter"]
        topology = document["topology"]["node_count"] > 1
        selected.setdefault("source", path) if mode == "build" else None
        selected.setdefault("job", path) if adapter != "openai" else None
        selected.setdefault("dual", path) if topology else None
        selected.setdefault("image", path) if mode == "image" else None
    assert {"source", "job", "dual"} <= selected.keys()
    for path in selected.values():
        payload = _run(path, root)
        assert payload["passed"] is True
        assert payload["physical_claim"] is False
        if path == selected.get("dual"):
            assert payload["compiled_roles"] > 1
    if "image" in selected:
        assert _run(selected["image"], root)["source_build"] is False


def test_container_gate_reports_environment_without_spark_claim(tmp_path: Path) -> None:
    root = _library_root()
    recipe = _recipes(root)[0]
    engine = tmp_path / "engine"
    engine.write_text("#!/bin/sh\nprintf '%s\\n' amd64\n", encoding="utf-8")
    engine.chmod(0o755)
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--recipe",
            str(recipe),
            "--library-root",
            str(root),
            "--platform-root",
            str(ROOT),
            "--level",
            "container",
            "--engine",
            str(engine),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 3
    payload = json.loads(result.stdout)
    assert payload["status"] == "environment-limited"
    assert payload["required_architecture"] == "arm64"
    assert payload["physical_claim"] is False
