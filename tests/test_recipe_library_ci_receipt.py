from __future__ import annotations

import hashlib
import importlib.metadata
import re
import tomllib
import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "tests/acceptance/recipe-library-revision.txt"
CONTRACTS_LOCK = ROOT / "control/packaging/public-contracts.lock"
WORKFLOWS = (
    ROOT / ".github/workflows/ci.yml",
    ROOT / ".github/workflows/installer-publication.yml",
)


def test_recipe_library_ci_receipt_is_the_only_workflow_revision_source() -> None:
    revision = RECEIPT.read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"[0-9a-f]{40}", revision)
    assert all(revision not in path.read_text(encoding="utf-8") for path in WORKFLOWS)


def test_acceptance_recipe_library_is_the_reviewed_contract_revision() -> None:
    """Every suite and the Spark acceptance use the locked contracts.

    They all run in the control environment, whose ``vonk_forge_contracts`` is
    the revision ``control/uv.lock`` pins, while they read models and recipes
    from the checked-out library. A library revision other than the reviewed
    contracts revision would validate the catalog with different contract
    code than the one that produced it, so the three must name one commit.
    """

    revision = RECEIPT.read_text(encoding="utf-8").strip()
    lock = tomllib.loads(CONTRACTS_LOCK.read_text(encoding="utf-8"))
    assert revision == lock["revision"]
    control_lock = tomllib.loads((ROOT / "control/uv.lock").read_text(encoding="utf-8"))
    [contracts] = [
        package
        for package in control_lock["package"]
        if package["name"] == "vonk-forge-public-contracts"
    ]
    assert contracts["source"]["git"].endswith("#" + revision)


def test_spark_acceptance_uses_the_locked_control_environment() -> None:
    publication = yaml.safe_load(WORKFLOWS[1].read_text())["jobs"]
    assert any(
        job.get("uses") == "./.github/workflows/release-acceptance-core.yml"
        for job in publication.values()
    )
    acceptance = yaml.safe_load(
        (ROOT / ".github/workflows/release-acceptance-core.yml").read_text()
    )["jobs"]["spark-acceptance"]
    run = next(
        step["run"]
        for step in acceptance["steps"]
        if "python tests/acceptance/test_spark_lifecycle.py run" in step.get("run", "")
    )
    assert "uv run --project control --frozen" in run


def test_shipped_contract_wheel_is_the_tested_revision() -> None:
    """The image ships the vendored wheel; every suite tests the locked source.

    The control environment installs ``vonk_forge_contracts`` from the locked
    recipes revision, while the Controller image installs the reviewed wheel by
    its digest without rebuilding it. Their package files must be identical so
    the image runs exactly the contract code the suites ran.
    """

    lock = tomllib.loads(CONTRACTS_LOCK.read_text(encoding="utf-8"))
    tested = importlib.metadata.distribution("vonk-forge-public-contracts")
    assert lock["package"] == f"{tested.name}=={tested.version}"

    def package_file(name: str) -> bool:
        return not name.split("/", 1)[0].endswith(".dist-info")

    with zipfile.ZipFile(ROOT / lock["wheel"]) as wheel:
        shipped = {
            name: hashlib.sha256(wheel.read(name)).hexdigest()
            for name in wheel.namelist()
            if package_file(name)
        }
    installed = {
        str(path): hashlib.sha256(path.read_binary()).hexdigest()
        for path in tested.files or ()
        if path.hash is not None and package_file(str(path))
    }
    assert shipped == installed
