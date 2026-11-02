"""The carry lane builds a release's own recipe library, end to end, before release."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tests.acceptance.spark_upgrade_carry import release_library_root
from tests.acceptance.test_spark_lifecycle import LifecycleError

pytestmark = pytest.mark.needs_recipe_library

REVISION = "0123456789abcdef0123456789abcdef01234567"


# Builds a whole recipe library index and its packages (about 40 s).
@pytest.mark.slow(60)
def test_a_release_library_is_fetched_and_built_from_the_revision_it_names(
    tmp_path: Path,
) -> None:
    source = Path(os.environ["VONK_RECIPE_LIBRARY_ROOT"])
    fetched: list[str] = []

    def fetch(revision: str, destination: Path) -> None:
        fetched.append(revision)
        shutil.copytree(
            source,
            destination,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(".git", "catalog-index.json", "packages"),
        )

    root = release_library_root(
        "a" * 40,
        tmp_path,
        read_revision=lambda sha: f"{REVISION}\n" if sha == "a" * 40 else "",
        fetch_library=fetch,
    )

    assert fetched == [REVISION]
    assert root.name == f"recipe-library-{REVISION}"
    assert (root / "catalog-index.json").is_file()
    assert (root / "tests/fixtures/canonical-synthetic-canary/index.json").is_file()
    # Asking again reuses the built library.
    assert (
        release_library_root(
            "a" * 40,
            tmp_path,
            read_revision=lambda _sha: REVISION,
            fetch_library=lambda *_: pytest.fail("fetched again"),
        )
        == root
    )


def test_a_release_that_names_no_exact_revision_is_refused(tmp_path: Path) -> None:
    with pytest.raises(LifecycleError):
        release_library_root("a" * 40, tmp_path, read_revision=lambda _sha: "main\n")
