"""The carry lane builds a release's own recipe library, end to end, before release."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.acceptance.spark_upgrade_carry import release_library_root

pytestmark = pytest.mark.needs_recipe_library


# Builds a whole recipe library index and its packages (about 40 s).
@pytest.mark.slow(120)
def test_a_release_library_is_checked_out_and_built_from_the_revision_it_names(
    tmp_path: Path,
) -> None:
    source = Path(os.environ["VONK_RECIPE_LIBRARY_ROOT"])
    revision = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    root = release_library_root(
        "a" * 40,
        tmp_path,
        read_revision=lambda sha: f"{revision}\n" if sha == "a" * 40 else "",
        repository=str(source),
    )

    assert root.name == f"recipe-library-{revision}"
    assert (root / "catalog-index.json").is_file()
    assert (root / "tests/fixtures/canonical-synthetic-canary/index.json").is_file()
    # Asking again reuses the built library.
    assert (
        release_library_root(
            "a" * 40,
            tmp_path,
            read_revision=lambda _sha: revision,
            repository="/nowhere",
        )
        == root
    )


def test_a_release_that_names_no_exact_revision_is_refused(tmp_path: Path) -> None:
    from tests.acceptance.test_spark_lifecycle import LifecycleError

    with pytest.raises(LifecycleError):
        release_library_root("a" * 40, tmp_path, read_revision=lambda _sha: "main\n")
