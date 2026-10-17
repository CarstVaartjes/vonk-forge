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

from deploy.compose.tests.test_runtime_asset_reload import _services

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
    (tmp_path / "test.yaml").write_text(
        yaml.safe_dump(
            {
                "rule_files": ["alerts.yaml", "control.yaml"],
                "evaluation_interval": "1m",
                "tests": tests,
            }
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
