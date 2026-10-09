"""Bounded retries for dependency acquisition, never builds or test execution."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from random import uniform
from time import sleep

# Every command here only acquires locked or digest-pinned inputs. A command
# is matched on its executable name, so a path such as
# control/web/node_modules/.bin/playwright is matched as ``playwright``.
# Three five-minute attempts fit the existing 20-minute acquisition lanes.
FETCH_TIMEOUT_SECONDS = 5 * 60

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
_TRANSIENT = re.compile(
    r"connection reset by peer|connection timed out|operation timed out|"
    r"temporary failure in name resolution|unexpected eof|tls handshake timeout|"
    r"could not resolve host|early eof|rpc failed|remote end hung up unexpectedly|"
    r"\b(?:econnreset|etimedout|eai_again|econnrefused)\b|socket hang up|"
    r"spurious network error|\b(?:429|500|502|503|504) (?:too many requests|internal server error|"
    r"bad gateway|service unavailable|gateway time-?out)\b",
    re.IGNORECASE,
)


def retryable(output: str) -> bool:
    if _PERMANENT.search(output):
        return False
    return bool(_TRANSIENT.search(output)) or (
        "git operation failed" in output.lower() and "failed to fetch" in output.lower()
    )


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
    for attempt in range(1, 4):
        timed_out = False
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=FETCH_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            timed_out = True

            # run() kills and reaps the expired child. Keep partial output on
            # stderr and let the existing bounded acquisition loop retry.
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
                f"Dependency acquisition attempt {attempt}/3 exceeded "
                f"{FETCH_TIMEOUT_SECONDS}s ({words[0]}).",
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
        if (
            attempt == 3
            or _PERMANENT.search(output)
            or (
                not timed_out
                and words[0] not in {"docker", "skopeo"}
                and not retryable(output)
            )
        ):
            return result.returncode
        delay = (
            uniform(0, 2**attempt) if words[0] in {"docker", "skopeo"} else 2**attempt
        )
        print(
            f"Transient dependency fetch failure; retry {attempt + 1}/3 in {delay:.2f}s.",
            file=sys.stderr,
        )
        sleep(delay)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
