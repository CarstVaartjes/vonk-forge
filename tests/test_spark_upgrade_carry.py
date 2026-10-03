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


def test_a_load_that_replaces_the_workload_names_the_run_that_now_serves():
    """The load also stops the old run, so its receipts name two runs.

    The first hardware run failed with "run_id evidence is invalid" because the
    helper required exactly one run in the whole application.
    """

    old_run = "11111111-1111-4111-8111-111111111111"
    new_run = "22222222-2222-4222-8222-222222222222"
    installation = "33333333-3333-4333-8333-333333333333"
    results: list[object] = [
        {"phase": "stop", "run_id": old_run},
        {
            "phase": "prepare",
            "subphase": "runtime-install",
            "installation_id": installation,
        },
        {"phase": "final_verify", "run_id": new_run},
    ]
    lane = object.__new__(carry.UpgradeCarryLifecycle)
    assert lane._serving_identity(results) == (installation, new_run)
    with pytest.raises(LifecycleError):
        lane._serving_identity([results[0], results[1]])


def _scenario_lane() -> carry.UpgradeCarryLifecycle:
    import threading

    lane = _lane([])
    lane._lock = threading.Lock()
    lane._phase = "baseline-install"
    return lane


def _failing() -> dict[str, object]:
    raise LifecycleError("the second install was refused")


def test_an_observed_phase_that_fails_is_reported_and_does_not_fail_the_lane(
    monkeypatch, capsys
):
    monkeypatch.setattr(carry, "OBSERVED_PHASES", frozenset({"new-phase"}))
    lane = _scenario_lane()
    lane._run_scenario("new-phase", _failing)
    assert lane.evidence.observed == [
        {
            "phase": "new-phase",
            "status": "failed",
            "error": "LifecycleError: the second install was refused",
        }
    ]
    assert "::warning title=Observed upgrade-carry phase new-phase failed" in (
        capsys.readouterr().out
    )


def test_an_observed_phase_that_crashes_on_a_surprise_is_still_only_observed(
    monkeypatch,
):
    monkeypatch.setattr(carry, "OBSERVED_PHASES", frozenset({"new-phase"}))
    lane = _scenario_lane()
    lane._run_scenario("new-phase", lambda: {}["missing"])
    assert lane.evidence.observed[0]["status"] == "failed"


def test_a_phase_taken_out_of_the_observed_set_gates_the_lane(monkeypatch):
    # Promoting a phase to gating is deleting its name from OBSERVED_PHASES.
    monkeypatch.setattr(carry, "OBSERVED_PHASES", frozenset())
    lane = _scenario_lane()
    with pytest.raises(LifecycleError, match="phase new-phase"):
        lane._run_scenario("new-phase", _failing)
    assert lane.evidence.observed == []


def test_an_observed_phase_that_passes_leaves_its_evidence_in_the_report(
    monkeypatch,
):
    monkeypatch.setattr(carry, "OBSERVED_PHASES", frozenset({"new-phase"}))
    lane = _scenario_lane()
    lane._run_scenario("new-phase", lambda: {"image_digest": "sha256:" + "a" * 64})
    (record,) = lane.evidence.observed
    assert record["status"] == "passed"
    assert str(record["image_digest"]).startswith("sha256:")
