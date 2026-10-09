"""Public Activity reads reconcile peer disagreement within one finite request."""

from collections import Counter

import pytest
from vonk_agent_protocol import LifecycleState
from vonk_control.operation_api.contracts import (
    JobProgress,
    OperationApiServices,
    OperationListPage,
    OperationPage,
    OperationProvider,
)
from vonk_control.operation_contract import OperationRecoveryAction
from vonk_control.operation_item_contract import OperationItem

from .test_operation_api import NODE_ID, _client


@pytest.mark.parametrize("clears", [False, True])
@pytest.mark.parametrize("collection", [False, True])
def test_activity_reads_reobserve_duplicate_owners_before_exposing_actions(
    clears, collection
):
    calls = Counter()
    stable = OperationItem(
        id="shared-operation",
        kind="run",
        state=LifecycleState.RUNNING.value,
        attempt=1,
        node_ids=[NODE_ID],
        created_at="2026-08-15T12:00:00Z",
        supported_actions=[OperationRecoveryAction.RESUME.value],
    )
    stale = stable.model_copy(update={"state": LifecycleState.FAILED.value})

    def observe_left(_operation_id):
        calls["left"] += 1
        return stable

    def observe_right(_operation_id):
        calls["right"] += 1
        return stable if calls["right"] > (1 if clears else 3) else stale

    def list_rows(_query):
        return OperationListPage(items=[stable], next_cursor=None, total=1)

    def list_right(_query):
        observed = stable if calls["right"] > (1 if clears else 3) else stale
        return OperationListPage(items=[observed], next_cursor=None, total=1)

    services = OperationApiServices(
        agents=lambda: (),
        job_operations=lambda *_args: OperationPage(
            (), None, JobProgress(completed=0, failed=0, running=0, total=0)
        ),
        resume_job=lambda _job_id: None,
        operation_providers=(
            OperationProvider("run", list_rows, observe_left),
            OperationProvider("run", list_right, observe_right),
        ),
    )
    client, operator, *_ = _client(operations=services)
    path = "/api/operations" if collection else "/api/operations/shared-operation"
    response = client.get(path, headers=operator)
    assert response.status_code == 200
    detail = response.json()["operations"][0] if collection else response.json()
    assert detail["id"] == stable.id and detail["node_ids"] == [NODE_ID]
    assert calls == {"left": 2 if clears else 3, "right": 2 if clears else 3}
    if clears:
        assert detail["state"] == stable.state
        assert detail["recovery"]["actions"] == [
            OperationRecoveryAction.INSPECT.value,
            OperationRecoveryAction.RESUME.value,
        ]
    else:
        assert detail["recovery"]["actions"] == [OperationRecoveryAction.INSPECT.value]
        assert detail["state"] not in {stable.state, stale.state}
    fresh = client.get(path, headers=operator)
    assert fresh.status_code == 200
    fresh_detail = fresh.json()["operations"][0] if collection else fresh.json()
    assert fresh_detail["state"] == stable.state
    assert fresh_detail["recovery"]["actions"] == [
        OperationRecoveryAction.INSPECT.value,
        OperationRecoveryAction.RESUME.value,
    ]


def test_activity_detail_retains_known_state_while_an_independent_peer_is_unreadable():
    calls = Counter()
    stable = OperationItem(
        id="shared-operation",
        kind="run",
        state=LifecycleState.RUNNING.value,
        attempt=1,
        node_ids=[NODE_ID],
        created_at="2026-08-15T12:00:00Z",
        supported_actions=[OperationRecoveryAction.RESUME.value],
    )

    def unreadable(_operation_id):
        calls["peer"] += 1
        if calls["peer"] <= 3:
            raise OSError("peer observation unavailable")
        return stable

    services = OperationApiServices(
        agents=lambda: (),
        job_operations=lambda *_args: OperationPage(
            (), None, JobProgress(completed=0, failed=0, running=0, total=0)
        ),
        resume_job=lambda _job_id: None,
        operation_providers=(
            OperationProvider(
                "run",
                lambda _query: OperationListPage(items=[], next_cursor=None, total=0),
                lambda _id: stable,
            ),
            OperationProvider(
                "run",
                lambda _query: OperationListPage(items=[], next_cursor=None, total=0),
                unreadable,
            ),
        ),
    )
    client, operator, *_ = _client(operations=services)
    response = client.get("/api/operations/shared-operation", headers=operator)
    assert response.status_code == 200
    assert calls["peer"] == 3
    assert response.json()["state"] == stable.state
    assert response.json()["recovery"]["actions"] == [
        OperationRecoveryAction.INSPECT.value
    ]
    fresh = client.get("/api/operations/shared-operation", headers=operator)
    assert fresh.status_code == 200
    assert fresh.json()["state"] == stable.state
    assert fresh.json()["recovery"]["actions"] == [
        OperationRecoveryAction.INSPECT.value,
        OperationRecoveryAction.RESUME.value,
    ]
