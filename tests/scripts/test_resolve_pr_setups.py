"""Main reuses the setup programs its pull request built, by tree."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/resolve-pr-setups", repo / "scripts")
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
    )

    def rev(name: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", name], text=True
        ).strip()

    return repo, rev("HEAD"), rev("HEAD^{tree}")


def _run(tmp_path: Path, names: list[str], built_tree: str):
    repo, source, _ = _repo(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "artifacts.json").write_text(
        json.dumps([{"artifacts": [{"name": name} for name in names]}])
    )
    (tmp_path / "commit.json").write_text(json.dumps([{"tree": {"sha": built_tree}}]))
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"root = {str(tmp_path)!r}\n"
        "name = '/commit.json' if '/git/commits/' in sys.argv[-1] else '/artifacts.json'\n"
        "print(open(root + name).read())\n"
    )
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    return source, subprocess.run(
        [str(repo / "scripts/resolve-pr-setups"), "42", source],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "GITHUB_REPOSITORY": "o/r",
        },
    )


PR = "a" * 40


def test_the_setups_built_from_this_tree_are_reused(tmp_path: Path) -> None:
    _, _, tree = _repo(tmp_path / "probe")
    names = [f"installer-setup-{PR}-nas", f"installer-setup-{PR}-linux-arm64", "other"]
    _, result = _run(tmp_path, names, tree)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"42\t0\t{PR}\n"


@pytest.mark.parametrize(
    ("names", "status"),
    [
        ([], 2),
        ([f"installer-setup-{PR}-nas"], 2),
        (
            [
                f"installer-setup-{PR}-nas",
                f"installer-setup-{PR}-linux-arm64",
                f"installer-setup-{'b' * 40}-nas",
            ],
            1,
        ),
    ],
)
def test_missing_or_mixed_setups_are_built_or_refused(tmp_path, names, status):
    _, _, tree = _repo(tmp_path / "probe")
    _, result = _run(tmp_path, names, tree)
    assert result.returncode == status, result.stderr


def test_setups_built_from_another_tree_are_refused(tmp_path: Path) -> None:
    names = [f"installer-setup-{PR}-nas", f"installer-setup-{PR}-linux-arm64"]
    _, result = _run(tmp_path, names, "f" * 40)
    assert result.returncode == 1
    assert "another tree" in result.stderr
