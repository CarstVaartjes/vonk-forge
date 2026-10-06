"""Where the installed-CLI tests find the CLI's locked dependencies.

The environment holds compiled, platform-specific packages (rpds, pydantic
core, ...). A checkout can be seen from several machines at once (macOS host
and the OrbStack ``vonk-ci`` VM share files), so one fixed directory would be
rebuilt by whichever OS ran last and then linked into a venv of the other.

The environment therefore lives in a directory keyed by OS and architecture,
and ``scripts/sync-cli-dependencies`` stamps it with the platform and
interpreter it was built for. Consumers accept an environment only when the
stamp matches the running interpreter; a mismatch is never used, only
rebuilt by the sync script.

Standard library only: the sync script runs this file with the system python.
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

STAMP = ".vonk-platform"
_ARCHITECTURES = {
    "arm64": "arm64",
    "aarch64": "arm64",
    "x86_64": "x86_64",
    "amd64": "x86_64",
}


def platform_key() -> str:
    """OS and architecture of this machine, e.g. ``darwin-arm64``."""

    machine = platform.machine().lower()
    return f"{sys.platform.rstrip('0123456789')}-{_ARCHITECTURES.get(machine, machine)}"


def stamp_text() -> str:
    """What a matching environment's stamp contains for this interpreter."""

    return f"{platform_key()} python{sys.version_info.major}.{sys.version_info.minor}\n"


def default_environment(root: Path) -> Path:
    return root / ".cli-dependencies" / platform_key()


def environment(root: Path) -> Path:
    override = os.environ.get("VONK_CLI_DEPENDENCY_ENV")
    return Path(override) if override else default_environment(root)


def mismatch(env: Path) -> str | None:
    """Why ``env`` cannot be linked into this interpreter's venv, else None."""

    if not env.is_dir():
        return f"{env} does not exist"
    stamp = env / STAMP
    found = stamp.read_text(encoding="utf-8") if stamp.is_file() else None
    if found != stamp_text():
        return (
            f"{env} was built for {found.strip() if found else 'an unknown platform'}, "
            f"this is {stamp_text().strip()}"
        )
    if not any(
        env.glob(
            f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
        )
    ):
        return f"{env} has no site-packages for this interpreter"
    return None


def site_packages(root: Path) -> tuple[Path | None, str | None]:
    """The matching site-packages, or None and the reason it is unusable."""

    env = environment(root)
    reason = mismatch(env)
    if reason:
        return None, f"{reason}; run scripts/sync-cli-dependencies"
    return next(env.glob("lib/python3.*/site-packages")), None


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "key":
        print(platform_key())
    elif command == "stamp":
        print(stamp_text(), end="")
    else:
        sys.exit("usage: cli_dependencies.py key|stamp")
