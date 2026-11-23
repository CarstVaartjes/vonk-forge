"""Controller CLI: run review reads."""

from __future__ import annotations

import asyncio
import json
import signal
import threading
from typing import cast

import pytest
from controller_cli_support import (
    _RUN_APPLICATION,
    _RUN_DIGEST,
    _RUN_KEY,
    FakeClient,
    _InjectedClock,
    _Prompt,
    _removal_review,
    _run_preview,
    _run_responses,
    run,
)
from library_route_fixtures import _recipe_projection

from cluster_profiles import cli
from cluster_profiles.control_client.errors import (
    ControlConflict,
    ControlMalformedResponse,
    ControlUnavailable,
)
from cluster_profiles.controller_cli import common, profile_authoring, selection
from cluster_profiles.controller_cli import observation as observation_module
from cluster_profiles.controller_cli import run as run_module
from cluster_profiles.controller_cli.selection import _read_concurrently, _skipped_read
from control.tests.insert_collision_support import Rendezvous


def test_run_review_leads_with_profile_sparks_and_what_stops(monkeypatch) -> None:
    preview = _run_preview("b" * 64, _RUN_DIGEST)
    preview["effects"] = {
        "runs": [
            {
                "alias": "coding",
                "action": "stop",
                "run_id": "55555555-5555-4555-8555-555555555555",
                "installation_id": "66666666-6666-4666-8666-666666666666",
                "node_ids": ["spk_" + "a" * 32],
            }
        ],
        "installations": [],
        "superseded": [],
    }
    client = FakeClient(
        {**_run_responses(), ("POST", "/api/profile/1/preview"): preview}
    )
    prompt = _Prompt(monkeypatch, "no")

    cli.main(
        ("--profile", "1", "run", "Qwen Code"),
        control_client=client,
        request_id_factory=lambda: _RUN_KEY,
    )

    lines = [
        prompt.transcript.index(text)
        for text in (
            "Profile edited: 1 (Default)",
            "Sparks selected: Atlas",
            "Will stop: coding",
            "Ready for review",
        )
    ]
    assert lines == sorted(lines)


@pytest.mark.parametrize(
    ("argv", "effect_suffix"),
    [
        (("--profile", "1", "profile", "load"), "/load"),
        (("model", "remove", "qwen"), "/remove"),
        (("recipe", "remove", "vision", "--keep-model"), "/remove"),
    ],
)
def test_declined_action_exits_nonzero_without_an_effect(
    argv, effect_suffix, monkeypatch
) -> None:
    client = FakeClient(
        {
            ("POST", "/api/profile/1/preview"): _run_preview("b" * 64, _RUN_DIGEST),
            ("GET", "/api/model/qwen/remove-review"): _removal_review(
                "model", "qwen", model_digest="a" * 64
            ),
            ("GET", "/api/recipe/vision/remove-review"): _removal_review(
                "recipe", "vision", with_model=False
            ),
        }
    )
    prompt = _Prompt(monkeypatch, "no")

    status = cli.main(argv, control_client=client, request_id_factory=lambda: _RUN_KEY)

    assert status == 2
    assert not [
        path
        for method, path, *_ in client.calls
        if method == "POST" and path.endswith(effect_suffix)
    ]
    assert "control.api_error" not in prompt.transcript


def test_run_binds_the_load_to_the_plan_it_showed_and_asks_again_when_it_changed(
    monkeypatch,
) -> None:
    changed = "f" * 64
    stale = ControlConflict(
        409, "The profile plan changed", code="profile.review_stale"
    )
    receipt = {
        "id": _RUN_APPLICATION,
        "request_key": _RUN_KEY,
        "state": "succeeded",
        "progress": {},
    }
    client = FakeClient(
        _run_responses()
        | {
            ("POST", "/api/profile/1/preview"): [
                _run_preview("b" * 64, _RUN_DIGEST),
                _run_preview("c" * 64, changed),
            ],
            ("POST", "/api/profile/1/load"): [stale, receipt],
        }
    )
    prompt = _Prompt(monkeypatch, "yes", "yes")

    status = cli.main(
        ("--profile", "1", "run", "Qwen Code", "--spark", "Atlas"),
        control_client=client,
        request_id_factory=lambda: _RUN_KEY,
    )

    assert status == 0, prompt.transcript
    loads = [call[2] for call in client.calls if call[1] == "/api/profile/1/load"]
    assert loads == [
        {"request_key": _RUN_KEY, "review": {"effects_digest": _RUN_DIGEST}},
        {"request_key": _RUN_KEY, "review": {"effects_digest": changed}},
    ]
    assert prompt.transcript.count("[y/N]") == 2


def test_run_yes_forwards_the_current_owner_review_binding() -> None:
    client = FakeClient(_run_responses())

    status = cli.main(
        ("--json", "--profile", "1", "run", "Qwen Code", "--spark", "Atlas", "--yes"),
        control_client=client,
        request_id_factory=lambda: _RUN_KEY,
    )

    assert status == 0
    loads = [call[2] for call in client.calls if call[1] == "/api/profile/1/load"]
    assert loads == [
        {"request_key": _RUN_KEY, "review": {"effects_digest": _RUN_DIGEST}}
    ]


def test_profile_list_traverses_the_server_continuation():
    # A full first page cannot hide later profiles from ordinary CLI output.
    client = FakeClient(
        {
            ("GET", "/api/profile"): [
                {
                    "profiles": [{"number": number} for number in range(1, 129)],
                    "next_after": 128,
                },
                {"profiles": [{"number": 129}], "next_after": None},
            ]
        }
    )
    status, result = run(("profile", "list", "--json"), client)
    assert status == 0
    profiles = result["profiles"]
    assert isinstance(profiles, list)
    assert [item["number"] for item in profiles] == list(range(1, 130))
    assert client.calls[-1][3] == {"after": 128}


# Independent CLI selection reads run at once and keep their own outcomes.
# Every fake read waits on a barrier that opens only when all of them are in
# flight together, so concurrency is proven by ordering, not by elapsed time.


def _barrier(parties: int) -> Rendezvous:
    return Rendezvous(parties, timeout=30)  # wall-clock: hang guard


def _meeting(barrier: Rendezvous, value: object, *, fail: bool = False):
    def read() -> object:
        barrier.wait()
        if fail:
            raise ValueError("fleet is unavailable")
        return value

    return read


def test_independent_reads_are_in_flight_together() -> None:
    barrier = _barrier(3)

    outcomes = _read_concurrently(
        _meeting(barrier, "a"),
        _meeting(barrier, "b"),
        _meeting(barrier, "c"),
    )

    assert [outcome.get() for outcome in outcomes] == ["a", "b", "c"]


def test_one_failing_read_does_not_hide_the_others() -> None:
    barrier = _barrier(3)

    first, failed, last = _read_concurrently(
        _meeting(barrier, "a"),
        _meeting(barrier, "b", fail=True),
        _meeting(barrier, "c"),
    )

    assert first.get() == "a"
    assert last.get() == "c"
    with pytest.raises(ValueError, match="fleet is unavailable"):
        failed.get()


def test_definition_read_past_its_deadline_reports_its_own_timed_out_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The definition read's own typed outcome reaches the operator, every run.

    The concurrency helper must never put a generic error in its place, so the
    run is repeated to prove no scheduling order changes the answer.
    """
    for _ in range(50):
        clock = _InjectedClock()
        for module in (observation_module, selection, profile_authoring):
            monkeypatch.setattr(module, "time", clock)
        responses = dict(_ADD_RESPONSES)
        responses[("GET", "/api/profile/2/definition")] = ControlUnavailable(
            503, "control API unavailable"
        )
        client = FakeClient(responses)

        status, payload = run(
            (
                "--profile",
                "2",
                "profile",
                "add",
                "Recipe UUID",
                "--spark",
                "Atlas",
                "--timeout-seconds",
                "1",
                "--json",
            ),
            client,
        )

        assert status == 2
        observation = payload["observation"]
        assert isinstance(observation, dict)
        assert observation["status"] == "timed_out"
        assert observation["path"] == "/api/profile/2/definition"
        assert observation["reconnect_command"]
        assert "deadline reached" not in json.dumps(payload)
        assert not any(call[0] == "PUT" for call in client.calls)


def test_a_single_real_read_runs_inline_and_keeps_its_error() -> None:

    seen: list[threading.Thread] = []

    def broken() -> object:
        seen.append(threading.current_thread())
        raise OSError("down")

    outcome, skipped = _read_concurrently(broken, _skipped_read)

    assert seen == [threading.current_thread()]
    assert skipped.get() is None
    with pytest.raises(OSError):
        outcome.get()


def test_a_read_that_dies_with_a_base_exception_does_not_hang_the_waiter() -> None:
    def cancelled() -> object:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        _read_concurrently(cancelled, lambda: "fine")


def test_ctrl_c_interrupts_at_once_while_reads_hang() -> None:
    release = threading.Event()

    def hanging() -> object:
        release.wait(30)  # wall-clock: hang guard
        return "late"

    main = threading.get_ident()
    sender = threading.Timer(0.2, lambda: signal.pthread_kill(main, signal.SIGINT))
    sender.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            _read_concurrently(hanging, hanging)
    finally:
        release.set()


class _MeetingClient(FakeClient):
    """Holds the first GET of each named path until all of them are in flight."""

    def __init__(self, responses, meet: set[str], failing: str | None = None):
        super().__init__(responses)
        self._barrier = _barrier(len(meet))
        self._meet = meet
        self._failing = failing
        self._first: set[str] = set()
        self._lock = threading.Lock()
        self.completed: set[str] = set()

    def request(self, method, path, *args, **kwargs):
        with self._lock:
            first = path in self._meet and path not in self._first
            self._first.add(path)
        if first:
            self._barrier.wait()
        if path == self._failing:
            raise ControlMalformedResponse("peer is down")
        result = super().request(method, path, *args, **kwargs)
        self.completed.add(path)
        return result


_ATLAS = {"id": "spk_" + "a" * 32, "display_name": "Atlas", "hostname": "atlas"}


_ADD_RESPONSES = {
    ("GET", "/api/profile/2/definition"): {
        "id": "11111111-1111-4111-8111-111111111111",
        "number": 2,
        "revision": 7,
        "definition": {"name": "Coding", "assignments": []},
    },
    ("GET", "/api/recipe/library"): {
        "recipes": [_recipe_projection("vonk-forge/recipe-uuid", "Recipe UUID")],
        "next_cursor": None,
    },
    ("GET", "/api/fleet"): {"nodes": [_ATLAS]},
    ("PUT", "/api/profile/2"): {"number": 2, "revision": 8},
}


_ADD_PATHS = {"/api/profile/2/definition", "/api/recipe/library", "/api/fleet"}


def _profile_add(client: FakeClient) -> tuple[int, dict[str, object]]:
    return run(
        (
            "--profile",
            "2",
            "profile",
            "add",
            "Recipe UUID",
            "--spark",
            "Atlas",
            "--yes",
            "--json",
        ),
        client,
    )


def test_profile_add_reads_definition_recipe_and_sparks_together() -> None:
    client = _MeetingClient(dict(_ADD_RESPONSES), _ADD_PATHS)

    status, _ = _profile_add(client)

    assert status == 0
    assert client.completed >= _ADD_PATHS


def test_profile_add_failing_peer_leaves_the_other_reads_intact() -> None:
    client = _MeetingClient(dict(_ADD_RESPONSES), _ADD_PATHS, failing="/api/fleet")

    status, _ = _profile_add(client)

    assert status != 0
    assert {"/api/profile/2/definition", "/api/recipe/library"} <= client.completed
    assert not any(call[0] == "PUT" for call in client.calls)


def _run_command(client: FakeClient) -> None:
    args = cli._parser().parse_args(
        ["--profile", "1", "run", "Qwen Code", "--yes", "--json"]
    )
    run_module._run(
        args,
        cast(common.ControllerClient, client),
        lambda: "11111111-1111-4111-8111-111111111111",
    )


class _StopAfterReads(RuntimeError):
    pass


_RUN_RESPONSES = {
    ("GET", "/api/recipe/library"): {
        "recipes": [_recipe_projection("vonk-forge/qwen-code", "Qwen Code")],
        "next_cursor": None,
    },
    ("GET", "/api/fleet"): {"nodes": [_ATLAS]},
    # Ends the command right after the two reads under test.
    ("GET", "/api/profile/1/definition"): _StopAfterReads(),
}


_RUN_PATHS = {"/api/recipe/library", "/api/fleet"}


def test_run_reads_the_library_and_the_fleet_together() -> None:
    client = _MeetingClient(dict(_RUN_RESPONSES), _RUN_PATHS)

    with pytest.raises(_StopAfterReads):
        _run_command(client)

    assert client.completed >= _RUN_PATHS


def test_run_failing_fleet_read_leaves_the_library_result_intact() -> None:
    client = _MeetingClient(dict(_RUN_RESPONSES), _RUN_PATHS, failing="/api/fleet")

    with pytest.raises(ControlMalformedResponse):
        _run_command(client)

    assert "/api/profile/1/definition" not in client.completed
    assert "/api/recipe/library" in client.completed


def test_profile_read_fetches_spark_names_for_people_but_not_for_json(capsys):
    """Catches ids in human output, and names leaking into the JSON document."""
    profile = {
        "number": 1,
        "name": "p",
        "definition": {
            "assignments": [{"recipe_selector": "r", "spark_ids": ["spk_1"]}]
        },
        "assignments": [],
    }
    roster = {"nodes": [{"id": "spk_1", "display_name": "spark-a"}]}
    responses = {("GET", "/api/profile/1"): profile, ("GET", "/api/fleet"): roster}

    client = FakeClient(dict(responses))
    status, document = run(("profile", "--json"), client)
    assert status == 0
    assert "spark_names" not in document
    assert ("GET", "/api/fleet") not in [call[:2] for call in client.calls]

    client = FakeClient(dict(responses))
    assert cli.main(("profile",), control_client=client) == 0
    capsys.readouterr()
    assert ("GET", "/api/fleet") in [call[:2] for call in client.calls]
