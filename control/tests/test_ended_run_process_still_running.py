"""A process still running for a run the Controller ended is never kept unowned.

The Spark's report that it still runs reopens the run as ``lost`` (stoppable),
so the next load on that Spark stops it; every other report about an ended run
changes nothing.
"""

from __future__ import annotations

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


def test_clock_skew_is_a_wait_not_a_verdict_on_the_report(
    agent_system,  # noqa: F811
) -> None:
    client, services, _, clock = agent_system
    run_id = _ended_run(services, clock, state="failed")

    response = _report(client, clock, run_id, running=True, observed_ahead=60)

    # The Spark's clock is ahead of the Controller's: the same bytes are
    # accepted once the skew clears, so the agent must keep the page.
    assert response.status_code == 503
    assert "retry-after" in response.headers


def test_run_leaving_the_stoppable_states_mid_report_is_a_wait(
    agent_system,  # noqa: F811
    monkeypatch,
) -> None:
    from vonk_control.agent_api import observations
    from vonk_control.recipe_operations.observation_helpers import RunNotAssigned

    client, services, _, clock = agent_system
    run_id = _ended_run(services, clock, state="lost")

    def raced(*_args, **_kwargs):
        raise RunNotAssigned("recipe run observation is not assigned")

    monkeypatch.setattr(
        observations, "prepare_exact_recipe_run_observation_nodes", raced
    )

    # Wrong implementation: 422 tells the Spark these bytes can never be
    # accepted, though a stop merely crossed the report.
    assert _report(client, clock, run_id, running=False).status_code == 503
