from __future__ import annotations

import json
import re
import uuid
from contextlib import redirect_stdout
from io import StringIO

from cluster_profiles import cli
from cluster_profiles.cli_select import SelectorError, select_exact
from cluster_profiles.controller_cli import _operation_progress_line


class FakeClient:
    """Strict in-process adapter for the current operator wire contracts."""

    def __init__(self, responses: dict[tuple[str, str], object]):
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, object] | None, object]] = []

    def request(self, method, path, payload=None, *, extra_headers=None, query=None):
        self._validate_request(method, path, payload, query)
        self.calls.append((method, path, payload, query))
        response = self.responses.get((method, path), {})
        if isinstance(response, list):
            return response.pop(0)
        return response

    def _validate_request(self, method, path, payload, query):
        profile_path = re.fullmatch(r"/api/profile/(\d+)(?:/(preview|load|progress))?", path)
        selector_path = re.fullmatch(
            r"/api/(model|recipe)/[^/]+(?:/(download|remove))?", path
        )
        operation_path = re.fullmatch(r"/api/(model|recipe)/operations/[^/]+", path)
        known_get = {
            "/api/fleet",
            "/api/model",
            "/api/model/library",
            "/api/recipe",
            "/api/recipe/library",
            "/api/profile",
        }
        known = (
            path in known_get
            or profile_path is not None
            or selector_path is not None
            or operation_path is not None
        )
        known = known or re.fullmatch(r"/api/fleet/[^/]+(?:/loginfo)?", path) is not None
        known = known or path in {
            "/api/fleet/enroll",
            "/api/fleet/upgrade",
            "/api/recipe/update",
        }
        known = known or re.fullmatch(r"/api/fleet/[^/]+/(rename|re-enroll|remove)", path) is not None
        if not known:
            raise AssertionError(f"CLI emitted retired or invented route: {method} {path}")

        if method == "GET":
            if path.endswith("/library"):
                assert query is None or set(query) <= {
                    "search", "usage", "family", "version", "quantization",
                    "updated_since", "sort", "limit", "cursor", "model", "all_models",
                }
            elif path.startswith("/api/fleet/") and not path.endswith("/loginfo"):
                assert query is None or set(query) <= {
                    "metrics", "range", "device", "interface", "run", "capabilities", "technical",
                }
            elif path.endswith("/loginfo"):
                assert query is None or set(query) <= {
                    "since", "lines", "recipe", "source", "follow",
                }
            elif selector_path is not None and selector_path.group(2) is None:
                assert query is None or set(query) <= {"technical"}
            elif operation_path is not None:
                assert query is None
            else:
                assert query is None
            return

        if method == "PUT":
            assert profile_path is not None and profile_path.group(2) is None
            assert isinstance(payload, dict)
            assert set(payload) <= {"assignments", "name", "expected_revision"}
            assert isinstance(payload.get("assignments"), list)
            for assignment in payload["assignments"]:
                assert set(assignment) <= {
                    "recipe_selector", "spark_ids", "assignment_name", "model_variant", "desired_state",
                }
                assert isinstance(assignment["recipe_selector"], str)
                assert isinstance(assignment["spark_ids"], list)
                assert "model_content_sha256" not in assignment
            return

        if method != "POST":
            raise AssertionError(f"unexpected method: {method}")
        if path.endswith("/preview"):
            assert profile_path is not None and payload == {}
        elif path.endswith("/load"):
            assert profile_path is not None
            assert set(payload) <= {"request_key", "dry_run"}
            assert "request_key" in payload
            uuid.UUID(payload["request_key"])
        elif selector_path is not None and selector_path.group(2) in {"download", "remove"}:
            assert isinstance(payload, dict)
            assert set(payload) <= {"schema_version", "request_key", "yes", "with_model"}
            assert payload["schema_version"] == 2
            uuid.UUID(payload["request_key"])
            if selector_path.group(1) == "model":
                assert "with_model" not in payload
            if selector_path.group(2) == "download":
                assert "yes" not in payload and "with_model" not in payload
        elif path == "/api/recipe/update":
            assert isinstance(payload, dict)
            assert set(payload) == {"schema_version", "request_key", "selectors", "all"}
            assert payload["schema_version"] == 2
            uuid.UUID(payload["request_key"])
            assert isinstance(payload["selectors"], list)
            assert isinstance(payload["all"], bool)
        elif path == "/api/fleet/upgrade":
            assert set(payload) == {"selectors", "all", "strategy"}
            assert isinstance(payload["selectors"], list)
        elif path == "/api/fleet/enroll":
            assert set(payload) == {"name", "ttl_seconds"}
        elif path.endswith("/rename"):
            assert set(payload) == {"display_name"}
        elif path.endswith("/re-enroll") or path.endswith("/remove"):
            assert payload is None
        else:
            raise AssertionError(f"invented mutation payload route: {path}")


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


def test_download_is_one_step_and_repeated_calls_keep_server_operation_states() -> None:
    client = FakeClient(
        {
            ("POST", "/api/model/qwen/download"): {
                "state": "accepted",
                "action": "Following",
            },
            ("POST", "/api/recipe/qwen-code/download"): {
                "state": "running",
                "action": "Resuming",
            },
        }
    )
    assert run(("model", "download", "qwen", "--json"), client)[1]["action"] == "Following"
    assert run(("model", "download", "qwen", "--json"), client)[0] == 0
    assert run(("recipe", "download", "qwen-code", "--json"), client)[0] == 0
    assert client.calls[0][2] == client.calls[1][2] == {
        "schema_version": 2,
        "request_key": "11111111-1111-4111-8111-111111111111",
    }
    assert client.calls[2][2] == {
        "schema_version": 2,
        "request_key": "11111111-1111-4111-8111-111111111111",
    }


def test_detail_supports_technical_query_and_nested_typed_table_fields() -> None:
    detail = FakeClient(
        {
            ("GET", "/api/model/qwen"): {
                "selector": "qwen",
                "name": "Qwen 3.8",
                "cache": {"state": "Not cached"},
                "technical": {"artifact_set_sha256": "digest"},
            },
            ("GET", "/api/model"): {
                "title": "Models",
                "models": [
                    {
                        "selector": "qwen",
                        "name": "Qwen 3.8",
                        "cache": {"state": "Cached"},
                        "running": {"spark_ids": ["Atlas"]},
                        "artifact": {"size_bytes": 42},
                    }
                ],
            },
        }
    )
    status, payload = run(("model", "detail", "qwen", "--technical", "--json"), detail)
    assert status == 0 and payload["technical"]["artifact_set_sha256"] == "digest"
    assert detail.calls[0][3] == {"technical": True}
    output = StringIO()
    with redirect_stdout(output):
        assert cli.main(("model",), control_client=detail) == 0
    text = output.getvalue()
    assert "Cached" in text and "Atlas" in text and "42" in text


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
            ("GET", "/api/recipe/operations/op-2"): {
                "state": "succeeded",
                "operation_id": "op-2",
            },
        }
    )
    assert run(("model", "remove", "qwen", "--yes", "--json"), client)[0] == 2
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
                        "recipe_selector": "recipe-uuid",
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
            ("GET", "/api/profile/1/progress"): {
                "state": "succeeded",
                "operation_id": "load-1",
            },
        }
    )
    assert run(("profile", "load", "--dry-run", "--json"), client)[1]["state"] == "blocked"
    assert run(("profile", "load", "--json"), client)[1]["state"] == "succeeded"
    assert client.calls[1][2] == {
        "request_key": "11111111-1111-4111-8111-111111111111"
    }


def test_profile_progress_follow_stops_at_current_terminal_state() -> None:
    client = FakeClient(
        {
            ("GET", "/api/profile/1/progress"): [
                {"state": "running", "progress": {"step": 1, "steps": 2}},
                {"state": "succeeded", "progress": {"step": 2, "steps": 2}},
            ]
        }
    )
    status, payload = run(
        ("profile", "progress", "--follow", "--interval-seconds", "0.1", "--json"),
        client,
    )
    assert status == 0 and payload["state"] == "succeeded"
    assert len(client.calls) == 2


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
    for argv in (
        ("model", "download", "qwen", "--force"),
        ("recipe", "download", "qwen-code", "--force"),
    ):
        try:
            parser.parse_args(argv)
        except cli._UsageError:
            pass
        else:
            raise AssertionError(f"redundant refresh flag accepted: {argv}")


def test_explicit_request_key_is_forwarded_for_retry_reconciliation() -> None:
    client = FakeClient({("POST", "/api/model/qwen/download"): {"state": "running"}})
    status, _ = run(
        (
            "model",
            "download",
            "qwen",
            "--request-key",
            "22222222-2222-4222-8222-222222222222",
            "--json",
        ),
        client,
    )
    assert status == 0
    assert client.calls[0][2]["request_key"] == "22222222-2222-4222-8222-222222222222"


def test_profile_revision_conflict_is_reported_without_a_second_write() -> None:
    class ConflictClient(FakeClient):
        def request(self, method, path, payload=None, *, extra_headers=None, query=None):
            self._validate_request(method, path, payload, query)
            self.calls.append((method, path, payload, query))
            if method == "PUT":
                raise ValueError("profile revision conflict")
            return {"number": 1, "revision": 4, "assignments": []}

    client = ConflictClient({})
    status, payload = run(
        ("profile", "add", "qwen-code", "--spark", "Atlas", "--json"), client
    )
    assert status == 2
    assert payload["error"] == "profile revision conflict"
    assert [call[0] for call in client.calls] == ["GET", "PUT"]


def test_ambiguous_mutation_error_is_not_retried_or_fuzzily_resolved() -> None:
    class AmbiguousClient(FakeClient):
        def request(self, method, path, payload=None, *, extra_headers=None, query=None):
            self._validate_request(method, path, payload, query)
            self.calls.append((method, path, payload, query))
            raise ValueError("ambiguous model selector; choose an exact selector")

    client = AmbiguousClient({})
    status, payload = run(("model", "download", "qwen", "--json"), client)
    assert status == 2
    assert "ambiguous model selector" in payload["error"]
    assert len(client.calls) == 1


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


def test_async_mutations_follow_the_noun_operation_until_terminal() -> None:
    client = FakeClient(
        {
            ("POST", "/api/model/qwen/download"): {
                "state": "accepted",
                "operation_id": "download-1",
            },
            ("GET", "/api/model/operations/download-1"): [
                {"state": "running", "operation_id": "download-1"},
                {"state": "succeeded", "operation_id": "download-1"},
            ],
        }
    )
    status, payload = run(
        ("model", "download", "qwen", "--interval-seconds", "0.01", "--json"),
        client,
    )
    assert status == 0 and payload["state"] == "succeeded"
    assert [call[1] for call in client.calls] == [
        "/api/model/qwen/download",
        "/api/model/operations/download-1",
        "/api/model/operations/download-1",
    ]


def test_detach_returns_acceptance_without_observing_operation() -> None:
    client = FakeClient(
        {
            ("POST", "/api/recipe/qwen-code/download"): {
                "state": "accepted",
                "operation_id": "download-2",
            }
        }
    )
    status, payload = run(
        ("recipe", "download", "qwen-code", "--detach", "--json"), client
    )
    assert status == 0 and payload["state"] == "accepted"
    assert [call[1] for call in client.calls] == ["/api/recipe/qwen-code/download"]


def test_watch_repaints_detail_and_stops_on_terminal_snapshot() -> None:
    client = FakeClient(
        {
            ("GET", "/api/model/qwen"): [
                {"state": "running", "phase": "copying"},
                {"state": "succeeded", "phase": "ready"},
            ]
        }
    )
    output = StringIO()
    with redirect_stdout(output):
        status = cli.main(
            (
                "model",
                "detail",
                "qwen",
                "--watch",
                "--interval-seconds",
                "0.01",
            ),
            control_client=client,
        )
    assert status == 0
    assert len(client.calls) == 2
    assert output.getvalue().count("state") >= 2


def test_noninteractive_removals_fail_closed_and_recipe_requires_model_choice() -> None:
    model = FakeClient({})
    status, payload = run(("model", "remove", "qwen", "--json"), model)
    assert status == 2 and "requires --yes" in payload["error"]
    assert model.calls == []

    recipe = FakeClient({})
    status, payload = run(
        ("recipe", "remove", "qwen-code", "--yes", "--json"), recipe
    )
    assert status == 2 and "--with-model or --keep-model" in payload["error"]
    assert recipe.calls == []


def test_terminal_partial_and_failure_states_have_nonzero_exit_codes() -> None:
    partial = FakeClient({("POST", "/api/model/qwen/download"): {"state": "partial"}})
    failed = FakeClient({("POST", "/api/model/qwen/download"): {"state": "failed"}})
    assert run(("model", "download", "qwen", "--json"), partial)[0] == 1
    assert run(("model", "download", "qwen", "--json"), failed)[0] == 2
