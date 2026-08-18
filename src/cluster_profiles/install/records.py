"""Sanitized enrollment records for accepted nodes."""

from __future__ import annotations

import re

from cluster_profiles.fleet.install_contracts import InstallationJournal


class RecordError(ValueError):
    pass


def _quoted(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def emit_node_record(
    journal: InstallationJournal,
    *,
    hostname: str | None = None,
) -> bytes:
    """Render one accepted node table without credentials or evidence logs."""

    if journal.state != "accepted":
        raise RecordError("only an accepted installation can emit a node record")
    request = journal.request
    resolved_hostname = hostname or request.endpoint.host
    if not resolved_hostname.strip() or re.search(r"[\x00-\x20]", resolved_hostname):
        raise RecordError("observed hostname is missing or unsafe")
    prefix = f"nodes.{request.node_id.value}"
    lines = [
        f"[{prefix}]",
        f"display_name = {_quoted(request.display_name)}",
        f"hostname = {_quoted(resolved_hostname)}",
        'lifecycle = "ready"',
        "",
        f"[{prefix}.management]",
        f"host = {_quoted(request.endpoint.host)}",
        f"user = {_quoted(request.endpoint.user)}",
        f"port = {request.endpoint.port}",
        "",
        f"[{prefix}.labels]",
    ]
    lines.extend(
        f"{key} = {_quoted(value)}" for key, value in sorted(request.labels.items())
    )
    return ("\n".join(lines) + "\n").encode()
