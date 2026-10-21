"""The same deterministic evidence, repeated, is shown as fleet attention.

Production evidence: every job on one Spark failed with the same code for
weeks, a refused runtime grant repeated on every Stop, and one profile retry
was refused identically 139 times in three hours, while every operator surface
stayed quiet.  Repetition is shown on the Spark as ``evidence.repeating`` once
it reaches the one failure limit; it never ends or blocks the work, and the
next success clears it.  A repetition about lasting work stays shown however
old it is; only a profile load, which can end without succeeding, ages out.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text
from vonk_agent_protocol import (
    FailureCode,
    ProjectionCode,
    SecurityRefusalReason,
    WaitReason,
)
from vonk_control.fleet_projection import FleetProjection
from vonk_control.host_helper_authority import HostHelperAuthorityError
from vonk_control.models import AgentNode
from vonk_control.recovery_policy import RECOVERY
from vonk_control.settings import PROFILE_STALL_WINDOW_SECONDS

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
WINDOW = timedelta(seconds=PROFILE_STALL_WINDOW_SECONDS)
MONTHS = timedelta(days=60)


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
    # A load can end without ever succeeding: its repetition ages out.
    assert _repeating(sessions, nodes[0], now + WINDOW + timedelta(minutes=1)) == []

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


@pytest.mark.parametrize(
    "report,code",
    [
        (
            {
                "state": "failed",
                "result": {
                    "status": "failed",
                    "error_code": FailureCode.RECIPE_STOP_FAILED,
                },
            },
            FailureCode.RECIPE_STOP_FAILED,
        ),
        # "job stop unconfirmed": the agent could not confirm the effect.
        (
            {
                "state": "observing",
                "result": {
                    "kind": "unknown",
                    "wait_reason": WaitReason.STOP_UNCONFIRMED,
                    "reason": "workload stop remains unconfirmed",
                },
            },
            WaitReason.STOP_UNCONFIRMED,
        ),
    ],
)
def test_every_stop_failing_with_the_same_code_is_attention_until_one_succeeds(
    tmp_path, report, code
) -> None:
    client, services, _codec, clock = make_agent_system(tmp_path)
    for _ in range(LIMIT):
        assert _repeating(services.sessions, NODE_A, clock.now) == []
        # Each Stop is a new order: the failure never holds back the next one.
        _report_order(client, services, clock, report)

    [alarm] = _repeating(services.sessions, NODE_A, clock.now)
    assert code.value in alarm
    assert f"x{LIMIT}" in alarm
    # Lasting work stays escalated until it succeeds, however long that takes.
    assert _repeating(services.sessions, NODE_A, clock.now + MONTHS) == [alarm]

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
        assert response.headers["x-vonk-error-code"] == (
            SecurityRefusalReason.HOST_HELPER_AUTHORITY_DENIED.value
        )

    [alarm] = _repeating(services.sessions, NODE_A, clock.now)
    assert SecurityRefusalReason.HOST_HELPER_AUTHORITY_DENIED.value in alarm
    assert _repeating(services.sessions, NODE_A, clock.now + MONTHS) == [alarm]


def test_an_attention_code_from_a_newer_controller_still_renders_in_the_cli(
    tmp_path, capsys
) -> None:
    from cluster_profiles.cli_render import render_payload
    from cluster_profiles.control_client import validate_control_document

    client, services, _codec, clock = make_agent_system(tmp_path)
    failed = {
        "state": "failed",
        "result": {"status": "failed", "error_code": FailureCode.RECIPE_STOP_FAILED},
    }
    for _ in range(LIMIT):
        _report_order(client, services, clock, failed)
    snapshot = FleetProjection(services.sessions, clock=lambda: clock.now).read()
    document = snapshot.model_dump(mode="json")
    [node] = [item for item in document["nodes"] if item["id"] == NODE_A]
    [warning] = [
        item
        for item in node["warnings"]
        if item["code"] == ProjectionCode.EVIDENCE_REPEATING
    ]
    # A code this CLI's contract has never seen is still readable attention.
    warning["code"] = "attention.from-a-newer-controller"

    validate_control_document("FleetSnapshot", document)
    render_payload(document, "fleet")
    rendered = capsys.readouterr().out
    assert warning["detail"] in rendered[rendered.index("Needs attention") :]
