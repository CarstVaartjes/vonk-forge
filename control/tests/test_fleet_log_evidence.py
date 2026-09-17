"""The operator log surface must retrieve the agent's own failure account.

A failed ``recipe.start`` reached the Controller with its reason, its stable
refusal code and its bounded diagnostics, but ``vonkctl fleet loginfo`` returned
an empty retained response because the projection only read Controller job-log
blobs.  These tests drive a real failed agent operation through the real read
path an operator uses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import Actor
from vonk_control.fleet_projection import FleetSnapshot
from vonk_control.logging import DatabaseJobLogStore
from vonk_control.models import AgentOperation, AgentOperationAttempt, Base
from vonk_control.operator_projection_api import (
    ControllerJobLogProvider,
    FleetOperatorServices,
    install_operator_projection_routes,
)

_NODE = "spk_" + "a" * 32
_NOW = datetime(2026, 9, 17, 12, 56, tzinfo=UTC)
_STABLE_CODE = "helper_request_argument_empty"
_REASON = f"container runtime could not start the workload: {_STABLE_CODE}"
_HELPER_DETAIL = "helper rejected: argv item is empty"


def _tail(text: str) -> dict[str, object]:
    return {
        "text": text,
        "truncated": False,
        "dropped_bytes": 0,
        "dropped_lines": 0,
    }


def _diagnostics() -> dict[str, object]:
    return {
        "schema_version": 1,
        "collected_at": _NOW.isoformat(),
        "phase": "starting",
        "category": "runtime",
        "stdout": _tail(""),
        "stderr": _tail(_HELPER_DETAIL),
        "versions": [],
        "sandbox": [],
        "storage": [],
        "preflight": [],
        "collector_errors": [],
    }


def _sessions(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'logs.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _failed_start(sessions, *, node_id: str = _NODE, preflight=None) -> str:
    diagnostics = _diagnostics()
    if preflight is not None:
        diagnostics["preflight"] = preflight
    operation_id = str(uuid4())
    with sessions.begin() as session:
        session.add(
            AgentOperation(
                id=operation_id,
                parent_job_id=str(uuid4()),
                node_id=node_id,
                kind="recipe.start",
                payload_digest="a" * 64,
                payload={},
                authority_revision="b" * 64,
                state="failed",
                status_reason=None,
                current_attempt=1,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        session.add(
            AgentOperationAttempt(
                id=str(uuid4()),
                operation_id=operation_id,
                attempt=1,
                fence=str(uuid4()),
                lease_deadline=_NOW,
                agent_certificate_serial="test-serial",
                state="failed",
                progress={"phase": "starting"},
                result={
                    "reason": _REASON,
                    "error_code": "recipe_start_failed",
                    "diagnostics": diagnostics,
                },
            )
        )
    return operation_id


class _FleetProjection:
    def read(self) -> FleetSnapshot:
        return cast(
            FleetSnapshot,
            SimpleNamespace(
                nodes=[
                    SimpleNamespace(
                        id=_NODE, display_name="Spark", hostname="spark.example.test"
                    )
                ]
            ),
        )


def _client(sessions) -> TestClient:
    app = FastAPI()
    install_operator_projection_routes(
        app,
        actor_dependency=Depends(lambda: Actor("viewer", "viewer")),
        fleet_projection=_FleetProjection(),
        library_projection=None,
        fleet_services=FleetOperatorServices(
            logs=ControllerJobLogProvider(
                sessions, DatabaseJobLogStore(sessions), clock=lambda: _NOW
            )
        ),
    )
    return TestClient(app)


def test_failed_start_is_retrievable_through_the_operator_log_path(tmp_path):
    # Wrong implementation: ``loginfo`` read only Controller job-log blobs and
    # returned an empty retained response for ``source=runtime``, so the agent's
    # own account of a failed ``recipe.start`` had no operator surface at all.
    sessions = _sessions(tmp_path)
    operation_id = _failed_start(sessions)
    client = _client(sessions)

    for params in ({"source": "runtime"}, {}):
        response = client.get(f"/api/fleet/{_NODE}/loginfo", params=params)
        assert response.status_code == 200, response.text
        payload = response.json()
        messages = [entry["message"] for entry in payload["entries"]]
        assert payload["retained"] is True
        assert payload["follow"] is False
        # The stable code is the only thing the protocol causes cross the wire
        # with, so its reason must be among the entries.
        assert any(_STABLE_CODE in message for message in messages), (params, messages)
        assert any("error_code=recipe_start_failed" in message for message in messages)
        # The bounded, already-sanitized process-log tail stays reachable.
        assert any(_HELPER_DETAIL in message for message in messages)
        assert {entry["evidence_id"] for entry in payload["entries"]} == {operation_id}
        assert {entry["source"] for entry in payload["entries"]} == {"runtime"}


def test_failed_start_is_also_available_without_a_source_filter(tmp_path):
    sessions = _sessions(tmp_path)
    operation_id = _failed_start(sessions)
    payload = _client(sessions).get(f"/api/fleet/{_NODE}/loginfo").json()
    assert payload["retained"] is True
    assert {entry["evidence_id"] for entry in payload["entries"]} == {operation_id}
    assert any(_STABLE_CODE in entry["message"] for entry in payload["entries"])


def test_sources_without_a_producer_report_absence_truthfully(tmp_path):
    # ``client`` and ``monitor`` have no producer in this build.  Reporting
    # ``retained: true`` made an empty store look like a searched one.
    sessions = _sessions(tmp_path)
    _failed_start(sessions)
    client = _client(sessions)
    for source in ("client", "monitor"):
        payload = client.get(
            f"/api/fleet/{_NODE}/loginfo", params={"source": source}
        ).json()
        assert payload["entries"] == []
        assert payload["retained"] is False


def test_failed_start_for_another_node_is_not_projected(tmp_path):
    sessions = _sessions(tmp_path)
    _failed_start(sessions, node_id="spk_" + "b" * 32)
    payload = _client(sessions).get(f"/api/fleet/{_NODE}/loginfo").json()
    assert payload["entries"] == []


def test_refused_request_bound_is_retrievable_through_the_operator_log(tmp_path):
    # Wrong implementation: the refusal named its rule but not the bound, so an
    # operator could not tell 518 of 4096 from 5000 of 4096.
    sessions = _sessions(tmp_path)
    _failed_start(
        sessions,
        preflight=[
            {
                "name": "request_refusal",
                "value": (
                    "rule=helper_request_argument_count_invalid limit=4096 observed=518"
                ),
            }
        ],
    )
    payload = _client(sessions).get(f"/api/fleet/{_NODE}/loginfo").json()
    messages = [entry["message"] for entry in payload["entries"]]
    assert any(
        "limit=4096" in message and "observed=518" in message for message in messages
    ), messages
