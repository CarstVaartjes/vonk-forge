"""Reject a raw TCP ``.accept()`` in vonk-agent test fixtures.

A development VM's port forwarder probes each new loopback listener with a
``GET /`` from ``Go-http-client``; a fixture that accepts first takes it for the
client under test. ``crate::loopback_probe::accept`` / ``accept_async`` drop that
probe and nothing else. A Unix-socket accept is exempt and says so with a
``// unix-socket`` comment on the same line.
"""

from __future__ import annotations

import re

KIND = (
    "raw TCP accept in a vonk-agent test fixture; use crate::loopback_probe::accept "
    "or accept_async (Unix sockets: add `// unix-socket`)"
)

_ACCEPT = re.compile(r"\.accept\(\)")
_OWNER = "rust/crates/vonk-agent/src/"


def scan_path(path: str, source: str, test_lines: set[int]) -> list[int]:
    if not path.startswith(_OWNER) or path.endswith("/loopback_probe.rs"):
        return []
    return [
        index
        for index, text in enumerate(source.splitlines(), 1)
        if index in test_lines
        and not text.lstrip().startswith("//")
        and _ACCEPT.search(text)
        and "// unix-socket" not in text
    ]
