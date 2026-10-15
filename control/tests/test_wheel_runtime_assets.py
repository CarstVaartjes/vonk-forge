from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from importlib.resources import files
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _stage_control_project(tmp_path: Path) -> Path:
    # Stage the project the way the image build does: the built admin UI is
    # copied into the package root before the wheel is built.
    project = tmp_path / "project"
    project.mkdir()
    for name in ("pyproject.toml", "alembic.ini"):
        shutil.copy2(ROOT / "control" / name, project / name)
    ignore = shutil.ignore_patterns("__pycache__")
    shutil.copytree(ROOT / "control/migrations", project / "migrations", ignore=ignore)
    shutil.copytree(ROOT / "control/src", project / "src", ignore=ignore)
    web = project / "src/vonk_control/web"
    web.mkdir()
    (web / "index.html").write_text("<h1>Admin</h1>", encoding="utf-8")
    return project


def test_control_wheel_packages_current_runtime_assets(tmp_path: Path) -> None:
    project = _stage_control_project(tmp_path)
    subprocess.run(
        # The locked build backend in the test environment: no isolated build
        # environment, no network.
        [
            sys.executable,
            "-m",
            "hatchling",
            "build",
            "--target",
            "wheel",
            "--directory",
            str(tmp_path),
        ],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(tmp_path.glob("vonk_control-*.whl"))

    with zipfile.ZipFile(wheel) as archive:
        members = set(archive.namelist())

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
    assert "vonk_control/web/index.html" in members

    fixture = tmp_path / "synthetic-canonical-recipe.json"
    fixture.write_bytes(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-source-build.json")
        .read_bytes()
    )
    smoke = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "from vonk_forge_contracts import read_recipe\n"
                "from vonk_control import catalog_revision_contract, catalog_service\n"
                "read_recipe(json.load(open(sys.argv[2], encoding='utf-8')))\n"
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

    # The app built from the wheel mounts the admin UI it ships.
    installed = tmp_path / "installed"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(installed)
    mounted = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "from fastapi import FastAPI\n"
                "from fastapi.testclient import TestClient\n"
                "import vonk_control\n"
                "from vonk_control.api.production import mount_admin_web\n"
                "assert vonk_control.__file__.startswith(sys.argv[1])\n"
                "app = FastAPI()\n"
                "mount_admin_web(app)\n"
                'assert [r for r in app.routes if r.name == "admin-web"]\n'
                "client = TestClient(app)\n"
                'assert client.get("/").text == "<h1>Admin</h1>"\n'
                'assert client.get("/profiles/x").text == "<h1>Admin</h1>"\n'
            ),
            str(installed),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert mounted.returncode == 0, mounted.stderr
