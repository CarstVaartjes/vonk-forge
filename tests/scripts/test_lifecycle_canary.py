from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
from vonk_agent_protocol import LifecycleState

ROOT = Path(__file__).resolve().parents[2]


def _module():
    loader = importlib.machinery.SourceFileLoader(
        "lifecycle_canary", str(ROOT / "scripts/lifecycle-canary")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


class FakeControl:
    """A scripted vonkctl: each load walks the given states, one per poll."""

    def __init__(self, walks, activity=None, outage_polls=0):
        self.walks = list(walks)
        self.activity = activity or {"items": []}
        self.outage_polls = outage_polls
        self.current: list[str] = []
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kwargs):
        self.calls.append(argv)
        tail = [a for a in argv if a not in {"vonkctl", "--profile", "1", "--json"}]
        if tail[:2] == ["profile", "load"]:
            self.current = list(self.walks.pop(0))
            return self._done({"application_id": "app-1", "state": "queued"})
        if tail[:2] == ["profile", "cancel"]:
            return self._done({"state": "cancelling"})
        if tail[:2] == ["profile", "progress"]:
            if self.outage_polls:
                self.outage_polls -= 1
                return subprocess.CompletedProcess(argv, 1, "", "unreachable")
            state = self.current.pop(0) if len(self.current) > 1 else self.current[0]
            return self._done({"application": {"id": "app-1", "state": state}})
        if tail[:2] == ["fleet", "activity"]:
            return self._done(self.activity)
        return self._done({"ok": True})

    @staticmethod
    def _done(document):
        return subprocess.CompletedProcess([], 0, json.dumps(document), "")


def _canary(control, *, minutes: float = 1):
    module = _module()
    ticks = iter(range(10_000))
    canary = module.Canary(
        module.Controller(["vonkctl"], 1, run=control, log=lambda _m: None),
        minutes=minutes,
        poll_seconds=1,
        restart_command=None,
        clock=lambda: float(next(ticks)),
        sleep=lambda _s: None,
        log=lambda _m: None,
    )
    return module, canary


def test_all_three_scenarios_pass_on_a_healthy_controller() -> None:
    control = FakeControl(
        [["running", "succeeded"], ["running", "cancelled"], ["running", "succeeded"]],
        outage_polls=0,
    )
    _, canary = _canary(control)
    assert canary.run_all() == 0
    assert canary.failures == []


def test_a_cancel_that_lost_the_race_still_passes() -> None:
    control = FakeControl([["succeeded"], ["succeeded"], ["succeeded"]])
    _, canary = _canary(control)
    assert canary.run_all() == 0


def test_a_controller_outage_during_the_wait_is_tolerated() -> None:
    control = FakeControl([["succeeded"]] * 3)
    control.outage_polls = 0
    _, canary = _canary(control)
    canary.controller.run = FakeControl([["succeeded"]], outage_polls=3)
    canary.start_load()
    assert canary.follow("app-1", label="t") == "succeeded"
    assert canary.tolerated_errors == 3


def test_a_stuck_admission_ends_and_a_fresh_load_completes() -> None:
    control = FakeControl(
        [[LifecycleState.RUNNING.value], [LifecycleState.SUCCEEDED.value]]
    )
    module, canary = _canary(control, minutes=0.05)
    canary.start_load()
    with pytest.raises(module.CanaryFailure):
        canary.follow("app-1", label="load")
    canary.start_load()
    assert canary.follow("app-1", label="fresh") == LifecycleState.SUCCEEDED.value


def test_an_operator_wait_without_an_action_fails_either_spelling() -> None:
    module = _module()
    for state in ("needs-operator", "waiting-for-operator"):
        document = {"items": [{"id": "j", "state": state, "supported_actions": []}]}
        assert module.operator_waits_without_action(document)
    advertised = {
        "items": [{"id": "j", "state": "needs-operator", "supported_actions": ["stop"]}]
    }
    assert module.operator_waits_without_action(advertised) == []


def test_the_final_activity_scan_fails_the_run() -> None:
    activity = {"items": [{"id": "j", "state": "needs-operator"}]}
    control = FakeControl(
        [["succeeded"], ["cancelled"], ["succeeded"]], activity=activity
    )
    _, canary = _canary(control)
    assert canary.run_all() == 1
    assert any("no operator action" in failure for failure in canary.failures)


def test_a_failed_load_fails_the_run() -> None:
    control = FakeControl([["failed"], ["cancelled"], ["succeeded"]])
    _, canary = _canary(control)
    assert canary.run_all() == 1


def test_it_refuses_to_run_without_a_connection(monkeypatch, capsys) -> None:
    module = _module()
    monkeypatch.delenv("VONK_CONTROL_URL", raising=False)
    assert module.main(["--profile", "1"]) == 2
    assert "VONK_CONTROL_URL" in capsys.readouterr().err
