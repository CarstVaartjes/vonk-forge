from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cluster_profiles.fleet import ManagementEndpoint, NodeId
from cluster_profiles.fleet.install_contracts import (
    InstallationJournal,
    InstallationRequest,
)
from cluster_profiles.install.proposal import (
    ProposalError,
    build_node_proposal,
)


def _journal(*, accepted: bool) -> InstallationJournal:
    now = datetime(2026, 8, 3, tzinfo=UTC)
    journal = InstallationJournal.start(
        InstallationRequest(
            node_id=NodeId.parse("spk_0123456789abcdef0123456789abcdef"),
            display_name='lab "alpha"',
            endpoint=ManagementEndpoint("node.local", "admin", 2222, "secret://ssh/admin"),
            labels={"zone": "west", "purpose": "inference"},
        ),
        at=now,
    )
    if not accepted:
        return journal
    states = (
        "identity-gated", "inventoried", "key-installed", "hardened",
        "policy-applied", "post-inventoried", "accepted",
    )
    for index, state in enumerate(states, 1):
        journal = journal.advance(state, evidence_digest=f"{index:x}" * 64, at=now + timedelta(seconds=index))
    return journal


def test_unaccepted_install_cannot_emit_proposal() -> None:
    with pytest.raises(ProposalError, match="accepted"):
        build_node_proposal("abc123", _journal(accepted=False), {})


def test_git_fleet_proposals_are_retired_after_acceptance() -> None:
    with pytest.raises(ProposalError, match="PostgreSQL"):
        build_node_proposal(
            "abc123",
            _journal(accepted=True),
            {"hostname": "runtime-name"},
        )


def test_observed_hostname_is_required_and_validated() -> None:
    journal = _journal(accepted=True)
    with pytest.raises(ProposalError, match="hostname"):
        build_node_proposal("abc123", journal, {})
    with pytest.raises(ProposalError, match="hostname"):
        build_node_proposal("abc123", journal, {"hostname": "bad\nname"})
