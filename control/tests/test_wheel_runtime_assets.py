from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_control_wheel_contains_runtime_contract_schemas(tmp_path: Path) -> None:
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

    assert {
        "vonk_control/schemas/recipe-v1.schema.json",
        "vonk_control/schemas/test-report-v1.schema.json",
    } <= members

    fixture = ROOT / "control/tests/fixtures/global/recipe-v1-minimal.json"
    smoke = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json,sys;"
                "sys.path.insert(0,sys.argv[1]);"
                "from vonk_control.catalog_service import _test_report_validator;"
                "from vonk_control.recipe_contract import validate_recipe;"
                "validate_recipe(json.load(open(sys.argv[2], encoding='utf-8')));"
                "_test_report_validator()"
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
