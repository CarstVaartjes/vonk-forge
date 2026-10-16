"""Every Spark-side "not valid yet" check tolerates a Spark clock that lags.

The Controller signs what a Spark checks against its own clock (host-helper
grants, node certificates). A check that refuses anything issued in the
Spark's future, with no allowance, refuses fresh documents on a Spark whose
clock is a fraction of a second behind: spark-2297 refused three fresh grants
on 2026-10-16 (``helper_grant_unauthorized``) and each retry passed.

Each such check is listed here with the test that proves its allowance. A new
check fails this guard until it is listed, with its own proof.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPARK_SOURCES = (
    ROOT / "rust/crates/vonk-agent/src",
    ROOT / "rust/crates/vonk-agent-helper/src",
    ROOT / "rust/crates/vonk-agent-protocol/src",
)

#: A comparison that refuses a document whose validity starts after "now".
NOT_YET_VALID = re.compile(
    r"\bnow\b[^<\n;]*<\s*[\w.()]*\b(?:issued_at|not_before|nbf)\b"
    r"|\b(?:issued_at|not_before|nbf)\b[^>\n;]*>\s*[\w.()]*\bnow\b"
)

#: source -> (how the lag is tolerated, the test that proves it)
SITES = {
    "rust/crates/vonk-agent-helper/src/protocol.rs": (
        "the Controller starts a grant HOST_HELPER_GRANT_CLOCK_SKEW_SECONDS early",
        "control/tests/test_host_helper_authority.py",
        "test_a_spark_whose_clock_lags_accepts_a_fresh_grant",
    ),
    "rust/crates/vonk-agent/src/identity.rs": (
        "the agent allows CERTIFICATE_CLOCK_SKEW_SECONDS before not_before",
        "rust/crates/vonk-agent/src/identity.rs",
        "a_fresh_certificate_is_valid_on_a_clock_that_lags_its_issuer",
    ),
}


def _is_test_source(path: Path) -> bool:
    parts = path.relative_to(ROOT).parts
    return path.name in {"tests.rs", "test_support.rs"} or any(
        part in {"tests", "test_support"} for part in parts
    )


def _production_lines(path: Path) -> list[str]:
    """The file's lines before its in-file ``#[cfg(test)]`` module, if any."""
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if (
            line.strip() == "#[cfg(test)]"
            and index + 1 < len(lines)
            and (lines[index + 1].lstrip().startswith("mod "))
        ):
            return lines[:index]
    return lines


def _not_yet_valid_sites() -> set[str]:
    found: set[str] = set()
    for root in SPARK_SOURCES:
        for path in sorted(root.rglob("*.rs")):
            if _is_test_source(path):
                continue
            if any(NOT_YET_VALID.search(line) for line in _production_lines(path)):
                found.add(path.relative_to(ROOT).as_posix())
    return found


def test_every_not_yet_valid_check_is_listed_with_its_allowance() -> None:
    assert _not_yet_valid_sites() == set(SITES)


def test_every_listed_allowance_has_its_proof() -> None:
    for site, (_allowance, proof, name) in SITES.items():
        text = (ROOT / proof).read_text(encoding="utf-8")
        assert re.search(rf"\b(?:def|fn) {name}\(", text), f"{site}: {proof}::{name}"


def test_the_scan_sees_a_check_without_an_allowance() -> None:
    for line in (
        "            || now < grant.claims.issued_at",
        "        if now + skew < not_before || now >= not_after {",
        "    if claims.issued_at > now {",
    ):
        assert NOT_YET_VALID.search(line)
    assert not NOT_YET_VALID.search("            || now >= grant.claims.expires_at")
