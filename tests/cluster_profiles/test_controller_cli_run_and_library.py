"""Controller CLI: run and library."""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib.resources import files
from typing import cast

import pytest
from controller_cli_support import (
    _RUN_DIGEST,
    _RUN_KEY,
    FakeClient,
    _Prompt,
    _run_preview,
    _run_responses,
    run,
)
from library_route_fixtures import _recipe_projection

from cluster_profiles import cli
from cluster_profiles.control_client.errors import (
    ControlConflict,
    ControlMalformedResponse,
    ControlNotFound,
    ControlTransportError,
    ControlUnavailable,
)
from cluster_profiles.controller_cli import common, dispatch
from cluster_profiles.controller_cli import observation as observation_module
from cluster_profiles.controller_cli import run as run_module
from cluster_profiles.controller_cli import selection as selection_module
from cluster_profiles.generated_control.models.fleet_profile_endpoints_view import (
    FleetProfileEndpointsView,
)


@pytest.mark.parametrize("since", ["yesterday", "15", "-1m", "2026-09-13T08:00:00"])
def test_log_since_rejects_ambiguous_windows_before_request(since: str) -> None:
    client = FakeClient({})
    assert (
        run(("fleet", "loginfo", "Atlas", "--since", since, "--json"), client)[0] == 2
    )
    assert not client.calls


@pytest.mark.parametrize("follow", [False, True])
def test_fleet_progress_never_reports_another_job(follow: bool) -> None:
    expected = "33333333-3333-4333-8333-333333333333"
    other = "44444444-4444-4444-8444-444444444444"
    path = f"/api/jobs/{expected}"
    wrong = {"id": other, "state": "succeeded"}
    repaired = {"id": expected, "state": "succeeded"}
    responses = (
        [{"id": expected, "state": "running"}, wrong, repaired]
        if follow
        else [wrong, repaired]
    )
    client = FakeClient({("GET", path): responses})
    arguments = ["fleet", "progress", expected, "--json"]
    if follow:
        arguments += ["--follow", "--interval-seconds", "0.01"]

    status, payload = run(tuple(arguments), client)

    assert status == 0
    assert payload == repaired
    assert [call[1] for call in client.calls] == [path] * (3 if follow else 2)


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("vonk-forge/qwen-code", "vonk-forge/qwen-code"),
        ("Qwen 3", "vonk-forge/qwen-code"),
    ],
)
def test_run_resolves_a_recipe_or_its_model_to_one_catalog_recipe(
    requested: str, expected: str
) -> None:
    display = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-display.json")
        .read_text()
    )
    display["model_title"] = "Qwen 3"
    client = FakeClient(
        {
            ("GET", "/api/model/library"): {
                "models": [{"selector": "Qwen/qwen3", "model_display": display}],
                "next_cursor": None,
            },
            ("GET", "/api/recipe/library"): {
                "recipes": [
                    _recipe_projection(
                        "vonk-forge/qwen-code", "Qwen Code", model_selector="Qwen/qwen3"
                    )
                ],
                "next_cursor": None,
            },
        }
    )
    assert (
        run_module._resolve_run_recipe(
            cast(common.ControllerClient, client),
            requested,
            deadline=observation_module.time.monotonic() + 30,
        )
        == expected
    )


def test_run_title_resolution_reads_the_model_library_in_pages_not_per_model() -> None:
    display = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-display.json")
        .read_text()
    )
    total, page_size = 150, 50
    pages = [
        {
            "models": [
                {
                    "selector": f"Org/model-{index}",
                    "model_display": {**display, "model_title": f"Title {index}"},
                }
                for index in range(start, start + page_size)
            ],
            "next_cursor": f"c{start + page_size}"
            if start + page_size < total
            else None,
        }
        for start in range(0, total, page_size)
    ]
    client = FakeClient(
        {
            ("GET", "/api/model/library"): pages,
            ("GET", "/api/recipe/library"): {
                "recipes": [
                    _recipe_projection(
                        f"vonk-forge/recipe-{index}",
                        f"Recipe {index}",
                        model_selector=f"Org/model-{index}",
                    )
                    for index in range(total)
                ],
                "next_cursor": None,
            },
        }
    )
    assert (
        run_module._resolve_run_recipe(
            cast(common.ControllerClient, client),
            "title 120",
            deadline=observation_module.time.monotonic() + 30,
        )
        == "vonk-forge/recipe-120"
    )
    model_reads = [call for call in client.calls if call[1].startswith("/api/model")]
    assert len(model_reads) == total // page_size
    assert {call[1] for call in model_reads} == {"/api/model/library"}


class _PagedClient(FakeClient):
    """A library peer answering each page from ``page_for(call_number)``."""

    def __init__(self, page_for, on_request=lambda: None):
        super().__init__({})
        self.page_for = page_for
        self.on_request = on_request
        self.queries: list[dict[str, object]] = []

    def request(self, method, path, payload=None, **kwargs):
        self.on_request()
        self.calls.append((method, path, payload, kwargs.get("query")))
        self.queries.append(dict(kwargs.get("query") or {}))
        return self.page_for(len(self.queries))


def _read_pages(client, deadline=1e9):
    return list(
        selection_module._library_pages(
            cast(common.ControllerClient, client),
            "/api/model/library",
            "models",
            {"sort": "name"},
            deadline=deadline,
        )
    )


def test_library_pages_follow_cursors_to_the_last_page_with_the_shared_page_size():
    pages = {
        None: {"models": [1], "next_cursor": "a"},
        "a": {"models": [2], "next_cursor": "b"},
        "b": {"models": [3], "next_cursor": None},
    }
    client = _PagedClient(lambda n: pages[client.queries[-1].get("cursor")])
    assert _read_pages(client) == [[1], [2], [3]]
    assert {query["limit"] for query in client.queries} == {512}
    assert [query.get("cursor") for query in client.queries] == [None, "a", "b"]


@pytest.mark.parametrize(
    ("second", "message"),
    [
        ({"models": [], "next_cursor": "a"}, "cursor repeated"),
        ({"models": [], "next_cursor": ""}, "invalid cursor"),
        ({"models": [], "next_cursor": 7}, "invalid cursor"),
        ({"items": [], "next_cursor": None}, "does not contain models"),
        ({"models": {}, "next_cursor": None}, "does not contain models"),
    ],
)
def test_library_pages_refuse_a_malformed_page(second, message):
    first = {"models": [], "next_cursor": "a"}
    client = _PagedClient(lambda n: first if n == 1 else second)
    with pytest.raises(ControlMalformedResponse, match=message):
        _read_pages(client)


def test_runaway_library_cursor_ends_at_the_deadline_and_a_fresh_resolve_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Clock:
        now = 0.0

        def monotonic(self) -> float:
            return self.now

    clock = Clock()
    monkeypatch.setattr(selection_module, "time", clock)

    def tick() -> None:
        clock.now += 1.0

    # Every page carries a fresh cursor: only the deadline can end the read.
    runaway = _PagedClient(
        lambda n: {"models": [], "next_cursor": f"fresh-{n}"}, on_request=tick
    )
    with pytest.raises(
        ControlMalformedResponse, match="selection observation deadline reached"
    ):
        run_module._model_titles(cast(common.ControllerClient, runaway), deadline=30.0)
    assert (
        len(runaway.calls) == 30
    )  # one page per injected second, ended by the deadline

    # The expired read leaves nothing behind: a fresh resolve is admitted.
    display = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-display.json")
        .read_text()
    )
    fresh = FakeClient(
        {
            ("GET", "/api/model/library"): {
                "models": [{"selector": "Qwen/qwen3", "model_display": display}],
                "next_cursor": None,
            },
            ("GET", "/api/recipe/library"): {
                "recipes": [
                    _recipe_projection(
                        "vonk-forge/qwen-code", "Qwen Code", model_selector="Qwen/qwen3"
                    )
                ],
                "next_cursor": None,
            },
        }
    )
    assert (
        run_module._resolve_run_recipe(
            cast(common.ControllerClient, fresh),
            "vonk-forge/qwen-code",
            deadline=clock.now + 30,
        )
        == "vonk-forge/qwen-code"
    )


def test_run_parser_exposes_spark_confirmation_and_wait_controls() -> None:
    args = cli._parser().parse_args(
        ["--profile", "3", "run", "Qwen Code", "--spark", "Atlas", "--yes"]
    )
    assert args.command == "run"
    assert args.profile_number == 3
    assert args.selector == "Qwen Code"
    assert args.spark == ["Atlas"]
    assert args.yes is True
    assert args.timeout_seconds == 30


@pytest.mark.parametrize(
    "reason_code", [None, "profile.spark_offline", "profile.node_revoked"]
)
def test_run_reviews_and_waits_before_reporting_endpoint(
    reason_code: str | None,
) -> None:
    profile = {
        "name": "Default",
        "description": "",
        "favorite": False,
        "installation_policy": "keep-cached",
        "labels": {},
        "assignments": [],
    }
    paths: list[str] = []
    application_id = "33333333-3333-4333-8333-333333333333"

    class RunClient:
        request_timeout_seconds = 1.0

        def request(
            self,
            method: str,
            path: str,
            payload: Mapping[str, object] | None = None,
            *,
            extra_headers: Mapping[str, str] | None = None,
            query: Mapping[str, object] | None = None,
            timeout_seconds: float | None = None,
            retry: bool = True,
        ) -> dict[str, object]:
            del extra_headers, query, timeout_seconds
            paths.append(path)
            if path == "/api/recipe/library":
                return {
                    "recipes": [
                        _recipe_projection("vonk-forge/qwen-code", "Qwen Code")
                    ],
                    "next_cursor": None,
                }
            if path == "/api/fleet":
                return {"nodes": [{"id": "spk_" + "a" * 32, "display_name": "Atlas"}]}
            if path == "/api/profile/1/definition":
                return {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "number": 1,
                    "revision": 0,
                    "definition": profile,
                }
            if method == "PUT" and path == "/api/profile/1":
                assert payload is not None
                profile.update(payload)
                profile.pop("expected_revision", None)
                return {"number": 1, "revision": 1, "definition": profile}
            if path == "/api/profile/1/preview":
                return {
                    "allowed": reason_code is None,
                    "plan_digest": "b" * 64,
                    "effects_digest": "c" * 64,
                    "reasons": []
                    if reason_code is None
                    else [{"code": reason_code, "detail": "Spark is not ready"}],
                }
            if method == "POST" and path == "/api/profile/1/load":
                assert payload is not None
                return {
                    "id": application_id,
                    "request_key": payload["request_key"],
                    "state": "running",
                    "progress": {
                        "intended_profile": {"reviewed_plan_digest": "b" * 64}
                    },
                }
            if path == f"/api/profile/applications/{application_id}":
                return {"id": application_id, "state": "succeeded"}
            if path == "/api/profile/1/endpoints":
                return self.profile_endpoints(1).to_dict()
            raise AssertionError((method, path, payload))

        def profile_endpoints(self, number, alias=None):
            assert number == 1
            assert alias is None
            return FleetProfileEndpointsView.from_dict(
                {
                    "assignments": [],
                    "number": 1,
                    "observed_at": "2026-09-27T10:00:00Z",
                    "application_id": application_id,
                    "application_state": "succeeded",
                }
            )

    args = cli._parser().parse_args(
        ["--profile", "1", "run", "Qwen Code", "--spark", "Atlas", "--yes", "--json"]
    )
    result = dispatch.run_controller(
        args,
        cast(common.ControllerClient, RunClient()),
        lambda: "11111111-1111-4111-8111-111111111111",
    )
    # An ordinary blocker is not a client-side stop: the load is submitted and
    # the Controller parks or refuses it.
    assert result["state"] == "succeeded"
    endpoints = result["endpoints"]
    assert isinstance(endpoints, Mapping)
    assert endpoints["application_id"] == application_id
    assert profile["assignments"][0]["recipe_selector"] == "vonk-forge/qwen-code"
    assert "/api/profile/1/load" in paths
    # The Controller prepares what the load needs; the client no longer downloads.
    assert not any("/download" in path for path in paths)


@pytest.mark.parametrize(
    "arguments",
    [
        ["recipe", "download", "vonk-forge/qwen-code"],
        ["model", "download", "vonk-forge/qwen"],
        ["recipe", "update", "--all"],
        ["fleet", "rename", "Atlas", "Nova"],
        ["fleet", "enroll", "Nova", "--output", "/tmp/nova.json"],
        ["profile", "add", "vonk-forge/qwen-code", "--spark", "Atlas"],
        ["key", "create", "app"],
    ],
)
def test_every_mutating_command_accepts_yes(arguments: list[str]) -> None:
    args = cli._parser().parse_args([*arguments, "--yes"])
    assert args.yes is True


def _zero_target_upgrade_client(job_id: str, request_key: str) -> FakeClient:
    return FakeClient(
        {
            ("POST", "/api/fleet/upgrade"): {
                "action": "upgrade",
                "state": "succeeded",
                "operation_id": job_id,
                "plan_digest": "a" * 64,
                "request_key": request_key,
                "targets": [],
            },
        }
    )


def test_fleet_upgrade_with_no_targets_does_not_follow() -> None:
    request_key = "11111111-1111-4111-8111-111111111111"
    client = _zero_target_upgrade_client("upgrade-job", request_key)

    status = cli.main(
        ("fleet", "upgrade", "--all", "--yes", "--request-key", request_key),
        control_client=client,
    )

    assert status == 0
    assert [call[1] for call in client.calls] == ["/api/fleet/upgrade"]


def test_fleet_upgrade_with_no_targets_json_keeps_the_receipt() -> None:
    request_key = "11111111-1111-4111-8111-111111111111"
    client = _zero_target_upgrade_client("upgrade-job", request_key)

    status, result = run(
        (
            "fleet",
            "upgrade",
            "--all",
            "--yes",
            "--request-key",
            request_key,
            "--json",
        ),
        client,
    )

    assert status == 0
    assert result["state"] == "succeeded"
    assert result["targets"] == []


def test_job_rendering_tolerates_a_missing_progress_object(capsys) -> None:
    job_id = "upgrade-job"
    client = FakeClient(
        {
            ("GET", f"/api/jobs/{job_id}"): {
                "id": job_id,
                "kind": "agent-upgrade",
                "state": "succeeded",
                "targets": [],
                "operations": [],
                "progress": None,
            }
        }
    )

    status = cli.main(("fleet", "progress", job_id), control_client=client)

    captured = capsys.readouterr()
    assert status == 0
    assert job_id in captured.out
    assert "not a valid object" not in captured.out + captured.err


def _effects(client: FakeClient) -> list[tuple[str, str]]:
    """The calls that change something, ignoring reads and the plan review."""

    return [
        (method, path)
        for method, path, *_ in client.calls
        if method in {"PUT", "DELETE"} or path.endswith(("/load", "/remove"))
    ]


def test_run_lost_load_response_reports_the_submission_to_reconnect_to(
    vonkctl_json,
) -> None:
    client = FakeClient(
        _run_responses()
        | {
            ("POST", "/api/profile/1/load"): ControlTransportError("response lost"),
            ("GET", f"/api/profile/1/requests/{_RUN_KEY}"): ControlNotFound(
                404, "request was not accepted"
            ),
        }
    )

    status, document = vonkctl_json(
        "--profile",
        "1",
        "run",
        "Qwen Code",
        "--spark",
        "Atlas",
        "--yes",
        control_client=client,
        request_id_factory=lambda: _RUN_KEY,
    )

    assert status == 2
    assert document["result"] == {}
    assert (
        f"profile progress --request-key {_RUN_KEY}"
        in document["observation"]["reconnect_command"]
    )
    assert (
        len(
            [
                call
                for call in client.calls
                if call[:2] == ("POST", "/api/profile/1/load")
            ]
        )
        == 2
    )
    client.responses.update(_run_responses())
    status, fresh = vonkctl_json(
        "--profile",
        "1",
        "run",
        "Qwen Code",
        "--spark",
        "Atlas",
        "--yes",
        control_client=client,
        request_id_factory=lambda: _RUN_KEY,
    )
    assert status == 0
    assert fresh["application"]["request_key"] == _RUN_KEY


@pytest.mark.parametrize("flags", [(), ("--no-input",), ("--json",)])
def test_run_that_cannot_be_confirmed_saves_nothing(flags) -> None:
    client = FakeClient(_run_responses())

    status = cli.main(
        (*flags, "--profile", "1", "run", "Qwen Code", "--spark", "Atlas"),
        control_client=client,
        request_id_factory=lambda: _RUN_KEY,
    )

    assert status == 2
    assert _effects(client) == []


def _bodies(client: FakeClient, method: str, path: str) -> list[dict]:
    """The request bodies sent to one route, in order."""
    return [
        cast(dict, call[2])
        for call in client.calls
        if call[0] == method and call[1] == path
    ]


def test_declined_run_sends_no_put_and_leaves_the_profile_unchanged(
    monkeypatch,
) -> None:
    client = FakeClient(_run_responses())
    prompt = _Prompt(monkeypatch, "no")

    status = cli.main(
        ("--profile", "1", "run", "Qwen Code", "--spark", "Atlas"),
        control_client=client,
        request_id_factory=lambda: _RUN_KEY,
    )

    assert status == 2
    assert _effects(client) == []
    assert "control.api_error" not in prompt.transcript
    # The reviewed plan was the unsaved draft's, with this recipe in it.
    (draft,) = _bodies(client, "POST", "/api/profile/1/preview")
    assert [a["recipe_selector"] for a in draft["definition"]["assignments"]] == [
        "vonk-forge/qwen-code"
    ]


def test_accepted_run_saves_the_reviewed_draft_then_loads_bound_to_its_digest(
    monkeypatch,
) -> None:
    client = FakeClient(_run_responses())
    prompt = _Prompt(monkeypatch, "yes")
    client.responses[("PUT", "/api/profile/1")] = {
        "number": 1,
        "revision": 1,
        "definition": {},
        "warnings": ["Option x is no longer offered by the recipe"],
    }

    status = cli.main(
        ("--profile", "1", "run", "Qwen Code", "--spark", "Atlas"),
        control_client=client,
        request_id_factory=lambda: _RUN_KEY,
    )

    assert status == 0, prompt.transcript
    sent = [(c[0], c[1]) for c in client.calls if c[0] in {"PUT", "POST"}]
    assert sent == [
        ("POST", "/api/profile/1/preview"),
        ("PUT", "/api/profile/1"),
        ("POST", "/api/profile/1/load"),
    ]
    (put,) = _bodies(client, "PUT", "/api/profile/1")
    (preview,) = _bodies(client, "POST", "/api/profile/1/preview")
    assert put["expected_revision"] == 0
    assert {k: v for k, v in put.items() if k != "expected_revision"} == preview[
        "definition"
    ]
    (load,) = _bodies(client, "POST", "/api/profile/1/load")
    assert load["review"] == {"effects_digest": _RUN_DIGEST}
    assert "no longer offered" in prompt.transcript


def test_run_rebuilds_on_the_newest_profile_when_the_save_conflicts(
    monkeypatch,
) -> None:
    base = _run_responses()
    older = base[("GET", "/api/profile/1/definition")]
    older = cast(dict, older)
    newest = {
        **older,
        "revision": 5,
        "definition": {**older["definition"], "description": "edited elsewhere"},
    }
    client = FakeClient(
        base
        | {
            ("GET", "/api/profile/1/definition"): [older, newest],
            ("PUT", "/api/profile/1"): [
                ControlConflict(
                    409, "profile revision conflict", code="profile.revision_conflict"
                ),
                base[("PUT", "/api/profile/1")],
            ],
        }
    )
    prompt = _Prompt(monkeypatch, "yes", "yes")

    status = cli.main(
        ("--profile", "1", "run", "Qwen Code", "--spark", "Atlas"),
        control_client=client,
        request_id_factory=lambda: _RUN_KEY,
    )

    assert status == 0, prompt.transcript
    puts = [
        put["expected_revision"] for put in _bodies(client, "PUT", "/api/profile/1")
    ]
    assert puts == [0, 5]
    assert len(_bodies(client, "POST", "/api/profile/1/preview")) == 2


def test_run_converges_when_the_profile_already_holds_the_assignment(
    monkeypatch,
) -> None:
    """A rerun after a death between save and load: equal-content save, bound load."""
    base = _run_responses()
    first = cli.main
    client = FakeClient(base)
    _Prompt(monkeypatch, "yes")
    assert (
        first(
            ("--profile", "1", "run", "Qwen Code", "--spark", "Atlas"),
            control_client=client,
            request_id_factory=lambda: _RUN_KEY,
        )
        == 0
    )
    (saved,) = _bodies(client, "PUT", "/api/profile/1")
    held = {k: v for k, v in saved.items() if k != "expected_revision"}
    rerun = FakeClient(
        base
        | {
            ("GET", "/api/profile/1/definition"): {
                **cast(dict, base[("GET", "/api/profile/1/definition")]),
                "revision": 1,
                "definition": held,
            }
        }
    )
    _Prompt(monkeypatch, "yes")

    status = cli.main(
        ("--profile", "1", "run", "Qwen Code", "--spark", "Atlas"),
        control_client=rerun,
        request_id_factory=lambda: _RUN_KEY,
    )

    assert status == 0
    (put,) = _bodies(rerun, "PUT", "/api/profile/1")
    assert put == {**held, "expected_revision": 1}
    (load,) = _bodies(rerun, "POST", "/api/profile/1/load")
    assert load["review"] == {"effects_digest": _RUN_DIGEST}


def test_run_ends_with_the_observation_when_the_profile_read_times_out(
    vonkctl_json,
) -> None:
    client = FakeClient(
        _run_responses()
        | {("GET", "/api/profile/1/definition"): ControlUnavailable(503, "down")}
    )

    status, document = vonkctl_json(
        "--profile",
        "1",
        "run",
        "Qwen Code",
        "--spark",
        "Atlas",
        "--yes",
        "--timeout-seconds",
        "1",
        "--interval-seconds",
        "0.05",
        control_client=client,
        request_id_factory=lambda: _RUN_KEY,
    )

    assert status == 2
    assert document["observation"]["status"] == "timed_out"
    assert _effects(client) == []


_RUN_OPTIONS = [
    {
        "name": "verification",
        "label": "Verification",
        "help": "How drafted tokens are verified.",
        "choices": [
            {"value": "standard", "label": "Standard", "help": "h", "default": True},
            {"value": "adaptive-k", "label": "Adaptive", "help": "h"},
        ],
    },
    {
        "name": "projections",
        "label": "Projections",
        "help": "Dense projection precision.",
        "choices": [
            {"value": "stock", "label": "Stock", "help": "h", "default": True},
            {"value": "dense-fp8", "label": "FP8", "help": "h"},
        ],
    },
]


_ATLAS_ID = "spk_" + "a" * 32


def _option_run(definitions: list[dict]) -> FakeClient:
    """A run whose recipe declares options and whose first save hits a race."""
    base = _run_responses()
    detail = cast(dict, base[("GET", "/api/recipe/vonk-forge%2Fqwen-code")])
    detail["projected"]["options"] = _RUN_OPTIONS
    view = cast(dict, base[("GET", "/api/profile/1/definition")])
    return FakeClient(
        base
        | {
            ("GET", "/api/profile/1/definition"): [
                {**view, "revision": number, "definition": definition}
                for number, definition in enumerate(definitions)
            ],
            ("PUT", "/api/profile/1"): [
                ControlConflict(
                    409, "profile revision conflict", code="profile.revision_conflict"
                ),
                base[("PUT", "/api/profile/1")],
            ],
        }
    )


def _empty_definition() -> dict:
    return {
        "name": "Default",
        "description": "",
        "favorite": False,
        "installation_policy": "keep-cached",
        "labels": {},
        "assignments": [],
    }


def _assignment(choices: dict[str, str]) -> dict:
    return {
        "recipe_selector": "vonk-forge/qwen-code",
        "spark_ids": [_ATLAS_ID],
        "desired_state": "running",
        "option_choices": choices,
    }


def _saved_choices(client: FakeClient) -> list[dict]:
    return [
        put["assignments"][0]["option_choices"]
        for put in _bodies(client, "PUT", "/api/profile/1")
    ]


def test_run_keeps_option_answers_across_a_rebuild_without_asking_again(
    monkeypatch,
) -> None:
    client = _option_run([_empty_definition(), _empty_definition()])
    # Two answers for the options, then consent in each of the two rounds.
    _Prompt(monkeypatch, "adaptive-k", "dense-fp8", "yes", "yes")

    status = cli.main(
        ("--profile", "1", "run", "Qwen Code", "--spark", "Atlas"),
        control_client=client,
        request_id_factory=lambda: _RUN_KEY,
    )

    assert status == 0
    chosen = {"verification": "adaptive-k", "projections": "dense-fp8"}
    assert _saved_choices(client) == [chosen, chosen]


def test_run_lets_a_newer_saved_choice_win_over_one_it_never_asked(
    monkeypatch,
) -> None:
    saved = _assignment({"projections": "stock"})
    newer = _assignment({"projections": "dense-fp8"})
    client = _option_run(
        [
            {**_empty_definition(), "assignments": [saved]},
            {**_empty_definition(), "assignments": [newer]},
        ]
    )
    # Only the option without a saved choice is asked, in round one.
    _Prompt(monkeypatch, "adaptive-k", "yes", "yes")

    status = cli.main(
        ("--profile", "1", "run", "Qwen Code", "--spark", "Atlas"),
        control_client=client,
        request_id_factory=lambda: _RUN_KEY,
    )

    assert status == 0
    assert _saved_choices(client) == [
        {"projections": "stock", "verification": "adaptive-k"},
        {"projections": "dense-fp8", "verification": "adaptive-k"},
    ]


def test_run_does_not_retry_a_refused_save_that_is_not_a_newer_profile(
    monkeypatch,
) -> None:
    client = FakeClient(
        _run_responses()
        | {("PUT", "/api/profile/1"): ControlConflict(409, "recipe is not offered")}
    )
    _Prompt(monkeypatch, "yes", "yes")

    status = cli.main(
        ("--profile", "1", "run", "Qwen Code", "--spark", "Atlas"),
        control_client=client,
        request_id_factory=lambda: _RUN_KEY,
    )

    assert status == 2
    assert _effects(client) == [("PUT", "/api/profile/1")]


def test_run_refuses_before_writing_when_the_controller_ignores_the_draft(
    monkeypatch,
) -> None:
    saved_row_preview = {**_run_preview("b" * 64, _RUN_DIGEST), "profile_revision": 3}
    client = FakeClient(
        _run_responses() | {("POST", "/api/profile/1/preview"): saved_row_preview}
    )
    _Prompt(monkeypatch, "yes")

    status = cli.main(
        ("--profile", "1", "run", "Qwen Code", "--spark", "Atlas"),
        control_client=client,
        request_id_factory=lambda: _RUN_KEY,
    )

    assert status == 2
    assert _effects(client) == []
