"""The same deterministic evidence, repeated, is shown as fleet attention.

Production evidence: every job on one Spark failed with the same code for
weeks, a refused runtime grant repeated on every Stop, and one profile retry
was refused identically 139 times in three hours, while every operator surface
stayed quiet.  Repetition is shown on the Spark as ``evidence.repeating`` once
it reaches the one failure limit; it never ends or blocks the work, and the
next success clears it.
"""

from __future__ import annotations

from sqlalchemy import text
from vonk_agent_protocol import FailureCode, ProjectionCode, SecurityRefusalReason
from vonk_control.fleet_projection import FleetProjection
from vonk_control.host_helper_authority import HostHelperAuthorityError
from vonk_control.models import AgentNode
from vonk_control.recovery_policy import RECOVERY

from .test_agent_api import (
    NODE_A,
    STOP_PAYLOAD,
    agent_headers,
    make_agent_system,
    parent,
)
from .test_fleet_profile_recovery_current import _failed_profile
from .test_fleet_profiles import _uuid
from .test_host_runtime_grant_answers import _BODY

LIMIT = RECOVERY.max_failures
EXTRA = "spk_" + "9" * 32


def _repeating(sessions, node_id: str, now) -> list[str]:
    snapshot = FleetProjection(sessions, clock=lambda: now).read()
    node = next(item for item in snapshot.nodes if item.id == node_id)
    return [
        warning.detail
        for warning in node.warnings
        if warning.code is ProjectionCode.EVIDENCE_REPEATING
    ]


def test_a_profile_retry_declined_the_same_way_is_attention_until_one_is_admitted(
    tmp_path,
) -> None:
    sessions, lifecycle, service, _profile, _desired, first, _child, nodes = (
        _failed_profile(tmp_path)
    )
    now = lifecycle._clock()
    # A Spark joins the fleet: the accepted scope changed, every retry declines.
    with sessions.begin() as session:
        session.add(AgentNode(node_id=EXTRA, state="active", last_seen_at=now))
    for attempt in range(LIMIT):
        assert _repeating(sessions, nodes[0], now) == []
        declined = service.retry(
            first.id, request_key=_uuid(3100 + attempt), actor="admin"
        )
        # The decline neither ends nor blocks the load: it keeps backing off.
        assert declined.id == first.id
        assert service.retry_eligible(first.id)

    alarms = [_repeating(sessions, node_id, now) for node_id in nodes[:2]]
    assert all(len(alarm) == 1 and f"x{LIMIT}" in alarm[0] for alarm in alarms)
    assert first.id in alarms[0][0]

    with sessions.begin() as session:
        session.delete(session.get(AgentNode, EXTRA))
    admitted = service.retry(first.id, request_key=_uuid(3200), actor="admin")
    assert admitted.retry_of_application_id == first.id
    assert _repeating(sessions, nodes[0], now) == []


def _report_order(client, services, clock, result: dict[str, object]) -> None:
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.stop",
        "a" * 64,
        STOP_PAYLOAD,
    )
    claim = client.post("/agent/claim", headers=agent_headers(NODE_A, "serial-a"))
    response = client.post(
        "/agent/result",
        headers=agent_headers(NODE_A, "serial-a"),
        json={"fence": claim.json()["fence"]} | result,
    )
    assert response.status_code in {202, 204}, response.text


def test_every_stop_failing_with_the_same_code_is_attention_until_one_succeeds(
    tmp_path,
) -> None:
    client, services, _codec, clock = make_agent_system(tmp_path)
    failed = {
        "state": "failed",
        "result": {"status": "failed", "error_code": FailureCode.RECIPE_STOP_FAILED},
    }
    for _ in range(LIMIT):
        assert _repeating(services.sessions, NODE_A, clock.now) == []
        # Each Stop is a new order: the failure never holds back the next one.
        _report_order(client, services, clock, failed)

    [alarm] = _repeating(services.sessions, NODE_A, clock.now)
    assert FailureCode.RECIPE_STOP_FAILED.value in alarm
    assert f"x{LIMIT}" in alarm

    _report_order(client, services, clock, {"state": "succeeded", "result": {}})
    assert _repeating(services.sessions, NODE_A, clock.now) == []


def test_an_unreadable_repeat_table_never_fails_the_report_or_the_fleet(
    tmp_path,
) -> None:
    client, services, _codec, clock = make_agent_system(tmp_path)
    with services.sessions.begin() as session:
        session.execute(text("DROP TABLE evidence_repeats"))
    failed = {
        "state": "failed",
        "result": {"status": "failed", "error_code": FailureCode.RECIPE_STOP_FAILED},
    }
    for _ in range(LIMIT):
        _report_order(client, services, clock, failed)
    assert _repeating(services.sessions, NODE_A, clock.now) == []


class _RefusingAuthority:
    def issue_grant(self, **_arguments: object) -> object:
        raise HostHelperAuthorityError("container runtime action authority is stale")


def test_a_runtime_grant_refused_on_every_ask_is_attention(tmp_path) -> None:
    client, services, _codec, clock = make_agent_system(tmp_path)
    object.__setattr__(services, "host_runtime_authority", _RefusingAuthority())
    services.operations.enqueue(
        parent(services.sessions, clock).id,
        NODE_A,
        "recipe.stop",
        "a" * 64,
        STOP_PAYLOAD,
    )
    claim = client.post("/agent/claim", headers=agent_headers(NODE_A, "serial-a"))
    body = _BODY | {"fence": claim.json()["fence"]}
    for _ in range(LIMIT):
        assert _repeating(services.sessions, NODE_A, clock.now) == []
        response = client.post(
            "/agent/host-runtime/grant",
            headers=agent_headers(NODE_A, "serial-a"),
            json=body,
        )
        assert response.status_code == 403

    [alarm] = _repeating(services.sessions, NODE_A, clock.now)
    assert SecurityRefusalReason.HOST_HELPER_AUTHORITY_DENIED.value in alarm
