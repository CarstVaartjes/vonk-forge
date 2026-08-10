from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/verify-release-tag-authority"


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", repository, *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def tagged_remote(tmp_path: Path) -> tuple[Path, Path, str, str]:
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", remote], check=True, capture_output=True)
    subprocess.run(["git", "init", work], check=True, capture_output=True)
    git(work, "config", "user.name", "Release Test")
    git(work, "config", "user.email", "release@example.invalid")
    (work / "release.txt").write_text("accepted\n")
    git(work, "add", "release.txt")
    git(work, "commit", "-m", "accepted")
    source_sha = git(work, "rev-parse", "HEAD")
    git(work, "tag", "-a", "v1.2.3", "-m", "v1.2.3", source_sha)
    tag_oid = git(work, "rev-parse", "refs/tags/v1.2.3")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "origin", "HEAD:refs/heads/main", "refs/tags/v1.2.3")
    return work, remote, source_sha, tag_oid


def verify(
    repository: Path, tag: str, tag_oid: str, source_sha: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [SCRIPT, tag, tag_oid, source_sha],
        cwd=repository,
        env={**os.environ, "LC_ALL": "C"},
        check=False,
        capture_output=True,
        text=True,
    )


def test_exact_remote_annotated_tag_authority_is_accepted(
    tagged_remote: tuple[Path, Path, str, str],
) -> None:
    work, _, source_sha, tag_oid = tagged_remote

    result = verify(work, "v1.2.3", tag_oid, source_sha)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_moved_remote_tag_is_rejected_even_when_it_still_peels_to_source(
    tagged_remote: tuple[Path, Path, str, str],
) -> None:
    work, _, source_sha, recorded_tag_oid = tagged_remote
    git(work, "tag", "-f", "-a", "v1.2.3", "-m", "replacement", source_sha)
    moved_tag_oid = git(work, "rev-parse", "refs/tags/v1.2.3")
    assert moved_tag_oid != recorded_tag_oid
    git(work, "push", "--force", "origin", "refs/tags/v1.2.3")

    result = verify(work, "v1.2.3", recorded_tag_oid, source_sha)

    assert result.returncode != 0


def test_lightweight_remote_tag_is_rejected(
    tagged_remote: tuple[Path, Path, str, str],
) -> None:
    work, _, source_sha, _ = tagged_remote
    git(work, "tag", "-f", "v1.2.3", source_sha)
    lightweight_oid = git(work, "rev-parse", "refs/tags/v1.2.3")
    git(work, "push", "--force", "origin", "refs/tags/v1.2.3")

    result = verify(work, "v1.2.3", lightweight_oid, source_sha)

    assert result.returncode != 0


@pytest.mark.parametrize(
    ("tag", "tag_oid", "source_sha"),
    (
        ("latest", "a" * 40, "b" * 40),
        ("v01.2.3", "a" * 40, "b" * 40),
        ("v1.2.3", "A" * 40, "b" * 40),
        ("v1.2.3", "a" * 39, "b" * 40),
        ("v1.2.3", "a" * 40, "B" * 40),
        ("v1.2.3", "a" * 40, "b" * 39),
    ),
)
def test_noncanonical_authority_inputs_fail_before_fetch(
    tmp_path: Path, tag: str, tag_oid: str, source_sha: str
) -> None:
    result = verify(tmp_path, tag, tag_oid, source_sha)

    assert result.returncode == 64
    assert "release tag authority is invalid" in result.stderr
