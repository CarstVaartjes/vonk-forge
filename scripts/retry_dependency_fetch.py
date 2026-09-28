"""Bounded retries for dependency acquisition, never builds or test execution."""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

# Every command here only acquires locked or digest-pinned inputs. A command
# is matched on its executable name, so a path such as
# control/web/node_modules/.bin/playwright is matched as ``playwright``.
_COMMANDS = (
    ("uv", "sync"),
    ("skopeo", "inspect"),
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
    r"digest mismatch|no solution found|failed to build",
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


def main(command: list[str] | None = None) -> int:
    command = sys.argv[1:] if command is None else command
    words = (Path(command[0]).name, *command[1:]) if command else ()
    if not any(tuple(words[: len(prefix)]) == prefix for prefix in _COMMANDS):
        print(
            "Only dependency acquisition may be retried: "
            + ", ".join(" ".join(prefix) for prefix in _COMMANDS),
            file=sys.stderr,
        )
        return 64
    for attempt in range(1, 4):
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        if result.returncode == 0:
            # A failed fetch may emit partial JSON. Never mix it into the
            # successful stdout consumed by a digest or attestation validator.
            print(result.stdout, end="")
            return 0
        if result.stdout:
            print(result.stdout, end="", file=sys.stderr)
        if attempt == 3 or not retryable(result.stdout + result.stderr):
            return result.returncode
        delay = 2**attempt
        print(
            f"Transient dependency fetch failure; retry {attempt + 1}/3 in {delay}s.",
            file=sys.stderr,
        )
        time.sleep(delay)
    raise AssertionError("retry loop must return")


if __name__ == "__main__":
    raise SystemExit(main())
