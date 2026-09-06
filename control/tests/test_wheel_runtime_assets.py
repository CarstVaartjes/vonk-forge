from __future__ import annotations

import subprocess
import sys
import zipfile
from importlib.resources import files
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
        "vonk_control/schemas/catalog-entity-v1.schema.json",
        "vonk_control/schemas/harness-evidence-v1.schema.json",
        "vonk_control/schemas/recipe-v1.schema.json",
        "vonk_control/schemas/test-report-v1.schema.json",
    } <= members

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
                "import json,sys;"
                "sys.path.insert(0,sys.argv[1]);"
                "from jsonschema import Draft202012Validator;"
                "from vonk_forge_contracts import RecipeDefinition;"
                "from vonk_control.schema_resources import read_runtime_schema;"
                "RecipeDefinition.model_validate(json.load(open(sys.argv[2], encoding='utf-8')));"
                "[Draft202012Validator.check_schema(json.loads(read_runtime_schema(name))) "
                "for name in ('catalog-entity-v1.schema.json','harness-evidence-v1.schema.json',"
                "'recipe-v1.schema.json','test-report-v1.schema.json')]"
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
