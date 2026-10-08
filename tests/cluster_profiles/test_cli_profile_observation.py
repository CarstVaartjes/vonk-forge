"""Continuation observes owner identity within one budget, never cancelling work."""

from __future__ import annotations

from typing import cast

import pytest

from cluster_profiles import cli
from cluster_profiles.control_client import (
    ControlMalformedResponse,
    ControlNotFound,
    ControlUnavailable,
)
from cluster_profiles.controller_cli.common import ControllerClient
from cluster_profiles.controller_cli.profile import _profile


class _Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, delay):
        self.now += delay


class _Successors:
    request_timeout_seconds = 1.0

    def __init__(
        self,
        clock: _Clock,
        *,
        different_request: bool = False,
        unavailable: ControlUnavailable | ControlNotFound | None = None,
        forged: bool = False,
    ):
        self.clock = clock
        self.different_request = different_request
        self.unavailable = unavailable
        self.forged = forged
        self.calls = []
        self.timeouts = []
        self.initial_delay = 0.0

    def request(self, method, path, payload=None, *, timeout_seconds=None, **kwargs):
        assert method == "GET"  # Observation cannot cancel or submit an effect.
        self.calls.append(path)
        if path.endswith("/progress"):
            assert timeout_seconds is not None
            self.clock.now += min(self.initial_delay, timeout_seconds)
            identity = "0"
        else:
            assert timeout_seconds is not None
            self.timeouts.append(timeout_seconds)
            self.clock.now += min(0.3, timeout_seconds)
            if self.unavailable is not None:
                raise self.unavailable
            identity = path.rsplit("/", 1)[-1]
        return {
            "id": "forged" if self.forged and identity != "0" else identity,
            "request_key": "later-request"
            if self.different_request and identity != "0"
            else "original-request",
            "retry_of_application_id": None
            if self.different_request
            else str(int(identity) - 1),
            "state": "superseded",
            "superseded_by": str(int(identity) + 1),
        }


def _observe(monkeypatch, controller):
    monkeypatch.setattr("time.monotonic", controller.clock.monotonic)
    monkeypatch.setattr("time.sleep", controller.clock.sleep)
    args = cli._parser().parse_args(
        [
            "profile",
            "progress",
            "--follow",
            "--timeout-seconds",
            "1",
            "--interval-seconds",
            "0.01",
        ]
    )
    result = _profile(args, cast(ControllerClient, controller), lambda: "fresh")
    return result, args.observation


@pytest.mark.parametrize("initial_delay", [0.0, 0.8])
def test_an_unending_successor_chain_ends_and_a_fresh_observation_is_admitted(
    monkeypatch,
    initial_delay,
):
    controller = _Successors(_Clock())
    controller.initial_delay = initial_delay
    result, observation = _observe(monkeypatch, controller)
    assert controller.clock.now <= 1.0
    assert len(controller.calls) <= 5
    assert observation.status == "timed_out"
    assert result["request_key"] == "original-request"
    assert observation.reconnect_command
    assert controller.timeouts[-1] <= controller.timeouts[0]
    assert controller.timeouts[0] <= 1.0 - initial_delay
    # The end left no gate; the next invocation gets its own bounded observation.
    controller.different_request = True
    result, observation = _observe(monkeypatch, controller)
    assert result["id"] == "0"
    assert observation.status == "complete"


@pytest.mark.parametrize(
    "outage", [ControlUnavailable(503, "unavailable"), ControlNotFound(404, "missing")]
)
def test_successor_outage_retries_within_budget_and_recovers_on_fresh_observation(
    monkeypatch,
    outage,
):
    controller = _Successors(_Clock(), unavailable=outage)
    result, observation = _observe(monkeypatch, controller)
    assert controller.clock.now <= 1.0
    assert len(controller.calls) > 2
    assert result["id"] == "0"
    assert observation.status == "timed_out"
    controller.unavailable = None
    controller.different_request = True
    result, observation = _observe(monkeypatch, controller)
    assert result["id"] == "0"
    assert observation.status == "complete"


def test_forged_successor_identity_is_never_adopted(monkeypatch):
    controller = _Successors(_Clock(), forged=True)
    with pytest.raises(ControlMalformedResponse):
        _observe(monkeypatch, controller)
    controller.forged = False
    controller.different_request = True
    result, _ = _observe(monkeypatch, controller)
    assert result["id"] == "0"


@pytest.mark.parametrize("code", ["cache.identity_mismatch", "cache.revoked_record"])
def test_bookkeeping_blockers_do_not_veto_new_removal(code):
    from tests.cluster_profiles.test_controller_cli import (
        FakeClient,
        _recipe_removal_receipt,
        _removal_review,
        run,
    )

    selector = "vision"
    key = "11111111-1111-4111-8111-111111111111"
    review = _removal_review("recipe", selector, with_model=False)
    review["blockers"] = [
        {"code": code, "detail": "Stored observation unavailable", "retryable": True}
    ]
    receipt = _recipe_removal_receipt(selector, key, with_model=False)
    client = FakeClient(
        {
            ("GET", "/api/recipe/vision/remove-review"): review,
            ("POST", "/api/recipe/vision/remove"): receipt,
        }
    )
    status, result = run(
        ("recipe", "remove", selector, "--keep-model", "--yes", "--detach", "--json"),
        client,
    )
    assert status == 0
    assert result["operation_id"] == receipt["operation_id"]
    assert [call[0] for call in client.calls] == ["GET", "POST"]


def test_owner_authorization_denial_does_not_replay_and_a_new_request_is_admitted():
    from cluster_profiles.control_client import ControlForbidden
    from tests.cluster_profiles.test_controller_cli import (
        FakeClient,
        _recipe_removal_receipt,
        _removal_review,
        run,
    )

    selector = "vision"
    key = "11111111-1111-4111-8111-111111111111"
    client = FakeClient(
        {
            ("GET", "/api/recipe/vision/remove-review"): _removal_review(
                "recipe", selector, with_model=False
            ),
            ("POST", "/api/recipe/vision/remove"): ControlForbidden(403, "denied"),
        }
    )
    argv = ("recipe", "remove", selector, "--keep-model", "--yes", "--detach", "--json")
    status, _ = run(argv, client)
    assert status == 2
    assert [call[0] for call in client.calls] == ["GET", "POST"]
    receipt = _recipe_removal_receipt(selector, key, with_model=False)
    client.responses[("POST", "/api/recipe/vision/remove")] = receipt
    status, result = run(argv, client)
    assert status == 0
    assert result["operation_id"] == receipt["operation_id"]
