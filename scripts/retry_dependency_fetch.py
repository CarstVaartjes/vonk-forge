"""Bounded retries for dependency acquisition, never builds or test execution."""

from __future__ import annotations

import re
import subprocess
import sys
import time

_COMMANDS = (("uv", "sync"), ("skopeo", "inspect"), ("docker", "buildx", "imagetools", "inspect"))
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
    r"\b(?:429|500|502|503|504) (?:too many requests|internal server error|"
    r"bad gateway|service unavailable|gateway timeout)\b",
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
    if not any(tuple(command[:len(prefix)]) == prefix for prefix in _COMMANDS):
        print("Only uv sync, skopeo inspect and docker buildx imagetools inspect may be retried.", file=sys.stderr)
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
        delay = 2 ** attempt
        print(f"Transient dependency fetch failure; retry {attempt + 1}/3 in {delay}s.", file=sys.stderr)
        time.sleep(delay)
    raise AssertionError("retry loop must return")


if __name__ == "__main__":
    raise SystemExit(main())
