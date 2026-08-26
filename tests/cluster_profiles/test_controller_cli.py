from __future__ import annotations

import json
from contextlib import redirect_stdout
from io import StringIO
from typing import Any

import pytest

from cluster_profiles import cli


class _Client:
    def __init__(
        self,
        responses: dict[tuple[str, str], dict[str, object] | list[dict[str, object]]]
        | None = None,
    ):
        self.responses = {
            key: list(value) if isinstance(value, list) else value
            for key, value in (responses or {}).items()
        }
        self.calls: list[
            tuple[str, str, dict[str, object] | None, dict[str, object] | None]
        ] = []

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        *,
        query: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.calls.append((method, path, payload, query))
        response = self.responses.get((method, path), {"ok": True})
        if isinstance(response, list):
            assert response, f"No fake responses remain for {method} {path}"
            return response.pop(0)
        return response


def _invoke(client: _Client, *argv: str) -> tuple[int, dict[str, Any]]:
    stdout = StringIO()
    with redirect_stdout(stdout):
        result = cli.main(
            argv, control_client=client, request_id_factory=lambda: "request-1"
        )
    return result, json.loads(stdout.getvalue())


def test_public_recipe_list_exposes_and_applies_the_web_catalog_filters() -> None:
    recipes = [
        {
            "title": "Qwen Vision",
            "slug": "qwen-vision",
            "description": "Multimodal chat",
            "model_title": "Qwen",
            "model_publisher": "qwen",
            "model_slug": "vision",
            "source_owner": "Qwen",
            "source_repository": "https://example.test/qwen",
            "runtime_distribution": "vllm-1",
            "precision": "bf16",
            "topology_mode": "single",
            "qualification": "cataloged",
            "execution_readiness": "executable",
            "node_count": 1,
            "expected_download_bytes": 20,
            "capabilities": ["chat", "vision"],
            "tags": ["featured"],
            "local": {"status": "update-available"},
        },
        {
            "title": "Audio model",
            "capabilities": ["audio"],
            "local": {"status": "not-imported"},
        },
    ]
    client = _Client({("GET", "/api/v1/catalog/public-recipes"): {"recipes": recipes}})

    result, payload = _invoke(
        client,
        "--json",
        "library",
        "public",
        "list",
        "--model-type",
        "vision",
        "--capability",
        "chat",
        "--qualification",
        "cataloged",
        "--readiness",
        "executable",
        "--local",
        "update-available",
    )

    assert result == 0
    assert payload["filtered_count"] == 1
    assert [recipe["slug"] for recipe in payload["recipes"]] == ["qwen-vision"]
    assert client.calls == [("GET", "/api/v1/catalog/public-recipes", None, None)]


def test_fleet_list_uses_the_same_health_and_warning_filters_as_the_web_view() -> None:
    client = _Client(
        {
            ("GET", "/api/v1/fleet"): {
                "nodes": [
                    {
                        "id": "spk_live",
                        "display_name": "Live",
                        "hostname": "live",
                        "connection": {"online_state": "online"},
                        "telemetry": {
                            "sample": {"observed_at": "2999-01-01T00:00:00Z"}
                        },
                        "warnings": [],
                    },
                    {
                        "id": "spk_offline",
                        "display_name": "Needs attention",
                        "hostname": "offline",
                        "connection": {"online_state": "offline"},
                        "telemetry": None,
                        "warnings": [{"severity": "warning"}],
                    },
                ]
            }
        }
    )

    result, payload = _invoke(client, "--json", "fleet", "list", "--health", "offline")

    assert result == 0
    assert payload["filtered_count"] == 1
    assert payload["nodes"][0]["id"] == "spk_offline"
    assert payload["nodes"][0]["operational_state"] == "offline"


def test_human_list_output_is_a_readable_table() -> None:
    client = _Client(
        {
            ("GET", "/api/v1/jobs"): {
                "jobs": [
                    {
                        "created_at": "2026-08-26T10:00:00Z",
                        "kind": "install",
                        "state": "running",
                        "id": "job-1",
                    }
                ],
                "total": 1,
                "next_cursor": None,
            }
        }
    )
    stdout = StringIO()

    with redirect_stdout(stdout):
        result = cli.main(("activity", "jobs"), control_client=client)

    assert result == 0
    assert "CREATED" in stdout.getvalue()
    assert "KIND" in stdout.getvalue()
    assert "running" in stdout.getvalue()
    assert "job-1" in stdout.getvalue()
    assert "total: 1" in stdout.getvalue()


def test_library_apply_is_plan_only_until_apply_flag_is_explicit() -> None:
    client = _Client()

    result, payload = _invoke(
        client,
        "--json",
        "library",
        "load",
        "apply",
        "--installation-id",
        "installation-1",
        "--alias",
        "qwen",
        "--plan-digest",
        "a" * 64,
    )

    assert result == 0
    assert payload["mode"] == "plan"
    assert payload["body"]["request_key"] == "request-1"
    assert client.calls == []

    result, payload = _invoke(
        client,
        "--json",
        "library",
        "load",
        "apply",
        "--installation-id",
        "installation-1",
        "--alias",
        "qwen",
        "--plan-digest",
        "a" * 64,
        "--apply",
    )

    assert result == 0
    assert payload == {"ok": True}
    assert client.calls[-1] == (
        "POST",
        "/api/v1/recipes/runs",
        {
            "installation_id": "installation-1",
            "alias": "qwen",
            "plan_digest": "a" * 64,
            "request_key": "request-1",
        },
        None,
    )


def test_activity_and_library_pagination_are_forwarded_to_the_api() -> None:
    client = _Client()

    result, _ = _invoke(
        client,
        "--json",
        "activity",
        "jobs",
        "--cursor",
        "next",
        "--limit",
        "50",
        "--status",
        "running",
        "--target",
        "spk_target",
    )

    assert result == 0
    assert client.calls[-1] == (
        "GET",
        "/api/v1/jobs",
        None,
        {"cursor": "next", "limit": 50, "status": "running", "target": "spk_target"},
    )


def test_telemetry_range_uses_the_web_resolution_and_point_defaults() -> None:
    client = _Client()

    result, _ = _invoke(
        client, "--json", "fleet", "telemetry", "spk_node", "--range", "7d"
    )

    assert result == 0
    query = client.calls[-1][3]
    assert query is not None
    assert query["resolution"] == "fifteen-minute"
    assert query["maximum_points"] == 672
    assert str(query["start"]).endswith("Z")
    assert str(query["end"]).endswith("Z")


def test_fleet_search_and_attention_sort_match_friendly_web_names() -> None:
    client = _Client(
        {
            ("GET", "/api/v1/fleet"): {
                "nodes": [
                    {
                        "id": "spk_" + "1" * 32,
                        "display_name": "spk_" + "1" * 32,
                        "hostname": "spk_" + "1" * 32,
                        "labels": {"spark_name": "studio-2"},
                        "connection": {"online_state": "online"},
                        "telemetry": {
                            "sample": {"observed_at": "2999-01-01T00:00:00Z"}
                        },
                        "warnings": [{"severity": "error", "code": "disk.low"}],
                        "installed": [],
                        "loaded": [],
                    },
                    {
                        "id": "spk_" + "2" * 32,
                        "display_name": "Studio 10",
                        "hostname": "studio-10",
                        "labels": {},
                        "connection": {"online_state": "online"},
                        "telemetry": {
                            "sample": {"observed_at": "2999-01-01T00:00:00Z"}
                        },
                        "warnings": [],
                        "installed": [],
                        "loaded": [],
                    },
                ]
            }
        }
    )

    result, payload = _invoke(client, "--json", "fleet", "list", "--search", "studio")

    assert result == 0
    assert [node["display_name"] for node in payload["nodes"]] == [
        "Studio 2",
        "Studio 10",
    ]


def test_library_search_matches_only_the_fields_exposed_by_the_web_browser() -> None:
    snapshot = {
        "models": [
            {
                "model": {
                    "publisher": "qwen",
                    "slug": "chat",
                    "content_sha256": "a" * 64,
                },
                "recipes": [
                    {
                        "recipe_id": "hidden-technical-id",
                        "title": "Qwen chat",
                        "slug": "qwen-chat",
                        "description": "Text generation",
                        "topology_name": "solo",
                        "capabilities": ["chat"],
                    }
                ],
            }
        ],
        "unlinked_recipes": [],
        "next_cursor": None,
    }
    client = _Client({("GET", "/api/v1/library"): snapshot})

    result, payload = _invoke(
        client, "--json", "library", "list", "--search", "technical-id"
    )

    assert result == 0
    assert payload["models"] == []

    client = _Client({("GET", "/api/v1/library"): snapshot})
    result, payload = _invoke(client, "--json", "library", "list", "--search", "chat")
    assert result == 0
    assert payload["models"][0]["recipes"][0]["recipe_id"] == "hidden-technical-id"


def test_library_all_follows_and_merges_bounded_server_pages() -> None:
    model = {"publisher": "qwen", "slug": "chat", "content_sha256": "a" * 64}
    client = _Client(
        {
            ("GET", "/api/v1/library"): [
                {
                    "models": [{"model": model, "recipes": [{"recipe_id": "r1"}]}],
                    "unlinked_recipes": [],
                    "next_cursor": "page-2",
                },
                {
                    "models": [{"model": model, "recipes": [{"recipe_id": "r2"}]}],
                    "unlinked_recipes": [{"recipe_id": "r3"}],
                    "next_cursor": None,
                },
            ]
        }
    )

    result, payload = _invoke(client, "--json", "library", "list", "--all")

    assert result == 0
    assert payload["loaded_pages"] == 2
    assert [recipe["recipe_id"] for recipe in payload["models"][0]["recipes"]] == [
        "r1",
        "r2",
    ]
    assert payload["unlinked_recipes"][0]["recipe_id"] == "r3"
    assert client.calls[1][3] == {"cursor": "page-2", "limit": 100}


def test_public_facets_and_comparison_expose_the_web_catalog_choices() -> None:
    recipes = [
        {
            "uri": "vonk://catalog/qwen/chat@sha256:" + "a" * 64,
            "title": "Chat",
            "model_publisher": "qwen",
            "model_slug": "chat",
            "model_title": "Qwen Chat",
            "source_owner": "Qwen",
            "source_repository": "https://example.test/qwen",
            "runtime_distribution": "vllm",
            "precision": "bf16",
            "topology_mode": "single",
            "qualification": "cataloged",
            "execution_readiness": "executable",
            "node_count": 1,
            "capabilities": ["chat", "reasoning"],
            "local": {"status": "current"},
        },
        {
            "uri": "vonk://catalog/qwen/image@sha256:" + "b" * 64,
            "title": "Image",
            "model_publisher": "qwen",
            "model_slug": "image",
            "model_title": "Qwen Image",
            "source_owner": "Qwen",
            "source_repository": "https://example.test/qwen-image",
            "runtime_distribution": "diffusers",
            "precision": "fp16",
            "topology_mode": "single",
            "qualification": "candidate",
            "execution_readiness": "integration-required",
            "node_count": 2,
            "capabilities": ["image-generation"],
            "local": {"status": "not-imported"},
        },
    ]
    response = {"repository": "recipes", "commit": "abc", "recipes": recipes}
    client = _Client({("GET", "/api/v1/catalog/public-recipes"): response})

    result, payload = _invoke(
        client, "--json", "library", "public", "facets", "--source-owner", "Qwen"
    )

    assert result == 0
    assert payload["matching_count"] == 2
    capability_counts = {
        item["value"]: item["count"] for item in payload["facets"]["capability"]
    }
    assert capability_counts["chat"] == 1
    assert capability_counts["image-generation"] == 1

    client = _Client({("GET", "/api/v1/catalog/public-recipes"): response})
    result, payload = _invoke(
        client,
        "--json",
        "library",
        "public",
        "facets",
        "--model",
        "qwen/chat",
        "--model-type",
        "language",
    )
    assert result == 0
    model_type_counts = {
        item["value"]: item["count"] for item in payload["facets"]["model_type"]
    }
    assert model_type_counts["image"] == 1
    assert [item["value"] for item in payload["facets"]["model"]] == ["qwen/chat"]

    client = _Client({("GET", "/api/v1/catalog/public-recipes"): response})
    result, payload = _invoke(
        client,
        "--json",
        "library",
        "public",
        "compare",
        recipes[1]["uri"],
        recipes[0]["uri"],
    )
    assert result == 0
    assert [recipe["title"] for recipe in payload["recipes"]] == ["Image", "Chat"]


def test_custom_recipe_template_uses_the_authoritative_web_builder_preset() -> None:
    result, payload = _invoke(
        _Client(), "--json", "library", "template", "--preset", "vllm"
    )

    assert result == 0
    assert payload["identity"]["slug"] == "custom-vllm-chat"
    assert payload["execution"]["harness"]["slug"] == "vllm-openai"
    assert payload["runtime"]["distribution"]["slug"] == "vllm-cuda"
    assert (
        payload["topology"]["roles"][0]["resources"]["memory"]["startup_peak_bytes"]
        == 68_719_476_736
    )


def test_templates_and_mutation_plans_do_not_require_controller_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.delenv("VONK_CONTROL_URL", raising=False)
    monkeypatch.delenv("VONK_CONTROL_TOKEN_FILE", raising=False)
    stdout = StringIO()
    with redirect_stdout(stdout):
        result = cli.main(("library", "template", "--preset", "custom", "--json"))
    assert result == 0
    assert json.loads(stdout.getvalue())["identity"]["slug"] == "custom-service"

    document = tmp_path / "recipe.json"
    document.write_text('{"schema_version":1}')
    stdout = StringIO()
    with redirect_stdout(stdout):
        result = cli.main(
            (
                "library",
                "create",
                "--slug",
                "offline-plan",
                "--document",
                str(document),
                "--json",
            )
        )
    assert result == 0
    assert json.loads(stdout.getvalue())["mode"] == "plan"


def test_local_comparison_requires_two_or_three_unique_recipes() -> None:
    client = _Client(
        {
            ("GET", "/api/v1/library/recipes/one"): {
                "recipe_id": "one",
                "title": "One",
            },
            ("GET", "/api/v1/library/recipes/two"): {
                "recipe_id": "two",
                "title": "Two",
            },
        }
    )

    result, payload = _invoke(client, "--json", "library", "compare", "one", "two")
    assert result == 0
    assert payload["compared_count"] == 2

    result, payload = _invoke(_Client(), "--json", "library", "compare", "one")
    assert result == 2
    assert "two or three" in payload["error"]


def test_activity_list_combines_web_sources_filters_and_attention_sort() -> None:
    client = _Client(
        {
            ("GET", "/api/v1/audit"): {
                "events": [
                    {
                        "request_id": "audit-1",
                        "actor": "admin",
                        "action": "catalog.recipe.update",
                        "occurred_at": "2026-08-26T10:00:00Z",
                        "targets": ["recipe-1"],
                    },
                    {
                        "request_id": "audit-2",
                        "actor": "admin",
                        "action": "auth.login.failed",
                        "occurred_at": "2026-08-26T12:00:00Z",
                        "targets": [],
                    },
                ]
            },
            ("GET", "/api/v1/jobs"): {
                "jobs": [
                    {
                        "id": "job-1",
                        "kind": "install",
                        "state": "waiting-for-operator",
                        "created_at": "2026-08-26T09:00:00Z",
                    }
                ],
                "total": 1,
                "next_cursor": None,
            },
            ("GET", "/api/v1/fleet"): {"nodes": []},
            ("GET", "/api/v1/library"): {
                "models": [],
                "unlinked_recipes": [
                    {
                        "recipe_id": "recipe-1",
                        "title": "Qwen Chat",
                        "selected_revision": None,
                        "installations": [],
                        "runs": [],
                    }
                ],
            },
        }
    )

    result, payload = _invoke(
        client, "--json", "activity", "list", "--sort", "attention"
    )

    assert result == 0
    assert payload["loaded_count"] == 3
    assert payload["events"][0]["request_id"] == "job-1"
    assert payload["events"][0]["label"] == "Install · Waiting for operator"
    assert payload["events"][1]["status"] == "unsuccessful"
    assert payload["events"][2]["target_names"] == ["Qwen Chat"]
    assert payload["summary"] == {
        "attention": 1,
        "in_progress": 0,
        "recorded": 1,
        "unknown": 0,
        "unsuccessful": 1,
    }


def test_activity_jobs_all_follows_continuation_cursors() -> None:
    client = _Client(
        {
            ("GET", "/api/v1/jobs"): [
                {"jobs": [{"id": "one"}], "total": 2, "next_cursor": "older"},
                {"jobs": [{"id": "two"}], "total": 2, "next_cursor": None},
            ]
        }
    )

    result, payload = _invoke(client, "--json", "activity", "jobs", "--all")

    assert result == 0
    assert [job["id"] for job in payload["jobs"]] == ["one", "two"]
    assert client.calls[1][3] == {"cursor": "older", "limit": 20}


def test_fleet_enrollments_all_follows_continuation_cursors() -> None:
    client = _Client(
        {
            ("GET", "/api/v1/agents/enrollments"): [
                {
                    "enrollments": [{"id": "one"}],
                    "total": 2,
                    "next_cursor": "older",
                },
                {
                    "enrollments": [{"id": "two"}],
                    "total": 2,
                    "next_cursor": None,
                },
            ]
        }
    )

    result, payload = _invoke(
        client, "--json", "fleet", "enrollments", "--state", "pending", "--all"
    )

    assert result == 0
    assert [entry["id"] for entry in payload["enrollments"]] == ["one", "two"]
    assert client.calls[1][3] == {
        "cursor": "older",
        "limit": 100,
        "state": "pending",
    }


def test_json_file_inputs_reject_duplicate_keys(tmp_path) -> None:
    document = tmp_path / "recipe.json"
    document.write_text('{"schema_version":1,"schema_version":2}')

    result, payload = _invoke(
        _Client(),
        "--json",
        "library",
        "create",
        "--slug",
        "recipe",
        "--document",
        str(document),
    )

    assert result == 2
    assert "duplicate key" in payload["error"]


@pytest.mark.parametrize(
    ("argv", "method", "path"),
    [
        (
            ("fleet", "profile", "spk/node", "--display-name", "Studio", "--apply"),
            "PATCH",
            "/api/v1/nodes/spk%2Fnode/profile",
        ),
        (
            ("fleet", "enroll", "--apply"),
            "POST",
            "/api/v1/agents/enrollments/grants",
        ),
        (
            ("fleet", "re-enroll", "spk_" + "1" * 32, "--apply"),
            "POST",
            "/api/v1/agents/enrollments/grants",
        ),
        (
            ("fleet", "revoke", "spk/node", "--apply"),
            "POST",
            "/api/v1/agents/nodes/spk%2Fnode/revoke",
        ),
        (
            ("library", "public", "preview", "vonk://recipe"),
            "POST",
            "/api/v1/catalog/imports/public/preview",
        ),
        (
            (
                "library",
                "public",
                "import",
                "vonk://recipe",
                "--expected-content-sha256",
                "a" * 64,
                "--apply",
            ),
            "POST",
            "/api/v1/catalog/imports/public",
        ),
        (
            (
                "library",
                "resolve",
                "recipe/id",
                "--expected-revision",
                "1",
                "--apply",
            ),
            "POST",
            "/api/v1/catalog/recipes/recipe%2Fid/resolve",
        ),
        (
            (
                "library",
                "fork",
                "recipe/id",
                "--revision",
                "1",
                "--slug",
                "forked",
                "--apply",
            ),
            "POST",
            "/api/v1/catalog/recipes/recipe%2Fid/fork",
        ),
        (
            (
                "library",
                "map",
                "preview",
                "--recipe-revision-id",
                "revision",
                "--node-id",
                "node",
            ),
            "POST",
            "/api/v1/recipes/mapping-plans/preview",
        ),
        (
            (
                "library",
                "map",
                "apply",
                "--recipe-revision-id",
                "revision",
                "--node-id",
                "node",
                "--placement-digest",
                "digest",
                "--apply",
            ),
            "POST",
            "/api/v1/recipes/mappings",
        ),
        (
            (
                "library",
                "install",
                "preview",
                "--mapping-id",
                "mapping",
                "--recipe-build-id",
                "build",
            ),
            "POST",
            "/api/v1/recipes/install-plans/preview",
        ),
        (
            (
                "library",
                "install",
                "apply",
                "--mapping-id",
                "mapping",
                "--recipe-build-id",
                "build",
                "--plan-digest",
                "digest",
                "--apply",
            ),
            "POST",
            "/api/v1/recipes/installations",
        ),
        (
            (
                "library",
                "load",
                "preview",
                "--installation-id",
                "install",
                "--alias",
                "alias",
            ),
            "POST",
            "/api/v1/recipes/run-plans/preview",
        ),
        (
            ("library", "stop", "preview", "run/id"),
            "POST",
            "/api/v1/recipes/stop-plans/preview",
        ),
        (
            (
                "library",
                "stop",
                "apply",
                "run/id",
                "--plan-digest",
                "digest",
                "--apply",
            ),
            "POST",
            "/api/v1/recipes/runs/run%2Fid/stop",
        ),
        (
            ("library", "uninstall", "preview", "install/id"),
            "POST",
            "/api/v1/recipes/uninstall-plans/preview",
        ),
        (
            (
                "library",
                "uninstall",
                "apply",
                "install/id",
                "--plan-digest",
                "digest",
                "--apply",
            ),
            "POST",
            "/api/v1/recipes/installations/install%2Fid/uninstall",
        ),
        (
            ("library", "operation", "show", "operation/id"),
            "GET",
            "/api/v1/recipes/operations/operation%2Fid",
        ),
        (
            ("library", "operation", "retry", "operation/id", "--apply"),
            "POST",
            "/api/v1/recipes/operations/operation%2Fid/retry",
        ),
        (
            ("library", "run", "run/id"),
            "GET",
            "/api/v1/recipes/runs/run%2Fid",
        ),
        (
            ("activity", "job", "job/id"),
            "GET",
            "/api/v1/jobs/job%2Fid",
        ),
        (
            ("activity", "resume", "job/id", "--apply"),
            "POST",
            "/api/v1/jobs/job%2Fid/resume",
        ),
    ],
)
def test_controller_actions_use_the_web_controller_routes(
    argv: tuple[str, ...], method: str, path: str
) -> None:
    client = _Client()

    result, _payload = _invoke(client, "--json", *argv)

    assert result == 0
    assert client.calls[-1][:2] == (method, path)


def test_custom_recipe_create_and_update_forward_canonical_documents(tmp_path) -> None:
    document = tmp_path / "recipe.json"
    document.write_text('{"schema_version":1,"identity":{"slug":"demo"}}')
    client = _Client()

    result, _payload = _invoke(
        client,
        "--json",
        "library",
        "create",
        "--slug",
        "demo",
        "--document",
        str(document),
        "--apply",
    )
    assert result == 0
    assert client.calls[-1][:3] == (
        "POST",
        "/api/v1/catalog/recipes",
        {
            "slug": "demo",
            "document": {"schema_version": 1, "identity": {"slug": "demo"}},
        },
    )

    result, _payload = _invoke(
        client,
        "--json",
        "library",
        "update",
        "recipe/id",
        "--expected-revision",
        "2",
        "--document",
        str(document),
        "--apply",
    )
    assert result == 0
    assert client.calls[-1][:3] == (
        "PUT",
        "/api/v1/catalog/recipes/recipe%2Fid/draft",
        {
            "expected_revision": 2,
            "document": {"schema_version": 1, "identity": {"slug": "demo"}},
        },
    )


def test_enrollment_grants_distinguish_new_and_replacement_certificates() -> None:
    client = _Client()
    result, _payload = _invoke(client, "--json", "fleet", "enroll", "--apply")
    assert result == 0
    assert client.calls[-1][2] == {"ttl_seconds": 900, "purpose": "new-node"}

    node_id = "spk_" + "a" * 32
    result, _payload = _invoke(
        client, "--json", "fleet", "re-enroll", node_id, "--apply"
    )
    assert result == 0
    assert client.calls[-1][2] == {
        "ttl_seconds": 900,
        "purpose": "re-enroll",
        "node_id": node_id,
    }


@pytest.mark.parametrize(
    "argv",
    [
        ("library", "compare", "one", "two", "three", "four"),
        ("library", "public", "compare", "one"),
        ("fleet", "enroll", "--ttl-seconds", "901"),
        ("fleet", "re-enroll", "spk_NOT_HEX"),
        ("fleet", "profile", "node", "--display-name", "   "),
    ],
)
def test_controller_rejects_out_of_contract_values(argv: tuple[str, ...]) -> None:
    result, payload = _invoke(_Client(), "--json", *argv)

    assert result == 2
    assert payload["error_type"] == "control_api"
