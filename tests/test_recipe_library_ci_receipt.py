from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "tests/acceptance/recipe-library-revision.txt"
CONTRACTS_LOCK = ROOT / "control/packaging/public-contracts.lock"
WORKFLOWS = (
    ROOT / ".github/workflows/ci.yml",
    ROOT / ".github/workflows/installer-publication.yml",
)


def test_recipe_library_ci_receipt_is_the_only_workflow_revision_source() -> None:
    lines = RECEIPT.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    revision = lines[0]
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
    workflow = (ROOT / ".github/workflows/installer-publication.yml").read_text(
        encoding="utf-8"
    )
    assert (
        "uv run --project control --frozen \\\n"
        "            python tests/acceptance/test_spark_lifecycle.py run"
    ) in workflow
