"""A mirror miss must preserve acquisition order and admit the next invocation."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("fault", ["", "mirror", "both", "unknown"])
def test_mirror_first_fallback_and_fresh_acquisition(
    tmp_path: Path, fault: str
) -> None:
    log = tmp_path / "docker.log"
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$DOCKER_LOG"\n'
        'if [[ "$1" == pull && "$FAULT" == unknown && "$3" == ghcr.io/* ]]; then\n'
        '  echo "unreadable registry reply" >&2; exit 1\n'
        "fi\n"
        'if [[ "$1" == pull && ( "$FAULT" == both || '
        '( "$FAULT" == mirror && "$3" == ghcr.io/* ) ) ]]; then\n'
        '  echo "manifest unknown" >&2; exit 1\n'
        "fi\n"
    )
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "DOCKER_LOG": str(log),
        "FAULT": fault,
        "GITHUB_REPOSITORY_OWNER": "carstvaartjes",
    }
    command = [str(ROOT / "scripts/pull-test-images"), "postgres"]
    first = subprocess.run(
        command, env=env, capture_output=True, timeout=10, check=False
    )
    calls = log.read_text().splitlines()
    mirror = calls[0].split()[-1]
    assert mirror.startswith("ghcr.io/carstvaartjes/ci-mirror/postgres@sha256:")
    if fault:
        upstream = calls[-1].split()[-1]
        assert upstream.startswith("postgres:")
        assert upstream.split("@")[-1] == mirror.split("@")[-1]
        assert calls == [
            f"pull --quiet {mirror}",
            f"pull --quiet {calls[-1].split()[-1]}",
        ]
    else:
        assert calls[1] == f"tag {mirror} postgres:18.6"
        assert len(calls) == 2
    assert (first.returncode == 0) == (fault != "both")

    # No lock, gate or retained failure may prevent a fresh acquisition.
    log.write_text("")
    env["FAULT"] = ""
    healed = subprocess.run(
        command, env=env, capture_output=True, timeout=10, check=False
    )
    assert healed.returncode == 0
    assert log.read_text().splitlines() == [
        f"pull --quiet {mirror}",
        f"tag {mirror} postgres:18.6",
    ]
