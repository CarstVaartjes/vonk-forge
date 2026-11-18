"""Keep the per-job peer secret's name inside the files that own its value.

The secret reaches a container through one path: the Controller's grant
authority, the signed grant field, and the helper's launch. A line naming
``peer_secret`` or ``VONK_PEER_SECRET`` anywhere else is a new place that could
hold, copy or log the value (a payload, a plan, a log call, an evidence field).
Add the line to an owner file, or add the file here with the reason.
"""

from __future__ import annotations

import re

KIND = "peer secret named outside the files that own its value"
NAMES = re.compile(r"peer_secret|PEER_SECRET", re.IGNORECASE)
OWNERS = frozenset(
    {
        "control/src/vonk_control/peer_secret.py",
        "control/src/vonk_control/host_helper_authority.py",
        "control/src/vonk_control/agent_services.py",
        "control/src/vonk_control/harnesses/canonical.py",
        "agent_protocol/src/vonk_agent_protocol/host_helper.py",
        "rust/crates/vonk-agent-protocol/src/host_runtime.rs",
        "rust/crates/vonk-agent-helper/src/operations/dispatch.rs",
        "rust/crates/vonk-agent-helper/src/operations/mod.rs",
        "rust/crates/vonk-agent-helper/src/operations/start.rs",
        "rust/crates/vonk-agent-helper/src/operations/validation.rs",
        "rust/crates/vonk-agent-helper/src/operations/arguments.rs",
        # A wire-shape probe building the grant struct; it holds no value.
        "rust/crates/vonk-agent/examples/privileged_oci_process_probe.rs",
    }
)


def in_scope(path: str) -> bool:
    """Code and data files that are not an owner (documents describe it freely)."""
    return (
        path not in OWNERS
        and not path.startswith("agent_protocol/fixtures/")
        and not path.endswith(".md")
    )


def scan_source(source: str, skip: set[int]) -> list[int]:
    return [
        number
        for number, text in enumerate(source.splitlines(), 1)
        if number not in skip and NAMES.search(text)
    ]
