from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cluster_profiles.fleet import ManagementEndpoint, NodeId
from cluster_profiles.fleet.install_contracts import (
    InstallationJournal,
    InstallationRequest,
)
from cluster_profiles.install.records import RecordError, emit_node_record


def _journal(*, accepted: bool) -> InstallationJournal:
    now = datetime(2026, 8, 3, tzinfo=UTC)
    journal = InstallationJournal.start(
        InstallationRequest(
            node_id=NodeId.parse("spk_0123456789abcdef0123456789abcdef"),
            display_name='lab "alpha"',
            endpoint=ManagementEndpoint(
                "node.local",
                "admin",
                2222,
                "secret://ssh/admin",
            ),
            labels={"zone": "west", "purpose": "inference"},
        ),
        at=now,
    )
    if not accepted:
        return journal
    states = (
        "identity-gated",
        "inventoried",
        "key-installed",
        "hardened",
        "policy-applied",
        "post-inventoried",
        "accepted",
    )
    for index, state in enumerate(states, 1):
        journal = journal.advance(
            state,
            evidence_digest=f"{index:x}" * 64,
            at=now + timedelta(seconds=index),
        )
    return journal


def test_emit_node_record_is_deterministic_and_sanitized() -> None:
    journal = _journal(accepted=True)

    first = emit_node_record(journal, hostname="runtime-name")
    second = emit_node_record(journal, hostname="runtime-name")

    assert first == second
    assert b"secret://ssh/admin" not in first
    assert b"credential_ref" not in first
    assert b'display_name = "lab \\"alpha\\""' in first
    assert b'hostname = "runtime-name"' in first
    assert b'purpose = "inference"' in first
    assert b'zone = "west"' in first


def test_unaccepted_install_cannot_emit_record() -> None:
    with pytest.raises(RecordError, match="accepted"):
        emit_node_record(_journal(accepted=False))


def test_observed_hostname_is_validated() -> None:
    journal = _journal(accepted=True)

    with pytest.raises(RecordError, match="hostname"):
        emit_node_record(journal, hostname="bad\nname")
