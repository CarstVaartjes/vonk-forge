"""Reject a Rust test fixture that binds on 127.0.0.1.

OrbStack's port forwarder in the `vonk-ci` VM probes every new `127.0.0.1`
listener with one `GET /` (`Go-http-client`); a fixture that accepts first takes
it for the client under test. The rest of `127.0.0.0/8` reaches the same
loopback interface and is not probed, so fixtures bind on the test address
(`FIXTURE_BIND` / `FIXTURE_HOST` in vonk-agent `client/test_support.rs`).
Unix listeners bind a path, not an address, and never match.
"""

from __future__ import annotations

import re

KIND = (
    "test fixture binds on 127.0.0.1; bind the test address (FIXTURE_BIND, "
    "127.0.0.2) so the VM port probe cannot reach it"
)

_BIND = re.compile(
    r"\bbind\s*\(\s*(?:\(\s*)?(?:\"127\.0\.0\.1\b|[A-Za-z_:]*Ipv4Addr::LOCALHOST\b"
    r"|[A-Za-z_:]*Ipv4Addr::new\(\s*127\s*,\s*0\s*,\s*0\s*,\s*1\s*\))"
)


def scan_source(source: str, test_lines: set[int]) -> list[int]:
    return [
        index
        for index, text in enumerate(source.splitlines(), 1)
        if index in test_lines
        and not text.lstrip().startswith("//")
        and _BIND.search(text)
    ]
