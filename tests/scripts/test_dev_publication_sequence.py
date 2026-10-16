"""Development versions are ordered by their source commit's committer time."""

from __future__ import annotations

import itertools
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev-publication-sequence"


def _commit(repo: Path, committed_at: int) -> str:
    environment = {
        **os.environ,
        "GIT_AUTHOR_DATE": "@1000000000 +0000",
        "GIT_COMMITTER_DATE": f"@{committed_at} +0000",
    }
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-qm",
            "merge",
        ],
        check=True,
        env=environment,
    )
    return subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()


def _sequence(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *arguments],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


def test_later_merges_get_higher_versions_than_run_numbered_ones(
    tmp_path: Path,
) -> None:
    """The committer time, not the author time, orders main; it is far above the
    run numbers apt dev published before, so the switch never sorts lower."""
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    first = _commit(repo, 1792000000)
    second = _commit(repo, 1792000060)

    sequences = [_sequence(repo, commit).stdout.strip() for commit in (first, second)]

    assert sequences == ["1792000000", "1792000060"]
    versions = [f"0.1.1~dev.{value}+g{'a' * 12}" for value in ("4161", *sequences)]
    for older, newer in itertools.pairwise(versions):
        assert (
            subprocess.run(
                ["dpkg", "--compare-versions", older, "lt", newer], check=False
            ).returncode
            == 0
        )


@pytest.mark.parametrize("arguments", [(), ("HEAD",), ("0" * 40,), ("a" * 40, "b")])
def test_only_one_existing_full_commit_is_accepted(tmp_path: Path, arguments) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _commit(repo, 1792000000)
    assert _sequence(repo, *arguments).returncode != 0
