"""The failure alerts count work that failed recently, not stored history.

On 2026-10-17 ``RepeatedControlJobFailure`` and ``RepeatedAgentOperationFailures``
had been true for 17 days: they summed every stored failed row
(``vonk_jobs{state="failed"}``, ``vonk_agent_operations{state="failed"}``), so
the sweep's recipe outcomes kept them on forever. They now read the failures
that ended within the last hour. The production rules are evaluated here by the
production Prometheus image's own rule engine.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from deploy.compose.tests.test_runtime_asset_startup import _services

ROOT = Path(__file__).resolve().parents[3]
TARGET = 'job="vonk-control",instance="control-api:8000"'
SHIPPED = {
    rule["alert"]: rule
    for group in json.loads(
        (ROOT / "deploy/compose/prometheus/alerts.yaml").read_text(encoding="utf-8")
    )["groups"]
    for rule in group["rules"]
}
JOBS = "RepeatedControlJobFailure"
OPERATIONS = "RepeatedAgentOperationFailures"
# The control: the job rule as it shipped before, over stored history.
HISTORY_RULE = {
    **SHIPPED[JOBS],
    "alert": "HistoryJobFailure",
    "expr": 'sum(vonk_jobs{state="failed"}) >= 3',
}
# A control for the restart case: a rule that pends for less than the lookback.
SHORT_RULE = {**SHIPPED[JOBS], "alert": "ShortJobFailure", "for": "1m"}


def _series(metric: str, labels: str, values: str) -> dict[str, str]:
    return {"series": f"{metric}{{{TARGET},{labels}}}", "values": values}


def _alert(name: str) -> dict:
    return {
        "exp_labels": dict(SHIPPED[name]["labels"]),
        "exp_annotations": SHIPPED[name]["annotations"],
    }


def _test(series: list[dict], checks: list[tuple[str, str, list[dict]]]) -> dict:
    return {
        "interval": "1m",
        "input_series": series,
        "alert_rule_test": [
            {"eval_time": at, "alertname": name, "exp_alerts": alerts}
            for at, name, alerts in checks
        ],
    }


def _promtool(tmp_path: Path, tests: list[dict]) -> None:
    """Evaluate the shipped rules (plus any control rules) with promtool."""
    rules = [
        "alerts.yaml",
        *(["control.yaml"] if (tmp_path / "control.yaml").exists() else []),
    ]
    (tmp_path / "test.yaml").write_text(
        yaml.safe_dump(
            {"rule_files": rules, "evaluation_interval": "1m", "tests": tests}
        )
    )
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-v",
            f"{tmp_path}:/rules:ro",
            "-w",
            "/rules",
            "--entrypoint",
            "promtool",
            _services()["prometheus"]["image"],
            "test",
            "rules",
            "test.yaml",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.needs_docker
@pytest.mark.slow(30)
def test_failure_alerts_follow_recent_failures_not_history(tmp_path: Path) -> None:
    shutil.copy(ROOT / "deploy/compose/prometheus/alerts.yaml", tmp_path)
    control = {"groups": [{"name": "control", "rules": [HISTORY_RULE, SHORT_RULE]}]}
    (tmp_path / "control.yaml").write_text(json.dumps(control))
    jobs = "vonk_recent_job_failures"
    operations = "vonk_recent_agent_operation_failures"
    # Production on 2026-10-17: 162 failed jobs stored since 2026-09-30, one
    # recipe outcome in the last hour.
    sweep = [
        _series("vonk_jobs", 'kind="other",state="failed"', "162x20"),
        _series(jobs, 'kind="recipe.run-switch.v2"', "1x20"),
        _series(operations, 'operation="recipe.start"', "1x20"),
    ]
    # Every load fails: three within the hour, then the window passes.
    burst = [
        _series(jobs, 'kind="recipe.run-switch.v2"', "1 2 3x10 1x8"),
        _series(operations, 'operation="recipe.start"', "1 1 2 3x10 0x7"),
    ]
    # Prometheus restarted at 4m and writes no staleness marker: the previous
    # process's last sample (a kind this release no longer emits) stays in the
    # five-minute lookback beside the new process's own series.
    restart = [
        _series(jobs, 'kind="retired-kind"', "_x4 3"),
        _series(jobs, 'kind="recipe.run-switch.v2"', "_x5 0x10"),
    ]
    tests = [
        _test(
            sweep,
            [
                ("15m", JOBS, []),
                ("15m", OPERATIONS, []),
                # The failure mode reproduces with the rule over history.
                ("15m", HISTORY_RULE["alert"], [_alert(JOBS)]),
            ],
        ),
        _test(
            burst,
            [
                ("6m", JOBS, []),
                ("8m", JOBS, [_alert(JOBS)]),
                ("9m", OPERATIONS, [_alert(OPERATIONS)]),
                # Never forever: it clears once the window has passed.
                ("14m", JOBS, []),
                ("15m", OPERATIONS, []),
            ],
        ),
        _test(
            restart,
            [
                ("5m", SHORT_RULE["alert"], [_alert(JOBS)]),
                ("9m", JOBS, []),
                ("10m", JOBS, []),
            ],
        ),
    ]
    _promtool(tmp_path, tests)


@pytest.mark.needs_docker
@pytest.mark.slow(30)
def test_history_retention_alert_requires_sustained_failure_to_drain(
    tmp_path: Path,
) -> None:
    """A backlog's existence or slow drain must not become a stall alarm."""
    name = "HistoryRetentionStalled"
    metric = "vonk_history_rows_past_grace"
    shutil.copy(ROOT / "deploy/compose/prometheus/alerts.yaml", tmp_path)

    def backlog(values: str) -> list[dict[str, str]]:
        return [{"series": f"{metric}{{{TARGET}}}", "values": values}]

    tests = [
        _test(
            backlog("100+1x800 0x400"),
            [
                ("6h", name, []),
                ("11h59m", name, []),
                ("12h", name, [_alert(name)]),
                ("14h", name, []),
            ],
        ),
        # Even a slow budget-limited drain advances within each six-hour
        # comparison. An age-only threshold would fire throughout this drain.
        _test(
            backlog("100x299 90x299 80x299 70x299"),
            [("12h", name, []), ("19h", name, [])],
        ),
        # A constant floor of legitimately held rows never fires.
        _test(
            backlog("100x1200"),
            [("12h", name, []), ("19h", name, [])],
        ),
        _test(
            backlog("10+1x800"),
            [("12h", name, [_alert(name)])],
        ),
        # A previous process's sample survives for the Prometheus lookback.
        # A healthy replacement's zero cannot leave the alert firing.
        _test(
            [
                {
                    "series": f'{metric}{{job="vonk-control",instance="old"}}',
                    "values": "100+1x719",
                },
                {
                    "series": f'{metric}{{job="vonk-control",instance="new"}}',
                    "values": "_x720 0x100",
                },
            ],
            [("12h", name, [_alert(name)]), ("12h5m", name, [])],
        ),
    ]
    _promtool(tmp_path, tests)


INGRESS = "AgentIngressDown"


def _node(metric: str, node: str, state: str, values: str) -> dict[str, str]:
    return _series(metric, f'node_id="{node}",state="{state}"', values)


@pytest.mark.needs_docker
@pytest.mark.slow(30)
def test_agent_ingress_alert_fires_only_when_every_active_node_is_offline(
    tmp_path: Path,
) -> None:
    """Catches the 2026-11-19 outage, which raised only per-node warnings."""
    shutil.copy(ROOT / "deploy/compose/prometheus/alerts.yaml", tmp_path)
    connection, agent = "vonk_node_connection_state", "vonk_agent_state"
    both_offline = [
        _node(connection, "a", "offline", "1x10"),
        _node(connection, "b", "offline", "1x10"),
        _node(agent, "a", "active", "1x10"),
        _node(agent, "b", "active", "1x10"),
    ]
    one_online = [
        _node(connection, "a", "offline", "1x10"),
        _node(connection, "b", "offline", "0x10"),
        _node(agent, "a", "active", "1x10"),
        _node(agent, "b", "active", "1x10"),
    ]
    nobody_active = [  # Offline, but the agent is retired.
        _node(connection, "a", "offline", "1x10"),
        _node(agent, "a", "retired", "1x10"),
    ]
    tests = [
        _test(both_offline, [("4m", INGRESS, []), ("6m", INGRESS, [_alert(INGRESS)])]),
        _test(one_online, [("8m", INGRESS, [])]),
        _test(nobody_active, [("8m", INGRESS, [])]),
    ]
    _promtool(tmp_path, tests)
