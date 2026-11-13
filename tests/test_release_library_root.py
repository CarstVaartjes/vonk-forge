"""The carry lane fetches a release's recipe library at the revision it names and builds it with the release's own script."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.acceptance.spark_upgrade_carry import release_library_root
from tests.acceptance.test_spark_lifecycle import LifecycleError

REVISION = "0123456789abcdef0123456789abcdef01234567"
# Stands in for the release's build script: it records what it was called with.
SCRIPT = '#!/bin/sh\nprintf \'%s %s %s\' "$1" "$2" "$3" > "$1/catalog-index.json"\n'


def test_a_release_library_is_fetched_and_built_from_the_revision_it_names(
    tmp_path: Path,
) -> None:
    fetched: list[str] = []

    def fetch(revision: str, destination: Path) -> None:
        fetched.append(revision)
        tool = destination / "tools/build-catalog-index"
        tool.parent.mkdir(parents=True)
        tool.write_text("")
        # A fixed commit time: the epoch must come from the tree, never "now".
        os.utime(tool, (1_700_000_000, 1_700_000_000))

    root = release_library_root(
        "a" * 40,
        tmp_path,
        read_revision=lambda sha: f"{REVISION}\n" if sha == "a" * 40 else "",
        read_build_script=lambda sha: SCRIPT if sha == "a" * 40 else "",
        fetch_library=fetch,
    )

    assert fetched == [REVISION]
    assert root.name == f"recipe-library-{REVISION}"
    assert (root / "catalog-index.json").read_text() == f"{root} {REVISION} 1700000000"
    # Asking again reuses the built library.
    assert (
        release_library_root(
            "a" * 40,
            tmp_path,
            read_revision=lambda _sha: REVISION,
            read_build_script=lambda _sha: pytest.fail("script read again"),
            fetch_library=lambda *_: pytest.fail("fetched again"),
        )
        == root
    )


def test_a_release_that_names_no_exact_revision_is_refused(tmp_path: Path) -> None:
    with pytest.raises(LifecycleError):
        release_library_root("a" * 40, tmp_path, read_revision=lambda _sha: "main\n")
