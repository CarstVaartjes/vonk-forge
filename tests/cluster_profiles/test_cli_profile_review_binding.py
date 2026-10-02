"""A confirmed profile load is bound to the effects the operator reviewed."""

from __future__ import annotations

import io
import sys

from cluster_profiles import cli
from cluster_profiles.control_client import ControlConflict

KEY = "11111111-1111-4111-8111-111111111111"
APPLICATION = "33333333-3333-4333-8333-333333333333"
FIRST = "a" * 64
CHANGED = "b" * 64
CHANGED_AGAIN = "d" * 64


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


class _Answers:
    def __init__(self, *answers: str) -> None:
        self._answers = list(answers)

    def isatty(self) -> bool:
        return True

    def readline(self, _limit: int = -1) -> str:
        return self._answers.pop(0) + "\n" if self._answers else ""


class _Controller:
    """Reviews its current plan; refuses a load bound to a different one."""

    request_timeout_seconds = 1.0

    def __init__(self, plan: str | None, *, drift: tuple[str, ...] = ()) -> None:
        self.plan = plan
        # Each entry is a change that lands just before one load arrives.
        self.drift = list(drift)
        self.calls: list[tuple[str, str, object]] = []

    def request(self, method, path, payload=None, **_kwargs):
        self.calls.append((method, path, payload))
        if path.endswith("/preview"):
            return {
                "allowed": True,
                "plan_digest": "c" * 64,
                **({"effects_digest": self.plan} if self.plan else {}),
                "profile_name": "Reviewed",
                "scope": {"node_ids": [], "idle_node_ids": []},
                "summary": {},
                "assignments": [],
                "steps": [],
                "preparations": [],
                "preparation_decisions": [],
                "assessments": [],
                "admission_decisions": [],
                "effects": {"runs": [], "installations": [], "superseded": []},
                "reasons": [],
            }
        assert path.endswith("/load")
        assert isinstance(payload, dict)
        if self.drift:
            self.plan = self.drift.pop(0)
        reviewed = payload.get("reviewed_effects_digest")
        if reviewed is not None and reviewed != self.plan:
            raise ControlConflict(
                409, "The profile plan changed", code="profile.review_stale"
            )
        return {
            "id": APPLICATION,
            "request_key": payload["request_key"],
            "state": "succeeded",
            "progress": {},
        }

    def loads(self) -> list[object]:
        return [payload for _m, path, payload in self.calls if path.endswith("/load")]

    def previews(self) -> int:
        return sum(path.endswith("/preview") for _m, path, _p in self.calls)


def _load(monkeypatch, controller, *answers):
    monkeypatch.setattr(sys, "stdin", _Answers(*answers))
    terminal = _Terminal()
    monkeypatch.setattr(sys, "stderr", terminal)
    status = cli.main(
        ("--profile", "2", "profile", "load", "--detach"),
        control_client=controller,
        request_id_factory=lambda: KEY,
    )
    return status, terminal.getvalue()


def test_confirmed_load_names_the_effects_that_were_shown(monkeypatch, capsys):
    controller = _Controller(FIRST)

    assert _load(monkeypatch, controller, "yes")[0] == 0

    assert controller.loads() == [
        {"request_key": KEY, "reviewed_effects_digest": FIRST}
    ]
    capsys.readouterr()


def test_stale_review_shows_the_new_plan_and_asks_again(monkeypatch, capsys):
    controller = _Controller(FIRST, drift=(CHANGED,))

    assert _load(monkeypatch, controller, "yes", "yes")[0] == 0

    # The first load is refused, the changed plan is reviewed again, and only
    # the second answer applies it. Nothing was accepted by the refusal, so the
    # same request identity is reused.
    assert controller.loads() == [
        {"request_key": KEY, "reviewed_effects_digest": FIRST},
        {"request_key": KEY, "reviewed_effects_digest": CHANGED},
    ]
    assert controller.previews() == 2
    capsys.readouterr()


def test_declining_the_changed_plan_loads_nothing(monkeypatch, capsys):
    controller = _Controller(FIRST, drift=(CHANGED,))

    assert _load(monkeypatch, controller, "yes", "no")[0] == 2

    assert controller.loads() == [
        {"request_key": KEY, "reviewed_effects_digest": FIRST}
    ]
    capsys.readouterr()


def test_a_plan_that_keeps_changing_is_refused_not_forced(monkeypatch, capsys):
    controller = _Controller(FIRST, drift=(CHANGED, CHANGED_AGAIN, "e" * 64, "f" * 64))

    status, transcript = _load(monkeypatch, controller, "yes", "yes", "yes", "yes")

    assert status == 2
    assert len(controller.loads()) == 3
    assert "plan changed" in transcript
    capsys.readouterr()


def test_yes_takes_the_current_plan_without_a_review(monkeypatch, capsys):
    controller = _Controller(FIRST)
    monkeypatch.setattr(sys, "stdin", _Answers())
    monkeypatch.setattr(sys, "stderr", _Terminal())

    status = cli.main(
        ("--profile", "2", "profile", "load", "--yes", "--detach", "--json"),
        control_client=controller,
        request_id_factory=lambda: KEY,
    )

    assert status == 0
    assert controller.calls == [("POST", "/api/profile/2/load", {"request_key": KEY})]
    capsys.readouterr()


def test_a_controller_without_effects_digests_loads_the_current_plan(
    monkeypatch, capsys
):
    controller = _Controller(None)

    assert _load(monkeypatch, controller, "yes")[0] == 0

    assert controller.loads() == [{"request_key": KEY}]
    capsys.readouterr()
