"""The upgrade-carry lane's verdict and its first-release behaviour."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.acceptance import spark_upgrade_carry as carry
from tests.acceptance.test_spark_lifecycle import LifecycleError


def _lane(probes: list[carry.ProbeResult]) -> carry.UpgradeCarryLifecycle:
    lane = object.__new__(carry.UpgradeCarryLifecycle)
    lane.evidence = carry.CarryEvidence(probes=probes)
    return lane


def _probe(phase: str, *, ok: bool = True, route: str = "published"):
    return carry.ProbeResult(
        at=0.0,
        phase=phase,
        models_listed=ok,
        inference_ok=ok,
        route_state=route,
        detail=None if ok else "gateway does not list the carried model",
    )


def test_a_withdrawn_route_after_the_agent_upgrade_fails_and_names_the_phase(
    capsys,
):
    # What release d30de9199 did: the upgraded agent stopped reporting the
    # run the previous agent started, and the Controller withdrew its route.
    lane = _lane(
        [
            _probe("baseline-serving"),
            _probe("agent-upgrade"),
            _probe("agent-settled"),
            _probe("agent-settled", ok=False, route="withdrawn"),
        ]
    )
    with pytest.raises(LifecycleError) as failed:
        lane._judge()
    message = str(failed.value)
    assert "phase agent-settled" in message
    assert "'withdrawn'" in message
    assert "agent-settled 1/2 failed" in message
    # The full probe evidence goes to the log and the report.
    assert "gateway does not list" in capsys.readouterr().err
    assert lane.failure_evidence is not None
    assert lane.failure_evidence["phase"] == "agent-settled"


def test_the_gateway_restart_during_the_controller_recreate_is_tolerated():
    lane = _lane(
        [
            _probe("baseline-serving"),
            _probe("controller-redeploy", ok=False, route="absent"),
            _probe("controller-redeploy", ok=False, route="absent"),
            _probe("controller-settled"),
            _probe("agent-settled"),
        ]
    )
    lane._judge()


def test_one_missed_probe_is_tolerated_but_two_in_a_row_are_not():
    _lane(
        [
            _probe("agent-settled"),
            _probe("agent-settled", ok=False),
            _probe("agent-settled"),
        ]
    )._judge()
    with pytest.raises(LifecycleError, match="phase agent-upgrade"):
        _lane(
            [
                _probe("agent-upgrade", ok=False),
                _probe("agent-upgrade", ok=False),
                _probe("agent-settled"),
            ]
        )._judge()


def test_the_first_release_is_skipped_visibly(tmp_path: Path, monkeypatch, capsys):
    output = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "spark_upgrade_carry",
            "--channel",
            "dev",
            "--candidate-generation",
            "a" * 64,
            "--run-id",
            "1",
            "--output",
            str(output),
        ],
    )
    assert carry.main() == 0
    assert (
        "::notice title=Upgrade-carry acceptance skipped::" in capsys.readouterr().out
    )
    report = json.loads(output.read_text())
    assert report["status"] == "skipped" and "no previous promoted" in report["reason"]
