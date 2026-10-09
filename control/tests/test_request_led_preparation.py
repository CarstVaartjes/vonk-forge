"""Missing assets are requested by the platform, never left to an operator."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from vonk_control import fleet_profiles as fp

ROOT = Path(__file__).resolve().parents[2]
#: Wording that sends an operator to do what the Controller prepares itself.
_OPERATOR_PREPARATION = re.compile(
    r"""(?<!")["'(]Prepare the (exact|named|model|runtime|asset)|[;,] prepare the (exact|named|model|runtime|asset)""",
)
_SCANNED = (
    # Whole trees: a module split into a package must stay in scope.
    *sorted((ROOT / "control/src/vonk_control").rglob("*.py")),
    *sorted((ROOT / "src/cluster_profiles").rglob("*.py")),
    ROOT / "docs/runbooks/development-agent-workloads.md",
)


def test_no_blocker_text_tells_an_operator_to_prepare_an_asset() -> None:
    """Ratchet: an asset the platform can prepare is requested, not asked for."""

    offenders = [
        f"{path.relative_to(ROOT)}:{number}"
        for path in _SCANNED
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if _OPERATOR_PREPARATION.search(line)
    ]
    assert offenders == []


@pytest.mark.parametrize("code", sorted(fp._PREPARATION_RESOLVABLE_CODES, key=str))
def test_every_preparation_blocker_requests_the_preparation(code: str) -> None:
    assignment = SimpleNamespace(
        assignment_id="a1", actions=("switch",), reasons=(), recipe_revision_id="r1"
    )
    reason = SimpleNamespace(code=code, severity="error")
    by_assessment = SimpleNamespace(
        assignment_id="a1", assessment=SimpleNamespace(blockers=[reason])
    )
    needing: Any = fp._assignments_needing_preparation
    assert needing([assignment], {"a1"}, [], [by_assessment]) == [assignment]
    assert needing([assignment], set(), [reason], []) == [assignment]


def test_ended_preparation_does_not_block_a_fresh_application(tmp_path) -> None:
    """A cancelled application's preparation cannot gate a new request."""
    from uuid import uuid4

    from vonk_agent_protocol import LifecycleState

    from .test_recipe_image_availability_request_recovery import _owner

    engine, _sessions, service, _now, _transport = _owner(tmp_path)
    service.ensure_preparation(
        "request-revision", actor="operator", application_id="old"
    )
    cancelled = service.cancel_profile_preparation(
        "request-revision",
        actor="operator",
        reason="application ended",
        application_id="old",
    )
    assert len(cancelled) == 1
    assert service.get(cancelled[0]).state == LifecycleState.CANCELLED
    service.ensure_preparation(
        "request-revision", actor="operator", application_id="new"
    )
    assert service.run_pending() == 1
    fresh = service.start("request-revision", actor="operator", request_id=str(uuid4()))
    assert service.run_pending() == 1
    assert service.get(fresh.id).artifact is not None
    engine.dispose()


def test_pending_profile_references_are_read_from_the_canonical_stored_plan() -> None:
    """A queued load retains its resolved revision after real plan serialization."""
    from vonk_control.fleet_profiles import FleetProfileService
    from vonk_control.unused_storage_collection import _applied_revision_ids

    from .test_fleet_profiles import NOW, _database, _input, _seed, _uuid

    sessions = _database()
    _, revision_id = _seed(sessions)

    def missing(_session, _assignment, _node_ids, **_kwargs):
        raise ValueError("no successful immutable runtime image build is available")

    service = FleetProfileService(
        sessions, clock=lambda: NOW, assessment_provider=missing
    )
    profile = service.create(_input(revision_id), actor="admin")
    application = service.apply(profile.id, request_key=_uuid(922), actor="admin")
    assert application.state == "queued"
    with sessions() as session:
        assert _applied_revision_ids(session) == frozenset({revision_id})
