"""``NasComposeBundleStale`` judges the running Controller, not the one before.

Compose restarts Prometheus with control-api (``depends_on: restart: true``),
and Prometheus writes no staleness marker when it stops. For five minutes after
a redeploy it still sees the previous process's series, whose labels name the
previous release. On 2026-10-17 that series (``expected`` = the previous
release, ``running="missing"``) raised the alert next to the new process's
current one. The production rules are evaluated here by the production
Prometheus image's own rule engine.
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
PREVIOUS = "7432bc56e90676447f5624bbe365185abf2babef53bdfab8e8b7918aba9b81d4"
RELEASE = "9779bfd2be1f6c393ca9dbd1ba517f4202bcf1b6c9f0f2ee5e032d5530d0c0df"
TARGET = 'job="vonk-control",instance="control-api:8000"'
SHIPPED = next(
    rule
    for group in json.loads(
        (ROOT / "deploy/compose/prometheus/alerts.yaml").read_text(encoding="utf-8")
    )["groups"]
    for rule in group["rules"]
    if rule["alert"] == "NasComposeBundleStale"
)


def _series(expected: str, running: str, values: str) -> dict[str, str]:
    return {
        "series": (
            f'vonk_nas_compose_bundle_current{{{TARGET},expected="{expected}",'
            f'running="{running}"}}'
        ),
        "values": values,
    }


def _alert(expected: str, running: str) -> dict:
    return {
        "exp_labels": {
            "severity": "warning",
            "job": "vonk-control",
            "instance": "control-api:8000",
            "expected": expected,
            "running": running,
        },
        "exp_annotations": SHIPPED["annotations"],
    }


# The control: the rule as #1708 shipped it.
BARE_RULE = {
    **SHIPPED,
    "alert": "BareComposeBundleStale",
    "expr": "vonk_nas_compose_bundle_current == 0",
}


def _test(series: list[dict], at: str, alertname: str, alerts: list[dict]) -> dict:
    return {
        "interval": "1m",
        "input_series": series,
        "alert_rule_test": [
            {"eval_time": at, "alertname": alertname, "exp_alerts": alerts}
        ],
    }


@pytest.mark.needs_docker
@pytest.mark.slow(30)
def test_the_alert_follows_the_running_controller_across_a_prometheus_restart(
    tmp_path: Path,
) -> None:
    shutil.copy(ROOT / "deploy/compose/prometheus/alerts.yaml", tmp_path)
    control = {"groups": [{"name": "control", "rules": [BARE_RULE]}]}
    (tmp_path / "control.yaml").write_text(json.dumps(control))
    # One sample a minute. A series that ends without ``stale`` belongs to a
    # process Prometheus last scraped before it restarted: still in lookback.
    # The installer reran: the previous process (old image, old Compose file)
    # ends at 4m, the new, current one starts at 5m.
    rerun = [
        _series(PREVIOUS, "missing", "0x4"),
        _series(RELEASE, RELEASE, "_x5 1x5"),
    ]
    # A pull without the installer: the previous process was current; the new
    # image disagrees with the old Compose file.
    pulled = [
        _series(PREVIOUS, PREVIOUS, "1x4"),
        _series(RELEASE, PREVIOUS, "_x5 0x10"),
    ]
    tests = [
        _test(rerun, "7m", "NasComposeBundleStale", []),
        # The failure mode reproduces with the bare rule.
        _test(rerun, "7m", BARE_RULE["alert"], [_alert(PREVIOUS, "missing")]),
        # Never silent: it fires once the previous series leaves the lookback.
        _test(pulled, "11m", "NasComposeBundleStale", [_alert(RELEASE, PREVIOUS)]),
        # Without a Prometheus restart the previous series is stale at once.
        _test(
            [
                _series(PREVIOUS, PREVIOUS, "1x4 stale"),
                _series(RELEASE, PREVIOUS, "_x5 0x10"),
            ],
            "7m",
            "NasComposeBundleStale",
            [_alert(RELEASE, PREVIOUS)],
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
