"""Mirror misses fall back to the same upstream digest and never block a fresh run."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DIGEST = "sha256:" + "4" * 64
PIN = f"docker.io/tonistiigi/binfmt@{DIGEST}"
MIRROR = f"ghcr.io/carstvaartjes/ci-mirror/tonistiigi/binfmt@{DIGEST}"


def _environment(tmp_path: Path, fault: str) -> dict[str, str]:
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
    return {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "DOCKER_LOG": str(tmp_path / "docker.log"),
        "GITHUB_ENV": str(tmp_path / "github.env"),
        "FAULT": fault,
        "GITHUB_REPOSITORY_OWNER": "carstvaartjes",
    }


def _run(env: dict[str, str], *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(ROOT / "scripts/pull-test-images"), *arguments],
        env=env,
        capture_output=True,
        timeout=10,
        check=False,
    )


@pytest.mark.parametrize("fault", ["", "mirror", "both", "unknown"])
def test_a_pin_reads_the_mirror_first_then_the_same_upstream_digest(
    tmp_path: Path, fault: str
) -> None:
    env = _environment(tmp_path, fault)
    log = Path(env["DOCKER_LOG"])
    first = _run(env, "--image", PIN)
    expected = [f"pull --quiet {MIRROR}"]
    if fault:
        expected.append(f"pull --quiet {PIN}")
    assert log.read_text().splitlines() == expected
    assert (first.returncode == 0) == (fault != "both")
    if fault != "both":
        selected = MIRROR if not fault else PIN
        assert Path(env["GITHUB_ENV"]).read_text().endswith(f"={selected}\n")

    # No lock, gate or retained failure may prevent a fresh acquisition.
    log.write_text("")
    env["FAULT"] = ""
    assert _run(env, "--image", PIN).returncode == 0
    assert log.read_text().splitlines() == [f"pull --quiet {MIRROR}"]


def test_a_named_test_image_comes_from_its_own_repository(tmp_path: Path) -> None:
    # Tests and compose resolve these as tag@digest, which Docker only finds
    # for an image pulled from that same repository.
    env = _environment(tmp_path, "")
    assert _run(env, "postgres").returncode == 0
    (call,) = Path(env["DOCKER_LOG"]).read_text().splitlines()
    assert call.startswith("pull --quiet postgres:")
    assert "@sha256:" in call
