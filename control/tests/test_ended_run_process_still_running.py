"""A process still running for a run the Controller ended is never kept unowned.

The Spark's report that it still runs reopens the run as ``lost`` (stoppable),
so the next load on that Spark stops it; every other report about an ended run
changes nothing.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from vonk_control.agent_api import ended_runs
from vonk_control.models import RecipeRun, RunNode

from .test_agent_api import NODE_A, NODE_B, agent_headers, agent_system  # noqa: F401

# The run rows carry only the plan fields this boundary reads.
pytestmark = pytest.mark.usefixtures("damaged_json_rows")


@pytest.fixture
def stop_buildable(monkeypatch) -> None:
    """These runs have no installation rows; their exact Stop is assumed."""

    monkeypatch.setattr(
        ended_runs, "durable_run_stop_payloads", lambda *_args, **_kwargs: {}
    )


def _ended_run(services, clock, *, state: str, node_id: str = NODE_A) -> str:
    run_id = str(uuid.uuid4())
    before = clock.now - timedelta(seconds=10)
    with services.sessions.begin() as session:
        session.add(
            RecipeRun(
                id=run_id,
                installation_id=str(uuid.uuid4()),
                mapping_id=str(uuid.uuid4()),
                mapping_generation=1,
                run_generation=3,
                alias="ended",
                plan_digest="1" * 64,
                plan={"schema_version": 1, "observation_schema_version": 2},
                state=state,
                route_state="withdrawn",
                actor="admin",
                created_at=before,
                updated_at=before,
                stopped_at=before if state == "stopped" else None,
            )
        )
        session.add(
            RunNode(
                run_id=run_id,
                node_id=node_id,
                rank=0,
                role="worker",
                state="failed" if state == "failed" else "stopped",
                port=8888,
                reserved_memory_bytes=1,
                updated_at=before,
            )
        )
    return run_id


def _report(
    client,
    clock,
    run_id: str,
    *,
    running: object,
    generation: object = 3,
    observed_ahead: object = 0,
):
    return client.post(
        "/agent/recipe-runs/observations",
        headers=agent_headers(NODE_A, "serial-a"),
        json={
            "observed_at": (
                clock.now + timedelta(seconds=float(observed_ahead))  # type: ignore[arg-type]
            ).isoformat(),
            "runs": [
                {
                    "run_id": run_id,
                    "run_generation": generation,
                    "process_running": running,
                    "endpoint_ready": None,
                }
            ],
        },
    )


def _state(services, run_id: str) -> tuple[str, str]:
    with services.sessions() as session:
        run = session.get(RecipeRun, run_id)
        node = session.scalar(select(RunNode).where(RunNode.run_id == run_id))
        assert run is not None and node is not None
        return run.state, node.state


@pytest.mark.usefixtures("stop_buildable")
@pytest.mark.parametrize("ended", ["failed", "stopped"])
def test_running_process_of_ended_run_reopens_it_as_lost(
    agent_system,  # noqa: F811
    ended: str,
) -> None:
    client, services, _, clock = agent_system
    run_id = _ended_run(services, clock, state=ended)

    assert _report(client, clock, run_id, running=True).status_code == 204

    with services.sessions() as session:
        run = session.get(RecipeRun, run_id)
        node = session.scalar(select(RunNode).where(RunNode.run_id == run_id))
        assert run is not None and node is not None
        assert run.state == "lost"
        assert run.stopped_at is None
        assert run.route_error is not None
        assert node.state == "running"
        assert node.observed_run_generation == 3
        assert node.observation_process_running is True
    # The agent now keeps reporting the run instead of dropping it unowned.
    disposition = client.get(
        f"/agent/recipe-runs/{run_id}/disposition",
        headers=agent_headers(NODE_A, "serial-a"),
    )
    assert "x-vonk-recipe-run-disposition" not in disposition.headers
    assert disposition.headers["x-vonk-recipe-run-generation"] == "3"


@pytest.mark.usefixtures("stop_buildable")
def test_other_reports_about_an_ended_run_change_nothing(
    agent_system,  # noqa: F811
) -> None:
    client, services, _, clock = agent_system

    def unchanged(run_id: str, **report: object) -> bool:
        before = _state(services, run_id)
        assert _report(client, clock, run_id, **report).status_code == 204
        return _state(services, run_id) == before

    assert unchanged(_ended_run(services, clock, state="failed"), running=False)
    assert unchanged(
        _ended_run(services, clock, state="failed"), running=True, generation=2
    )
    assert unchanged(
        _ended_run(services, clock, state="stopped", node_id=NODE_B), running=True
    )

    # A report taken before the run ended crossed its end: it is not evidence.
    crossed = _ended_run(services, clock, state="failed")
    with services.sessions.begin() as session:
        run = session.get(RecipeRun, crossed)
        assert run is not None
        run.updated_at = clock.now
    assert unchanged(crossed, running=True)


def test_ended_run_without_an_exact_stop_is_not_reopened(
    agent_system,  # noqa: F811
) -> None:
    # Reopened, a run no plan can stop would block every later load there.
    client, services, _, clock = agent_system
    run_id = _ended_run(services, clock, state="failed")
    before = _state(services, run_id)
    assert _report(client, clock, run_id, running=True).status_code == 204
    assert _state(services, run_id) == before


def _body(clock, run_id: str, *, ahead: int = 0, running: bool = True) -> dict:
    return {
        "observed_at": (clock.now + timedelta(seconds=ahead)).isoformat(),
        "runs": [
            {
                "run_id": run_id,
                "run_generation": 3,
                "process_running": running,
                "endpoint_ready": None,
            }
        ],
    }


def _post(client, path: str, body: dict):
    return client.post(path, headers=agent_headers(NODE_A, "serial-a"), json=body)


def test_clock_skew_is_a_wait_and_the_same_bytes_land_once_it_clears(
    agent_system,  # noqa: F811
) -> None:
    client, services, _, clock = agent_system
    run_id = _ended_run(services, clock, state="lost")
    body = _body(clock, run_id, ahead=60)

    # Wrong implementation: 422 tells the Spark these bytes can never be
    # accepted, though they are fine once this clock catches up.
    waiting = _post(client, "/agent/recipe-runs/observations", body)
    assert waiting.status_code == 503
    assert json.loads(waiting.headers["retry-after"]) >= 1
    assert waiting.json()["reason"] == "clock_skew"

    clock.now += timedelta(seconds=31)
    assert _post(client, "/agent/recipe-runs/observations", body).status_code == 204


def test_inventory_from_a_clock_ahead_is_a_wait_and_lands_once_it_clears(
    agent_system,  # noqa: F811
) -> None:
    client, _, _, clock = agent_system
    body = {
        "schema_version": 1,
        "observed_at": (clock.now + timedelta(seconds=60)).isoformat(),
        "disk_total_bytes": 1000,
        "disk_free_bytes": 700,
        "host_memory_total_bytes": 2000,
        "host_memory_free_bytes": 1500,
        "gpu_memory_total_bytes": 1000,
        "gpu_memory_free_bytes": 800,
        "gpu_count": 1,
        "memory_pool": "separate",
        "artifact_store_read_only": False,
        "capabilities": [],
        "nvidia_driver_version": "580.65.06",
        "container_runtime_version": "28.3.3",
    }
    waiting = _post(client, "/agent/inventory", body)
    assert waiting.status_code == 503
    assert waiting.json()["reason"] == "clock_skew"

    clock.now += timedelta(seconds=31)
    assert _post(client, "/agent/inventory", body).status_code == 204


def test_run_leaving_the_stoppable_states_between_the_reads_is_a_wait(
    agent_system,  # noqa: F811
    monkeypatch,
) -> None:
    from vonk_control.agent_api import observations

    client, services, _, clock = agent_system
    run_id = _ended_run(services, clock, state="lost")
    real = observations.prepare_exact_recipe_run_observation_nodes
    raced = {"once": True}

    def stop_first(session, *args, **kwargs):
        # A stop commits between the unlocked read and the locked read.
        if raced.pop("once", False):
            run = session.get(RecipeRun, run_id)
            assert run is not None
            run.state = "stopped"
            session.flush()
        return real(session, *args, **kwargs)

    monkeypatch.setattr(
        observations, "prepare_exact_recipe_run_observation_nodes", stop_first
    )
    body = _body(clock, run_id, running=False)

    # Wrong implementation: 422 tells the Spark these bytes can never be
    # accepted, though a stop merely crossed the report.
    waiting = _post(client, "/agent/recipe-runs/observations", body)
    assert waiting.status_code == 503
    assert waiting.headers["x-vonk-error-code"]
    # The same bytes now find the run ended and are consumed.
    assert _post(client, "/agent/recipe-runs/observations", body).status_code == 204
