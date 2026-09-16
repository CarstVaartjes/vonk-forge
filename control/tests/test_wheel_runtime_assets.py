from __future__ import annotations

import subprocess
import sys
import zipfile
from importlib.resources import files
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_control_wheel_packages_current_runtime_assets(tmp_path: Path) -> None:
    subprocess.run(
        [
            "uv",
            "build",
            "--offline",
            "--wheel",
            "--project",
            str(ROOT / "control"),
            "--out-dir",
            str(tmp_path),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(tmp_path.glob("vonk_control-*.whl"))

    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())

    # The test report is a canonical Pydantic contract now, so the retired
    # hand-written runtime schema is no longer packaged.
    assert "vonk_control/schemas/test-report-v1.schema.json" not in members
    assert "vonk_control/schema_resources.py" not in members
    assert (
        not {
            "vonk_control/schemas/catalog-entity-v1.schema.json",
            "vonk_control/schemas/harness-evidence-v1.schema.json",
            "vonk_control/catalog_contract.py",
            "vonk_control/catalog_seeds.py",
            "vonk_control/harnesses/registry.py",
        }
        & members
    )
    assert "vonk_control/schemas/recipe-v1.schema.json" not in members
    assert "vonk_control/alembic.ini" in members

    fixture = tmp_path / "synthetic-canonical-recipe.json"
    fixture.write_bytes(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-image.json")
        .read_bytes()
    )
    smoke = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "from vonk_forge_contracts import RecipeDefinition, TestReport\n"
                "from vonk_control import catalog_revision_contract, catalog_service\n"
                "RecipeDefinition.model_validate("
                "json.load(open(sys.argv[2], encoding='utf-8')))\n"
                "assert catalog_revision_contract.TestReport is TestReport\n"
                "assert catalog_service.TestReport is TestReport\n"
            ),
            str(wheel),
            str(fixture),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert smoke.returncode == 0, smoke.stderr
