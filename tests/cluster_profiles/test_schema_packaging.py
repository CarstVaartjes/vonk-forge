from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CANONICAL = ROOT / "src/cluster_profiles/schemas"
MIRROR = ROOT / "schemas"
STANDALONE_SCHEMAS = {
    "install-release-manifest.schema.json",
    "workload-artifact-build.schema.json",
}
WEB_RECIPE_PRESETS = ROOT / "control/web/src/pages/custom-recipe-presets.json"
QUALIFICATION_AUTHORITIES = ROOT / "src/cluster_profiles/qualification_authorities"


def test_repository_schema_mirrors_match_canonical_package_schemas() -> None:
    canonical_names = {path.name for path in CANONICAL.glob("*.json")}
    mirror_names = {
        path.name
        for path in MIRROR.glob("*.json")
        if path.name not in STANDALONE_SCHEMAS
    }

    assert mirror_names == canonical_names
    for name in sorted(canonical_names):
        assert (MIRROR / name).read_bytes() == (CANONICAL / name).read_bytes()


def test_cluster_profile_schema_identifiers_use_the_vonk_forge_namespace() -> None:
    for path in sorted(CANONICAL.glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if "$id" in document:
            assert document["$id"].startswith("https://vonk-forge.")


def test_built_wheel_contains_every_canonical_schema(tmp_path: Path) -> None:
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(tmp_path.glob("*.whl"))

    with zipfile.ZipFile(wheel) as archive:
        for schema in sorted(CANONICAL.glob("*.json")):
            assert (
                archive.read(f"cluster_profiles/schemas/{schema.name}")
                == schema.read_bytes()
            )
        assert (
            archive.read("cluster_profiles/resources/custom-recipe-presets.json")
            == WEB_RECIPE_PRESETS.read_bytes()
        )
        for authority in sorted(QUALIFICATION_AUTHORITIES.glob("*.json")):
            assert (
                archive.read(
                    f"cluster_profiles/qualification_authorities/{authority.name}"
                )
                == authority.read_bytes()
            )
