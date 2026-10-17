"""Main reuses a pull request's images only when every one verifies for its tree."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = "scripts/reuse-pr-candidate-images"
ROLES = ("api", "worker", "hermes", "litellm")
DIGESTS = {role: "sha256:" + hex_digit * 64 for role, hex_digit in zip(ROLES, "abde")}
RUN = "38921170575"
COMMIT = "c" * 40


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _workspace(tmp_path: Path, *, unverified: str = "") -> Path:
    """The real script beside a recording verifier."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / SCRIPT, root / SCRIPT)
    log = tmp_path / "calls"
    _executable(
        root / "scripts/verify-published-image",
        f'echo "verify $*" >> {log}\n[[ "$1" != "{unverified}" ]]\n',
    )
    return root


def _references(directory: Path, run: str = RUN, skip: str = "") -> Path:
    directory.mkdir()
    for role in ROLES:
        if role != skip:
            (directory / role).write_text(
                f"ghcr.io/carstvaartjes/vonk-forge-{role}:acceptance-{'a' * 40}-{run}-1"
                f"@{DIGESTS[role]}\n"
            )
    return directory


def _reuse(root: Path, references: Path, temp: Path):
    temp.mkdir(exist_ok=True)
    return subprocess.run(
        [str(root / SCRIPT), str(references), RUN, COMMIT],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={**os.environ, "RUNNER_TEMP": str(temp)},
    )


def test_every_verified_image_is_reused_by_digest_without_an_export(tmp_path):
    """Catches main pulling the verified images again: the pull request proved
    these digests, and the publisher tags and signs them in the registry."""
    root = _workspace(tmp_path)
    temp = tmp_path / "temp"
    result = _reuse(root, _references(tmp_path / "images"), temp)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        *(f"{role}_digest={DIGESTS[role]}" for role in ROLES),
        "reused=true",
    ]
    calls = (tmp_path / "calls").read_text()
    for role in ROLES:
        assert f"verify {role} ghcr.io/carstvaartjes/vonk-forge-{role} " in calls
        assert f"- {COMMIT}" in calls
    assert not list(temp.iterdir())


@pytest.mark.parametrize("fault", ["missing", "other-run", "unverified"])
def test_anything_short_of_four_verified_images_builds_instead(tmp_path, fault):
    """Catches a partial reuse, a reference borrowed from another run, or an
    image that is not this tree."""
    root = _workspace(tmp_path, unverified="hermes" if fault == "unverified" else "")
    references = _references(
        tmp_path / "images",
        run="1" if fault == "other-run" else RUN,
        skip="worker" if fault == "missing" else "",
    )
    temp = tmp_path / "temp"
    result = _reuse(root, references, temp)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
