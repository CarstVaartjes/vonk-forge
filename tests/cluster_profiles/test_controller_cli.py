from __future__ import annotations

import json
from contextlib import redirect_stdout
from io import StringIO

from cluster_profiles import cli
from cluster_profiles.cli_select import SelectorError, select_exact
from cluster_profiles.controller_cli import _operation_progress_line


class FakeClient:
    def __init__(self, responses: dict[tuple[str, str], dict[str, object]]):
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, object] | None, object]] = []

    def request(self, method, path, payload=None, *, extra_headers=None, query=None):
        self.calls.append((method, path, payload, query))
        return self.responses.get((method, path), {})


def run(argv: tuple[str, ...], client: FakeClient) -> tuple[int, dict[str, object]]:
    output = StringIO()
    with redirect_stdout(output):
        status = cli.main(
            argv,
            control_client=client,
            request_id_factory=lambda: "11111111-1111-4111-8111-111111111111",
        )
    return status, json.loads(output.getvalue())


def test_parser_exposes_only_current_singular_operator_roots() -> None:
    parser = cli._parser()
    assert set(parser._subparsers._group_actions[0].choices) == {
        "fleet",
        "model",
        "recipe",
        "profile",
    }
    for argv in (
        ("models", "list"),
        ("recipes", "list"),
        ("profiles", "list"),
        ("admin", "fleet"),
        ("fleet", "profile"),
    ):
        try:
            parser.parse_args(argv)
        except cli._UsageError:
            pass
        else:
            raise AssertionError(f"retired command accepted: {argv}")


def test_model_and_recipe_library_use_final_singular_routes_and_facets() -> None:
    model = FakeClient({("GET", "/api/model/library"): {"models": []}})
    status, payload = run(
        ("model", "library", "--usage", "code", "--family", "Qwen", "--json"),
        model,
    )
    assert status == 0 and payload == {"models": []}
    assert model.calls == [
        (
            "GET",
            "/api/model/library",
            None,
            {"usage": ["code"], "family": ["Qwen"], "sort": "updated", "limit": 100},
        )
    ]

    recipe = FakeClient({("GET", "/api/recipe/library"): {"recipes": []}})
    status, _ = run(
        ("recipe", "library", "--all-models", "--model", "vision", "--json"),
        recipe,
    )
    assert status == 0
    assert recipe.calls[0][1] == "/api/recipe/library"
    assert recipe.calls[0][3] == {
        "sort": "updated",
        "limit": 100,
        "model": ["vision"],
        "all_models": True,
    }


def test_download_is_one_step_and_repeat_can_request_refresh() -> None:
    client = FakeClient(
        {
            ("POST", "/api/model/qwen/download"): {
                "state": "running",
                "action": "Following",
            },
            ("POST", "/api/recipe/qwen-code/download"): {
                "state": "running",
                "action": "Re-downloading",
            },
        }
    )
    assert run(("model", "download", "qwen", "--json"), client)[1]["action"] == "Following"
    assert run(("model", "download", "qwen", "--force", "--json"), client)[0] == 0
    assert run(("recipe", "download", "qwen-code", "--force", "--json"), client)[0] == 0
    assert client.calls[0][2]["force_refresh"] is False
    assert client.calls[1][2]["force_refresh"] is True
    assert client.calls[2][2]["force_refresh"] is True


def test_cache_actions_bind_schema_two_request_and_remove_semantics() -> None:
    client = FakeClient(
        {
            ("POST", "/api/model/qwen/remove"): {
                "state": "cancelled",
                "operation_id": "op-1",
            },
            ("POST", "/api/recipe/vision/remove"): {
                "state": "accepted",
                "operation_id": "op-2",
            },
        }
    )
    assert run(("model", "remove", "qwen", "--yes", "--json"), client)[0] == 0
    assert client.calls[0][2] == {
        "schema_version": 2,
        "request_key": "11111111-1111-4111-8111-111111111111",
        "yes": True,
    }
    assert run(
        ("recipe", "remove", "vision", "--with-model", "--yes", "--json"), client
    )[0] == 0
    assert client.calls[1][1] == "/api/recipe/vision/remove"
    assert client.calls[1][2]["with_model"] is True


def test_profile_add_autosaves_whole_fleet_authoring_shape_with_revision() -> None:
    client = FakeClient(
        {
            ("GET", "/api/profile/2"): {
                "number": 2,
                "name": "Coding",
                "revision": 7,
                "assignments": [],
            },
            ("PUT", "/api/profile/2"): {
                "number": 2,
                "revision": 8,
                "warnings": ["incomplete group"],
            },
        }
    )
    status, payload = run(
        (
            "--profile",
            "2",
            "profile",
            "add",
            "recipe-uuid",
            "--spark",
            "Boreal",
            "--spark",
            "Atlas",
            "--json",
        ),
        client,
    )
    assert status == 0
    assert payload["revision"] == 8
    assert client.calls == [
        ("GET", "/api/profile/2", None, None),
        (
            "PUT",
            "/api/profile/2",
            {
                "assignments": [
                    {
                        "recipe_id": "recipe-uuid",
                        "spark_ids": ["Atlas", "Boreal"],
                        "desired_state": "running",
                    }
                ],
                "name": "Coding",
                "expected_revision": 7,
            },
            None,
        ),
    ]


def test_profile_load_preview_is_non_mutating_and_load_is_one_step() -> None:
    client = FakeClient(
        {
            ("POST", "/api/profile/1/preview"): {
                "state": "blocked",
                "blockers": ["offline Spark"],
            },
            ("POST", "/api/profile/1/load"): {
                "state": "accepted",
                "operation_id": "load-1",
            },
        }
    )
    assert run(("profile", "load", "--dry-run", "--json"), client)[1]["state"] == "blocked"
    assert run(("profile", "load", "--json"), client)[1]["state"] == "accepted"
    assert client.calls[1][2] == {
        "request_key": "11111111-1111-4111-8111-111111111111"
    }


def test_progress_does_not_invent_percentage_for_unknown_total() -> None:
    assert _operation_progress_line(
        {"state": "building", "progress": {"step": 4, "steps": 7}}
    ) == "building · step 4/7"
    assert _operation_progress_line(
        {"state": "copying", "progress": {"completed_bytes": 20}}
    ) == "copying · progress unavailable"


def test_selector_requires_exact_unique_match_for_local_selection() -> None:
    rows = [
        {"selector": "qwen-code", "name": "Qwen Code"},
        {"selector": "qwen-chat", "name": "Qwen Chat"},
    ]
    assert select_exact(rows, "QWEN-CODE", noun="recipe")["selector"] == "qwen-code"
    try:
        select_exact(rows, "qwen", noun="recipe")
    except SelectorError as error:
        assert "unknown recipe selector" in str(error)
    else:
        raise AssertionError("partial selector was silently accepted")


def test_removal_flags_are_scoped_to_recipe_model_dependency() -> None:
    parser = cli._parser()
    for argv in (
        ("fleet", "remove", "Atlas", "--with-model"),
        ("model", "remove", "qwen", "--with-model"),
    ):
        try:
            parser.parse_args(argv)
        except cli._UsageError:
            pass
        else:
            raise AssertionError(f"invalid dependency flag accepted: {argv}")
    assert parser.parse_args(
        ("recipe", "remove", "qwen-code", "--keep-model")
    ).keep_model is True


def test_profile_selection_and_progress_use_numbered_current_routes() -> None:
    client = FakeClient(
        {
            ("GET", "/api/profile"): {"profiles": [{"number": 1, "name": "Default"}]},
            ("GET", "/api/profile/3"): {
                "number": 3,
                "state": "running",
                "operation_id": "load-3",
            },
            ("GET", "/api/profile/3/progress"): {
                "state": "completed",
                "operation_id": "load-3",
            },
        }
    )
    assert run(("--profile", "3", "profile", "list", "--json"), client)[0] == 0
    assert run(("--profile", "3", "profile", "progress", "--json"), client)[0] == 0
    assert [call[1] for call in client.calls] == [
        "/api/profile",
        "/api/profile/3/progress",
    ]


def test_fleet_actions_use_readable_selectors_and_avoid_legacy_agent_routes() -> None:
    client = FakeClient(
        {
            ("GET", "/api/fleet/Atlas"): {"display_name": "Atlas", "state": "live"},
            ("POST", "/api/fleet/Atlas/rename"): {"display_name": "Studio"},
            ("GET", "/api/fleet/Atlas/loginfo"): {"logs": []},
        }
    )
    assert run(("fleet", "detail", "Atlas", "--json"), client)[0] == 0
    assert run(("fleet", "rename", "Atlas", "Studio", "--json"), client)[0] == 0
    assert run(
        (
            "fleet",
            "loginfo",
            "Atlas",
            "--since",
            "1h",
            "--source",
            "runtime",
            "--json",
        ),
        client,
    )[0] == 0
    assert [call[1] for call in client.calls] == [
        "/api/fleet/Atlas",
        "/api/fleet/Atlas/rename",
        "/api/fleet/Atlas/loginfo",
    ]
    assert all("/agent" not in call[1] for call in client.calls)


def test_error_output_redacts_request_secrets_and_keeps_json_clean() -> None:
    class FailingClient(FakeClient):
        def request(self, method, path, payload=None, *, extra_headers=None, query=None):
            raise ValueError("authorization: Bearer secret-value")

    status, payload = run(("model", "detail", "qwen", "--json"), FailingClient({}))
    assert status == 2
    assert "secret-value" not in json.dumps(payload)
    assert payload["error_type"] == "control_api"


def test_plain_output_is_adaptive_and_keeps_identity_before_optional_columns() -> None:
    client = FakeClient(
        {
            ("GET", "/api/model"): {
                "title": "Models",
                "models": [
                    {
                        "name": "Qwen 3.8",
                        "selector": "qwen-3.8-nvfp4",
                        "usage": ["code"],
                        "cache_state": "Cached",
                    }
                ],
            }
        }
    )
    output = StringIO()
    with redirect_stdout(output):
        assert cli.main(("model",), control_client=client) == 0
    text = output.getvalue()
    assert "MODEL" in text and "USE qwen-3.8-nvfp4" in text
    assert "Qwen 3.8" in text
