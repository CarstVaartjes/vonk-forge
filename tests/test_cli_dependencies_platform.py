"""The CLI dependency environment is never reused across platforms.

The checkout is visible to the macOS host and the Linux VM at once; the
environment holds compiled packages, so each platform gets its own directory
and an environment stamped for another platform or interpreter is refused.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tools import cli_dependencies

ROOT = Path(__file__).resolve().parents[1]


def _build(env: Path, stamp: str | None) -> Path:
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    (env / "lib" / version / "site-packages").mkdir(parents=True)
    if stamp is not None:
        (env / cli_dependencies.STAMP).write_text(stamp, encoding="utf-8")
    return env


def test_default_environment_is_keyed_by_platform() -> None:
    assert cli_dependencies.default_environment(ROOT) == (
        ROOT / ".cli-dependencies" / cli_dependencies.platform_key()
    )
    assert cli_dependencies.platform_key() == cli_dependencies.platform_key().lower()


def test_matching_environment_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _build(tmp_path / "env", cli_dependencies.stamp_text())
    monkeypatch.setenv("VONK_CLI_DEPENDENCY_ENV", str(env))
    found, reason = cli_dependencies.site_packages(ROOT)
    assert reason is None and found is not None and found.is_dir()


@pytest.mark.parametrize(
    "stamp",
    [
        None,
        "darwin-arm64 python3.14\n"
        if cli_dependencies.platform_key() != "darwin-arm64"
        else "linux-arm64 python3.14\n",
        f"{cli_dependencies.platform_key()} python2.7\n",
    ],
    ids=["unstamped", "other-platform", "other-interpreter"],
)
def test_mismatched_environment_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stamp: str | None
) -> None:
    env = _build(tmp_path / "env", stamp)
    monkeypatch.setenv("VONK_CLI_DEPENDENCY_ENV", str(env))
    found, reason = cli_dependencies.site_packages(ROOT)
    assert found is None
    assert reason is not None and "scripts/sync-cli-dependencies" in reason


def test_missing_environment_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VONK_CLI_DEPENDENCY_ENV", str(tmp_path / "absent"))
    found, reason = cli_dependencies.site_packages(ROOT)
    assert found is None and reason is not None


def test_sync_script_keys_and_stamps_the_environment() -> None:
    script = (ROOT / "scripts" / "sync-cli-dependencies").read_text(encoding="utf-8")
    assert (
        '.cli-dependencies/$(python3 "$root/tools/cli_dependencies.py" key)' in script
    )
    assert "rm -rf" in script and ".vonk-platform" in script
    assert cli_dependencies.STAMP == ".vonk-platform"
