"""Continuation observes owner identity within one budget, never cancelling work."""

from __future__ import annotations

from typing import cast

import pytest

from cluster_profiles import cli
from cluster_profiles.control_client import (
    ControlConflict,
    ControlHTTPError,
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
        mismatched: bool = False,
    ):
        self.clock = clock
        self.different_request = different_request
        self.unavailable = unavailable
        self.mismatched = mismatched
        self.calls = []
        self.timeouts = []
        self.initial_delay = 0.0
        self.finish = False
        self.successor_reads = 0

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
        if self.finish and identity != "0":
            self.successor_reads += 1
            from cluster_profiles.cli_states_generated import RUNNING, SUCCEEDED

            return {
                "id": identity,
                "request_key": "original-request",
                "retry_of_application_id": "0",
                "state": RUNNING if self.successor_reads == 1 else SUCCEEDED,
            }
        return {
            "id": "mismatched" if self.mismatched and identity != "0" else identity,
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
    if isinstance(outage, ControlNotFound):
        with pytest.raises(ControlNotFound):
            _observe(monkeypatch, controller)
    else:
        result, observation = _observe(monkeypatch, controller)
        assert result["id"] == "0"
        assert observation.status == "timed_out"
    assert controller.clock.now <= 1.0
    controller.unavailable = None
    controller.finish = True
    result, observation = _observe(monkeypatch, controller)
    assert result["id"] == "1"
    assert result["request_key"] == "original-request"
    assert result["state"] == "succeeded"
    assert result["supersedes_chain"] == ["0"]
    assert observation.status == "complete"


@pytest.mark.parametrize("repair", [True, False])
def test_wrong_successor_is_discarded_and_original_continuation_recovers(
    monkeypatch, repair
):
    controller = _Successors(_Clock(), mismatched=True)
    request = controller.request
    observations = 0

    def observe(*args, **kwargs):
        nonlocal observations
        candidate = request(*args, **kwargs)
        if candidate["id"] == "mismatched":
            observations += 1
            if repair:
                controller.mismatched = False
                controller.finish = True
        return candidate

    monkeypatch.setattr(controller, "request", observe)
    result, observation = _observe(monkeypatch, controller)
    assert result["id"] != "mismatched"
    assert observation.status == ("complete" if repair else "timed_out")
    assert controller.clock.now <= 1
    controller.mismatched = False
    result, _ = _observe(monkeypatch, controller)
    assert result["id"] != "mismatched"
    assert result["request_key"] == "original-request"


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


def test_closed_output_ends_only_observation_and_fresh_observer_reconnects(monkeypatch):
    from cluster_profiles.controller_cli.observation import _poll_path

    controller = _Successors(_Clock())
    monkeypatch.setattr("time.monotonic", controller.clock.monotonic)
    monkeypatch.setattr("time.sleep", controller.clock.sleep)
    args = cli._parser().parse_args(
        ("profile", "progress", "--follow", "--timeout-seconds", "1")
    )

    def closed(_snapshot):
        raise BrokenPipeError()

    args._watch_callback = closed
    initial = {"id": "0", "request_key": "original-request", "state": "running"}
    result = _poll_path(
        cast(ControllerClient, controller),
        "/api/profile/applications/0",
        cast(dict[str, object], initial),
        args,
    )
    assert result == initial
    assert args.observation.status == "interrupted"
    assert args.observation.reconnect_command
    assert controller.calls == []
    args._watch_callback = None
    result = _poll_path(
        cast(ControllerClient, controller),
        "/api/profile/applications/0",
        cast(dict[str, object], initial),
        args,
        terminal=lambda _: True,
        fetch_initial=True,
    )
    assert result["id"] == "0" and result["request_key"] == "original-request"
    assert args.observation.status == "complete"
    assert controller.calls == ["/api/profile/applications/0"]


def test_peer_broken_submission_response_reconciles_the_original_without_replay():
    from cluster_profiles.cli_states_generated import SUCCEEDED
    from tests.cluster_profiles.test_controller_cli import FakeClient, run

    key = "11111111-1111-4111-8111-111111111111"
    application = "22222222-2222-4222-8222-222222222222"
    receipt = {"id": application, "request_key": key, "state": SUCCEEDED}
    lookup = f"/api/profile/1/requests/{key}"
    client = FakeClient(
        {
            ("POST", "/api/profile/1/load"): BrokenPipeError(),
            ("GET", lookup): receipt,
        }
    )
    status, ended = run(
        (
            "--profile",
            "1",
            "profile",
            "load",
            "--yes",
            "--detach",
            "--json",
            "--request-key",
            key,
        ),
        client,
    )
    assert status == 0 and ended["id"] == application
    assert [call[0] for call in client.calls] == ["POST", "GET"]
    status, fresh = run(
        ("--profile", "1", "profile", "progress", "--request-key", key, "--json"),
        client,
    )
    assert status == 0 and fresh["id"] == application
    assert [call[:2] for call in client.calls] == [
        ("POST", "/api/profile/1/load"),
        ("GET", lookup),
        ("GET", lookup),
    ]


@pytest.mark.parametrize("repair", [True, False])
@pytest.mark.parametrize("fault", ["identity", "request", "missing", "unavailable"])
def test_initial_request_lookup_recovers_or_ends_then_fresh_observer_is_admitted(
    monkeypatch, repair, fault
):
    from vonk_agent_protocol.reason_codes import ProjectionCode

    from cluster_profiles.cli_states_generated import SUCCEEDED
    from cluster_profiles.control_client import ControlObservationUnavailable
    from tests.cluster_profiles.test_controller_cli import FakeClient, run

    key = "11111111-1111-4111-8111-111111111111"
    application = "33333333-3333-4333-8333-333333333333"
    path = f"/api/profile/1/requests/{key}"
    ready = {"id": application, "request_key": key, "state": SUCCEEDED}
    unknown = {
        "identity": {},
        "request": ready | {"request_key": "22222222-2222-4222-8222-222222222222"},
        "missing": ControlNotFound(404, "Projection absent"),
        "unavailable": ControlObservationUnavailable(
            ProjectionCode.OBSERVATION_TRANSFER_UNAVAILABLE.value,
            "Projection unavailable",
        ),
    }[fault]
    controller = FakeClient({("GET", path): [unknown, ready] if repair else unknown})
    clock = _Clock()
    monkeypatch.setattr("time.monotonic", clock.monotonic)
    monkeypatch.setattr("time.sleep", clock.sleep)
    argv = (
        "--profile",
        "1",
        "profile",
        "progress",
        "--request-key",
        key,
        "--follow",
        "--timeout-seconds",
        "0.1",
        "--interval-seconds",
        "0.01",
        "--json",
    )
    status, result = run(argv, controller)
    if repair and fault != "missing":
        assert status == 0 and result == ready
    else:
        assert status == 2 and result["result"] == {}
        observation = result["observation"]
        assert isinstance(observation, dict)
        assert observation["status"] == ("ended" if fault == "missing" else "timed_out")
        assert observation["path"] == path
        assert key in str(observation["reconnect_command"])
    assert clock.now <= 0.1
    controller.responses[("GET", path)] = ready
    status, result = run(argv, controller)
    assert status == 0 and result == ready
    assert all(call[0] == "GET" for call in controller.calls)


@pytest.mark.parametrize(
    "fault",
    [
        BrokenPipeError(),
        ControlUnavailable(503, "unknown"),
        ControlConflict(409, "projection unavailable"),
        ControlHTTPError(429, "observation rate limited"),
    ],
)
def test_peer_loss_retries_without_publishing_a_candidate_and_fresh_observer_works(
    monkeypatch, fault
):
    from cluster_profiles.cli_states_generated import SUCCEEDED
    from tests.cluster_profiles.test_controller_cli import FakeClient, run

    clock = _Clock()
    monkeypatch.setattr("time.monotonic", clock.monotonic)
    monkeypatch.setattr("time.sleep", clock.sleep)
    identity = "33333333-3333-4333-8333-333333333333"
    path = f"/api/profile/applications/{identity}"
    ready = {"id": identity, "state": SUCCEEDED}
    client = FakeClient({("GET", path): [fault, ready]})
    argv = (
        "profile",
        "progress",
        "--application",
        identity,
        "--follow",
        "--timeout-seconds",
        "0.1",
        "--interval-seconds",
        "0.01",
        "--json",
    )
    status, result = run(argv, client)
    if isinstance(fault, ControlConflict):
        assert status == 2
        assert result["http_status"] == 409
    else:
        assert status == 0 and result == ready
    status, result = run(argv, client)
    assert status == 0 and result == ready
    assert all(call[0] == "GET" for call in client.calls)


def test_successor_relationship_is_decided_before_any_watcher_publication(monkeypatch):
    controller = _Successors(_Clock(), different_request=True)
    monkeypatch.setattr("time.monotonic", controller.clock.monotonic)
    monkeypatch.setattr("time.sleep", controller.clock.sleep)
    args = cli._parser().parse_args(
        (
            "profile",
            "progress",
            "--follow",
            "--timeout-seconds",
            "1",
            "--interval-seconds",
            "0.01",
        )
    )
    snapshots = []
    args._watch_callback = lambda snapshot: snapshots.append(dict(snapshot))
    result = _profile(args, cast(ControllerClient, controller), lambda: "fresh")
    assert result["id"] == "0"
    assert snapshots and all(snapshot.get("id", "0") == "0" for snapshot in snapshots)
    assert all(
        snapshot.get("request_key", "original-request") == "original-request"
        for snapshot in snapshots
    )
    assert controller.calls[-1].endswith("/1")
    controller.finish = True
    controller.different_request = False
    result, observation = _observe(monkeypatch, controller)
    assert result["id"] == "1" and observation.status == "complete"


def test_zero_budget_has_reconnect_receipt_and_a_fresh_observer_is_admitted(
    monkeypatch,
):
    from cluster_profiles.cli_states_generated import SUCCEEDED
    from tests.cluster_profiles.test_controller_cli import FakeClient, run

    identity = "33333333-3333-4333-8333-333333333333"
    path = f"/api/profile/applications/{identity}"
    ready = {"id": identity, "state": SUCCEEDED}
    controller = FakeClient({("GET", path): ready})
    argv = (
        "profile",
        "progress",
        "--application",
        identity,
        "--follow",
        "--json",
        "--timeout-seconds",
    )
    status, result = run((*argv, "0"), controller)
    assert status == 2 and result["result"] == {}
    observation = result["observation"]
    assert isinstance(observation, dict)
    assert observation["path"] == path
    assert identity in str(observation["reconnect_command"])
    assert controller.calls == []
    status, result = run((*argv, "1"), controller)
    assert status == 0 and result == ready
    assert len(controller.calls) == 1


@pytest.mark.parametrize(
    "argv,method,path,ready",
    [
        (("profile", "--json"), "GET", "/api/profile/1", {"id": "profile-1"}),
        (("profile", "list", "--json"), "GET", "/api/profile", {"profiles": []}),
        (
            ("--profile", "1", "profile", "load", "--review", "--json"),
            "POST",
            "/api/profile/1/preview",
            {"allowed": True},
        ),
    ],
)
@pytest.mark.parametrize("persistent", [False, True])
def test_profile_read_paths_reobserve_and_a_fresh_read_is_admitted(
    monkeypatch, argv, method, path, ready, persistent
):
    from tests.cluster_profiles.test_controller_cli import FakeClient, run

    clock = _Clock()
    monkeypatch.setattr("time.monotonic", clock.monotonic)
    monkeypatch.setattr("time.sleep", clock.sleep)
    fault = ControlUnavailable(503, "projection unavailable")
    client = FakeClient({(method, path): fault if persistent else [fault, ready]})
    status, result = run(argv, client)
    assert 2 <= len(client.calls) <= 31
    if persistent:
        assert status == 2
        assert result["result"] == {}
    else:
        assert status == 0 and result == ready
    client.responses[(method, path)] = ready
    status, result = run(argv, client)
    assert status == 0 and result == ready
    assert all(call[:2] == (method, path) for call in client.calls)
