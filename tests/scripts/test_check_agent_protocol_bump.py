"""The agent protocol changes only with a minor or major workspace bump."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev/check-agent-protocol-bump"


def _protocol(old: int, new: int) -> str:
    return (
        "--- a/agent_protocol/src/vonk_agent_protocol/claims.py\n"
        "+++ b/agent_protocol/src/vonk_agent_protocol/claims.py\n"
        "@@ -33 +33 @@\n"
        f"-AGENT_PROTOCOL_VERSION = {old}\n"
        f"+AGENT_PROTOCOL_VERSION = {new}\n"
        "--- a/rust/crates/vonk-agent/src/client/mod.rs\n"
        "+++ b/rust/crates/vonk-agent/src/client/mod.rs\n"
        "@@ -115 +115 @@\n"
        f"-pub const AGENT_PROTOCOL_VERSION: u32 = {old};\n"
        f"+pub const AGENT_PROTOCOL_VERSION: u32 = {new};\n"
    )


def _version(old: str, new: str) -> str:
    return (
        f'--- a/Cargo.toml\n+++ b/Cargo.toml\n@@ -14 +14 @@\n-version = "{old}"\n'
        f'+version = "{new}"\n'
    )


def _check(patch: str) -> int:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=patch,
        text=True,
        capture_output=True,
        check=False,
    ).returncode


@pytest.mark.parametrize(
    ("patch", "accepted"),
    [
        (_protocol(4, 5), False),
        (_protocol(4, 5) + _version("1.0.0", "1.0.1"), False),
        (_protocol(4, 5) + _version("1.0.1", "1.1.0"), True),
        (_protocol(4, 5) + _version("0.1.1", "1.0.0"), True),
        (_version("1.0.0", "1.0.1"), True),
        ("", True),
    ],
)
def test_protocol_change_needs_a_minor_or_major_bump(
    patch: str, accepted: bool
) -> None:
    assert (_check(patch) == 0) is accepted
