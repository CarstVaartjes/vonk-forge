"""A serving workload that dies after a successful start keeps its cause.

Its start succeeded, so no failed operation holds the account: the agent's
observation of the exited process is the only carrier, and recovery clears the
current observation when it restarts the rank.  These tests drive the agent's
real observation report, the recovery's fresh live evidence and the operator's
``fleet loginfo`` read path.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from vonk_agent_protocol.failure_evidence import (
    FailureDiagnostics,
    FailureLogTail,
    FailureProperty,
)
from vonk_control.failure_evidence import MAX_LOG_BYTES
from vonk_control.models import RecipeRun, RunNode
from vonk_control.operator_projection_api import AgentFailureLogProvider

from .test_agent_api import NODE_A, NODE_B, agent_headers, make_agent_system

_RUN = "70000000-0000-4000-8000-000000000071"
_TRACEBACK = (
    "INFO:     Application startup complete.\n"
    "ERROR [core.py:612] EngineCore encountered a fatal error.\n"
    "Traceback (most recent call last):\n"
    '  File "/usr/lib/python3/dist-packages/vllm/v1/engine/core.py", line 603,'
    " in run_engine_core\n"
    "RuntimeError: CUDA error: an illegal memory access was encountered\n"
    "HF_TOKEN=hf_abcdefghijklmnopqrstuvwxyz0123456789\n"
)


def _tail(text: str) -> FailureLogTail:
    return FailureLogTail(text=text, truncated=False, dropped_bytes=0, dropped_lines=0)


def _crash(collected_at: str, stderr: str) -> FailureDiagnostics:
    """What the agent reports for an exited container it read before removal."""

    return FailureDiagnostics(
        collected_at=collected_at,
        phase="recipe.start",
        category="runtime",
        stdout=_tail(""),
        stderr=_tail(stderr),
        versions=[],
        sandbox=[],
        storage=[],
        collector_errors=[],
        preflight=[
            FailureProperty(name="exit_code", value="1"),
            FailureProperty(name="exit_cause", value="unclassified"),
        ],
    )


@pytest.fixture
def serving(tmp_path):
    client, services, _, clock = make_agent_system(tmp_path)
    with services.sessions.begin() as session:
        session.add(
            RecipeRun(
                id=_RUN,
                installation_id="80000000-0000-4000-8000-000000000008",
                mapping_id="90000000-0000-4000-8000-000000000009",
                mapping_generation=1,
                run_generation=2,
                alias="qwen-serving",
                plan_digest="1" * 64,
                plan={"schema_version": 1, "observation_schema_version": 2},
                state="running",
                route_state="withdrawn",
                actor="admin",
                created_at=clock.now,
                updated_at=clock.now,
            )
        )
        session.add(
            RunNode(
                run_id=_RUN,
                node_id=NODE_A,
                rank=0,
                role="worker",
                state="running",
                port=8888,
                reserved_memory_bytes=1,
                updated_at=clock.now - timedelta(seconds=1),
            )
        )

    def report(running: bool, diagnostics: FailureDiagnostics | None) -> None:
        response = client.post(
            "/agent/recipe-runs/observations",
            headers=agent_headers(NODE_A, "serial-a"),
            json={
                "observed_at": clock.now.isoformat(),
                "runs": [
                    {
                        "run_id": _RUN,
                        "run_generation": 2,
                        "process_running": running,
                        "endpoint_ready": running,
                        "failure_diagnostics": (
                            diagnostics.model_dump(mode="json", exclude_none=True)
                            if diagnostics
                            else None
                        ),
                    }
                ],
            },
        )
        assert response.status_code == 204, response.text

    def loginfo(node: str = NODE_A, **query):
        provider = AgentFailureLogProvider(services.sessions, clock=lambda: clock.now)
        return provider.list(
            node,
            since=query.get("since"),
            lines=1_000,
            recipe=None,
            source=query.get("source", "runtime"),
            follow=False,
        )

    return report, loginfo, services, clock


@pytest.mark.usefixtures("damaged_json_rows")
def test_crashed_serving_rank_stderr_outlives_its_recovery(serving) -> None:
    report, loginfo, services, clock = serving
    crashed_at = clock.now
    report(False, _crash(crashed_at.isoformat(), _TRACEBACK))
    # Recovery restarted the rank: its fresh live evidence clears the current
    # observation, which is where the crash's account used to die.
    clock.now += timedelta(seconds=30)
    report(True, None)
    with services.sessions() as session:
        node = session.scalar(select(RunNode).where(RunNode.run_id == _RUN))
        assert node is not None
        assert node.state == "running"
        assert node.observation_failure_diagnostics is None

    for source in ("runtime", None):
        messages = [entry.message for entry in loginfo(source=source).entries]
        assert any("workload process exited" in line for line in messages)
        assert (
            "stderr: RuntimeError: CUDA error: an illegal memory access was encountered"
            in messages
        )
        assert "stderr: ERROR [core.py:612] EngineCore encountered a fatal error." in (
            messages
        )
        assert "preflight: exit_code=1" in messages
        assert not any("hf_abcdefghij" in line for line in messages)
    assert {entry.evidence_id for entry in loginfo().entries} == {_RUN}
    # Another Spark's log, a job-only query and a window after the crash say
    # nothing about this rank.
    assert loginfo(NODE_B).entries == []
    assert loginfo(source="job").entries == []
    assert loginfo(since=crashed_at + timedelta(seconds=1)).entries == []


@pytest.mark.usefixtures("damaged_json_rows")
def test_next_crash_replaces_the_kept_account_and_stays_bounded(serving) -> None:
    report, loginfo, _, clock = serving
    report(False, _crash(clock.now.isoformat(), _TRACEBACK))
    clock.now += timedelta(seconds=30)
    report(True, None)
    # A full-size tail: the most the agent may send for one stream.
    full = "".join(f"rank 0 step {index:04d}\n" for index in range(200))[
        -MAX_LOG_BYTES:
    ]
    clock.now += timedelta(seconds=30)
    later = clock.now
    report(False, _crash(later.isoformat(), full))

    entries = loginfo().entries
    stderr = [
        entry.message for entry in entries if entry.message.startswith("stderr: ")
    ]
    assert not any("CUDA error" in line for line in stderr)
    assert stderr[-1] == "stderr: rank 0 step 0199"
    assert sum(len(line) - len("stderr: ") + 1 for line in stderr) <= MAX_LOG_BYTES
    assert {entry.observed_at for entry in entries} == {later}
