"""The closed state words of Controller-only stored records.

These records never cross the wire, so their enums live here and not in
``vonk_agent_protocol.state_machines`` (whose machines are published through
``wire.json`` into Rust, OpenAPI and TypeScript).  Each is a ``WireEnum`` so its
CHECK is generated with ``machine_check`` and no second spelling exists.
"""

from __future__ import annotations

from vonk_agent_protocol.wire_model import WireEnum


class CatalogRevisionState(WireEnum):
    """The standing of one catalog document revision."""

    CANDIDATE = "candidate"
    ACTIVE = "active"
    FAILED = "failed"
