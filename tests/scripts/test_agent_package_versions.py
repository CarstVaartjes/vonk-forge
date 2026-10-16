"""A package set's versions travel with it, whoever sealed it."""

from __future__ import annotations

import io
import json
import os
import runpy
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/agent-package-versions"
VERSIONS = runpy.run_path(str(SCRIPT))
METADATA = runpy.run_path(str(ROOT / "scripts/agent-package-metadata"))
SHA = "c" * 40


def _workspace() -> str:
    return METADATA["workspace_version"]()


def test_a_commit_built_set_names_exactly_what_the_metadata_names() -> None:
    expected = METADATA["metadata"](
        ["development", "branch", "main", SHA, "1792000000"]
    )
    fields = dict(line.split("=", 1) for line in expected)
    assert (
        VERSIONS["lines"](SHA, fields["version"], fields["baseline_version"])
        == expected
    )


@pytest.mark.parametrize(
    ("version", "baseline"),
    [
        ("9.9.9~dev.1+g" + "a" * 12, "0.0.1~acceptance.1+g" + "a" * 12),
        ("{w}~dev.0+g" + "a" * 12, "0.0.1~acceptance.1+g" + "a" * 12),
        ("{w}~dev.17+gshort", "0.0.1~acceptance.1+g" + "a" * 12),
        ("{w}~dev.17+g" + "a" * 12, "0.0.1~dev.1+g" + "a" * 12),
    ],
)
def test_versions_outside_the_development_scheme_are_refused(version, baseline):
    with pytest.raises(ValueError):
        VERSIONS["lines"](SHA, version.format(w=_workspace()), baseline)


def _fake_gh(tmp_path: Path, record: str | None) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    archive = io.BytesIO()
    if record is not None:
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("versions.txt", record)
    (tmp_path / "record.zip").write_bytes(archive.getvalue())
    listing = [{"id": 7, "expired": False}] if record is not None else []
    (tmp_path / "listing.json").write_text(json.dumps({"artifacts": listing}))
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"root = {str(tmp_path)!r}\n"
        "endpoint = sys.argv[-1]\n"
        "name = '/record.zip' if endpoint.endswith('/zip') else '/listing.json'\n"
        "sys.stdout.buffer.write(open(root + name, 'rb').read())\n"
    )
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GITHUB_REPOSITORY": "o/r",
    }


def test_a_sealed_pull_request_set_is_read_from_its_record(tmp_path: Path) -> None:
    """Catches recomputing a version from main's commit for a package that
    keeps the pull request's version: assembly would look for another file."""
    version = f"{_workspace()}~dev.1792000123+g" + "b" * 12
    baseline = METADATA["acceptance_baseline_version"](_workspace(), "b" * 40)
    environment = _fake_gh(
        tmp_path, f"version={version}\nbaseline_version={baseline}\n"
    )
    result = subprocess.run(
        [str(SCRIPT), "read", "42", SHA],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == VERSIONS["lines"](SHA, version, baseline)


def test_a_damaged_record_is_refused(tmp_path: Path) -> None:
    environment = _fake_gh(tmp_path, "version=1.0.0\n")
    result = subprocess.run(
        [str(SCRIPT), "read", "42", SHA],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0


def test_a_set_without_a_record_follows_its_source_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@example.com",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--allow-empty",
            "-qm",
            "main",
        ],
        check=True,
        env={**os.environ, "GIT_COMMITTER_DATE": "@1792000000 +0000"},
    )
    sha = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    environment = _fake_gh(tmp_path, None)
    result = subprocess.run(
        [str(SCRIPT), "read", "42", sha],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == METADATA["metadata"](
        ["development", "branch", "main", sha, "1792000000"]
    )
