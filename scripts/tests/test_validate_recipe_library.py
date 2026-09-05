from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "validate-recipe-library"
CANDIDATE = Path(
    os.environ.get(
        "VONK_FORGE_RECIPE_LIBRARY",
        "/private/tmp/vonk-forge-recipes-contract-conversion-final-795b44f",
    )
)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _candidate_library_or_skip() -> Path:
    if not CANDIDATE.is_dir():
        pytest.skip("central contract recipe-library fixture is unavailable")
    return CANDIDATE


def test_contract_recipe_library_snapshot_is_validated_end_to_end() -> None:
    candidate = _candidate_library_or_skip()
    result = _run(
        "--library-root",
        str(candidate),
        "--platform-root",
        str(ROOT),
        "--json",
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["schema_version"] == 2
    assert report["recipe_count"] == len(list((candidate / "recipes").glob("*.json")))
    assert report["package_check"] == "identity-verified"
    assert report["contract_source"] == "contracts/"


def test_contract_recipe_library_rejects_an_archive_without_recipe_entrypoint(
    tmp_path: Path,
) -> None:
    candidate = _candidate_library_or_skip()
    library = tmp_path / "library"
    library.mkdir()
    for directory in ("models", "recipes", "contracts"):
        (library / directory).symlink_to(candidate / directory, target_is_directory=True)
    package_dir = library / "packages"
    package_dir.mkdir()
    for source in (candidate / "packages").glob("*.tar.gz"):
        shutil.copy2(source, package_dir / source.name)

    recipe_path = next((candidate / "recipes").glob("*.json"))
    recipe_slug = recipe_path.stem
    recipe_document = json.loads(recipe_path.read_text())
    context_path = recipe_document["execution"]["build"]["context"]["path"]
    shutil.copytree(candidate / context_path, library / context_path)
    package_name = f"{recipe_slug}.tar.gz"
    source_package = candidate / "packages" / package_name
    if not source_package.is_file():
        pytest.skip(f"candidate package is unavailable: {package_name}")
    invalid_package = io.BytesIO()
    with (
        tarfile.open(source_package, mode="r:gz") as source_archive,
        tarfile.open(fileobj=invalid_package, mode="w:gz") as target_archive,
    ):
        for member in source_archive.getmembers():
            if member.name == "recipe.json":
                continue
            payload = source_archive.extractfile(member)
            target_archive.addfile(member, payload)
    invalid_bytes = invalid_package.getvalue()
    (package_dir / package_name).unlink()
    (package_dir / package_name).write_bytes(invalid_bytes)

    index = json.loads((candidate / "catalog-index.json").read_text())
    for row in index["recipes"]:
        if row["document"]["identity"]["slug"] == recipe_slug:
            row["package"]["sha256"] = hashlib.sha256(invalid_bytes).hexdigest()
            row["package"]["expected_bytes"] = len(invalid_bytes)
            break
    (library / "catalog-index.json").write_text(json.dumps(index))

    result = _run(
        "--library-root",
        str(library),
        "--platform-root",
        str(ROOT),
    )

    assert result.returncode != 0
    assert "exactly one recipe.json entrypoint" in result.stderr
