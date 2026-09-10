from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "tests/acceptance/recipe-library-revision.txt"
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


def test_spark_acceptance_uses_the_locked_control_environment() -> None:
    workflow = (ROOT / ".github/workflows/installer-publication.yml").read_text(
        encoding="utf-8"
    )
    assert (
        'uv run --project control --frozen --with-editable . \\\n'
        '            --with-editable "$VONK_RECIPE_LIBRARY_ROOT/contracts" \\\n'
        '            python tests/acceptance/test_spark_lifecycle.py run'
    ) in workflow
