from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/qualify-recipe"
DS4 = ROOT / "config/recipes/deepseek-v4-flash-0731-ds4-single.json"
MIA = ROOT / "config/recipes/deepseek-v4-flash-0731-mia-dual.json"


def _fake_engine(path: Path, architecture: str) -> Path:
    engine = path / "docker"
    engine.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = info ]; then\n"
        f"  printf '%s\\n' '{architecture}'\n"
        "  exit 0\n"
        "fi\n"
        "exit 97\n",
        encoding="utf-8",
    )
    engine.chmod(0o755)
    return engine


def test_container_qualification_reports_non_arm64_as_limitation(
    tmp_path: Path,
) -> None:
    assert SCRIPT.is_file()
    engine = _fake_engine(tmp_path, "amd64")
    result = subprocess.run(
        [
            str(SCRIPT),
            "--recipe",
            str(DS4),
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
    assert payload["detected_architecture"] == "amd64"
    assert payload["passed"] is False


def test_structural_qualification_validates_both_native_recipes() -> None:
    assert SCRIPT.is_file()
    for recipe in (DS4, MIA):
        result = subprocess.run(
            [str(SCRIPT), "--recipe", str(recipe), "--level", "structural"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        payload = json.loads(result.stdout)
        assert payload["status"] == "passed"
        assert payload["passed"] is True
        assert payload["recipe"] == recipe.name
