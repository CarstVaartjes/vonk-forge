"""Deterministic, non-mutating enrollment records for accepted nodes."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from cluster_profiles.fleet.install_contracts import InstallationJournal


class ProposalError(ValueError):
    pass


@dataclass(frozen=True)
class RepositoryProposal:
    base_commit: str
    target_path: str
    content: bytes
    sha256: str


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
        raise ProposalError("only an accepted installation can emit a node record")
    request = journal.request
    resolved_hostname = hostname or request.endpoint.host
    if not resolved_hostname.strip() or re.search(r"[\x00-\x20]", resolved_hostname):
        raise ProposalError("observed hostname is missing or unsafe")
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


def build_node_proposal(
    base_commit: str,
    accepted_journal: InstallationJournal,
    observations: Mapping[str, object],
) -> RepositoryProposal:
    """Reject retired Git Fleet proposals after validating caller inputs."""

    if accepted_journal.state != "accepted":
        raise ProposalError("only an accepted installation can emit a node record")
    if not base_commit.strip() or re.search(r"\s", base_commit):
        raise ProposalError("base commit must be a nonblank revision")
    hostname = observations.get("hostname")
    if not isinstance(hostname, str) or not hostname.strip():
        raise ProposalError("accepted observation must contain a hostname")
    if re.search(r"[\x00-\x20]", hostname):
        raise ProposalError("observed hostname is missing or unsafe")
    raise ProposalError(
        "Git Fleet proposals are retired; persist enrolled nodes in PostgreSQL"
    )
