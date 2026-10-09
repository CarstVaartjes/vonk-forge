"""Bounded retries for dependency acquisition, never builds or test execution."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from random import uniform
from time import monotonic, sleep

# Every command here only acquires locked or digest-pinned inputs. A command
# is matched on its executable name, so a path such as
# control/web/node_modules/.bin/playwright is matched as ``playwright``.
# Each attempt is capped at five minutes and all attempts together at fifteen,
# which fits the existing 20-minute acquisition lanes.
FETCH_TIMEOUT_SECONDS = 5 * 60
FETCH_DEADLINE_SECONDS = 15 * 60
MAX_ATTEMPTS = 6
BACKOFF_BASE_SECONDS = 2
BACKOFF_CAP_SECONDS = 60

_COMMANDS = (
    ("uv", "sync"),
    ("skopeo", "inspect"),
    ("skopeo", "copy"),
    ("docker", "pull"),
    ("docker", "buildx", "imagetools", "inspect"),
    ("git", "fetch"),
    ("npm", "ci"),
    ("cargo", "fetch"),
    ("rustup", "toolchain", "install"),
    ("playwright", "install"),
)
_PERMANENT = re.compile(
    r"authentication failed|unauthorized|forbidden|permission denied|access denied|"
    r"repository(?: [^\n]+)? not found|not our ref|couldn't find remote ref|"
    r"could not read (?:username|password)|terminal prompts disabled|manifest unknown|"
    r"certificate verify failed|certificate signed by unknown authority|"
    r"digest mismatch|hash mismatch|hashes are required|no solution found|failed to build",
    re.IGNORECASE,
)


def retryable(output: str) -> bool:
    """Every input here is locked or digest-pinned, so only a refusal at the
    registry or integrity edge is final; any other failure, recognised or not,
    is retried within the deadline."""
    return not _PERMANENT.search(output)


def backoff(attempt: int) -> float:
    """Capped exponential backoff with full jitter (AWS Builders' Library)."""
    return uniform(1, min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS**attempt))


def locked_temporary_pip_acquisition(words: tuple[str, ...]) -> bool:
    """Only the declared hash-locked resolver-cache preparation may retry."""
    if (
        len(words) != 9
        or words[:4] != ("uv", "pip", "install", "--python")
        or words[5:8] != ("--reinstall", "--require-hashes", "-r")
    ):
        return False
    temporary = os.environ.get("RUNNER_TEMP")
    cache = os.environ.get("UV_CACHE_DIR")
    if not temporary or not cache:
        return False
    try:
        temporary_root = Path(temporary)
        python = Path(words[4])
        requirements = Path(words[8])
        cache_root = Path(cache)
        if not all(
            path.is_absolute()
            for path in (temporary_root, python, requirements, cache_root)
        ):
            return False
        temporary_root = temporary_root.resolve()
        environment_root = python.parent.parent.resolve()
        return (
            python.parent.name == "bin"
            and python.name == "python"
            and environment_root.is_relative_to(temporary_root)
            and environment_root != temporary_root
            and (environment_root / "pyvenv.cfg").is_file()
            and python.is_file()
            and requirements.is_file()
            and requirements.resolve().is_relative_to(temporary_root)
            and cache_root.resolve().is_relative_to(temporary_root)
            and cache_root.resolve() != temporary_root
        )
    except (OSError, ValueError):
        return False


def main(command: list[str] | None = None) -> int:
    command = sys.argv[1:] if command is None else command
    words = (Path(command[0]).name, *command[1:]) if command else ()
    if not (
        any(tuple(words[: len(prefix)]) == prefix for prefix in _COMMANDS)
        or locked_temporary_pip_acquisition(words)
    ):
        print(
            "Only dependency acquisition may be retried: "
            + ", ".join(" ".join(prefix) for prefix in _COMMANDS)
            + "; hash-locked uv pip install into a declared RUNNER_TEMP venv/cache",
            file=sys.stderr,
        )
        return 64
    deadline = monotonic() + FETCH_DEADLINE_SECONDS
    for attempt in range(1, MAX_ATTEMPTS + 1):
        timeout = max(1.0, min(FETCH_TIMEOUT_SECONDS, deadline - monotonic()))
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            # run() kills and reaps the expired child. Keep partial output on
            # stderr and let the bounded acquisition loop retry.
            def text_output(output: str | bytes | None) -> str:
                return (
                    output.decode(errors="replace")
                    if isinstance(output, bytes)
                    else (output or "")
                )

            result = subprocess.CompletedProcess(
                command, 124, text_output(error.stdout), text_output(error.stderr)
            )
            print(
                f"Dependency acquisition attempt {attempt}/{MAX_ATTEMPTS} exceeded "
                f"{timeout:.0f}s ({words[0]}).",
                file=sys.stderr,
            )
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        if result.returncode == 0:
            # A failed fetch may emit partial JSON. Never mix it into the
            # successful stdout consumed by a digest or attestation validator.
            print(result.stdout, end="")
            return 0
        if result.stdout:
            print(result.stdout, end="", file=sys.stderr)
        output = result.stdout + result.stderr
        delay = backoff(attempt)
        if (
            attempt == MAX_ATTEMPTS
            or not retryable(output)
            or monotonic() + delay >= deadline
        ):
            return result.returncode
        print(
            f"Dependency fetch failed; retry {attempt + 1}/{MAX_ATTEMPTS} in {delay:.0f}s.",
            file=sys.stderr,
        )
        sleep(delay)
    raise AssertionError("retry loop must return")


if __name__ == "__main__":
    raise SystemExit(main())
