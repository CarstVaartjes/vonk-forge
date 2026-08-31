from __future__ import annotations

import hashlib
import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
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
        self.uploads: list[tuple[str, Path, str, str, int]] = []
        self.downloads: list[tuple[str, Path, str, str, int, bool]] = []
        self.extra_headers: list[dict[str, str] | None] = []

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        *,
        extra_headers: dict[str, str] | None = None,
        query: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.calls.append((method, path, payload, query))
        self.extra_headers.append(extra_headers)
        response = self.responses.get((method, path), {"ok": True})
        if isinstance(response, list):
            assert response, f"No fake responses remain for {method} {path}"
            return response.pop(0)
        return response

    def upload_file(
        self,
        path: str,
        source: Path,
        *,
        media_type: str,
        expected_sha256: str,
        expected_size: int,
    ) -> dict[str, object]:
        self.uploads.append((path, source, media_type, expected_sha256, expected_size))
        response = self.responses.get(("UPLOAD", path), {"state": "draft"})
        assert not isinstance(response, list)
        return response

    def download_file(
        self,
        path: str,
        destination: Path,
        *,
        media_type: str,
        expected_sha256: str,
        expected_size: int,
        overwrite: bool,
    ) -> dict[str, object]:
        self.downloads.append(
            (
                path,
                destination,
                media_type,
                expected_sha256,
                expected_size,
                overwrite,
            )
        )
        return {
            "destination": str(destination),
            "media_type": media_type,
            "sha256": expected_sha256,
            "size_bytes": expected_size,
        }


def _invoke(client: _Client, *argv: str) -> tuple[int, dict[str, Any]]:
    stdout = StringIO()
    with redirect_stdout(stdout):
        result = cli.main(
            argv, control_client=client, request_id_factory=lambda: "request-1"
        )
    return result, json.loads(stdout.getvalue())


def _artifact_capabilities(*, remaining: int = 16 * 1024**3) -> dict[str, object]:
    maximum = 16 * 1024**3
    return {
        "schema_version": 1,
        "transport": {
            "max_input_files": 32,
            "max_input_file_bytes": 512 * 1024**2,
            "max_input_total_bytes": 1024**3,
            "max_output_files": 32,
            "max_output_file_bytes": 1024**3,
            "max_output_total_bytes": 2 * 1024**3,
            "max_timeout_seconds": 3_600,
            "reserved_input_names": ["manifest.json"],
        },
        "storage": {
            "max_stored_bytes": maximum,
            "used_bytes": maximum - remaining,
            "remaining_bytes": remaining,
        },
    }


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


def test_human_agent_upgrade_detail_separates_diagnosis_from_raw_evidence() -> None:
    expected_binary = "b" * 64
    expected_build = "sha256:" + "c" * 64
    old_binary = "d" * 64
    old_build = "sha256:" + "e" * 64
    client = _Client(
        {
            ("GET", "/api/v1/jobs/upgrade-1"): {
                "id": "upgrade-1",
                "kind": "agent-upgrade",
                "state": "waiting-for-operator",
                "status_reason": "The exact target identity was not proven.",
                "agent_upgrade_diagnostics": {
                    "expected_identity": {
                        "version": "0.1.0~dev.350+g15f9faf7c5bf",
                        "binary_digest": expected_binary,
                        "build_digest": expected_build,
                    },
                    "targets": [
                        {
                            "node_id": "spk_" + "1" * 32,
                            "attempts": 2,
                            "target_proven": False,
                            "observed_identity": {
                                "version": "0.1.0",
                                "binary_digest": old_binary,
                                "build_digest": old_build,
                            },
                            "raw_reason": "agent upgrade request is invalid",
                            "retry_not_before": "2026-08-28T21:27:40+00:00",
                            "retry_queued": False,
                        }
                    ],
                    "legacy_generic_ambiguous": True,
                    "next_action": "Inspect package-helper and dpkg recovery state before resuming.",
                    "operator_summary": "The exact target identity was not proven.",
                },
            }
        }
    )
    stdout = StringIO()

    with redirect_stdout(stdout):
        result = cli.main(("activity", "job", "upgrade-1"), control_client=client)

    output = stdout.getvalue()
    assert result == 0
    assert "expected_release: 0.1.0~dev.350+g15f9faf7c5bf" in output
    assert f"expected_binary_digest: {expected_binary}" in output
    assert "install_attempts: 2" in output
    assert "target_proven: false" in output
    assert f"observed_build_digest: {old_build}" in output
    assert "raw_helper_reason: agent upgrade request is invalid" in output
    assert "retry_not_before: 2026-08-28T21:27:40+00:00" in output
    assert "retry_queued: false" in output
    assert "legacy helper response is ambiguous" in output
    assert "next_action: Inspect package-helper" in output


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


@pytest.mark.parametrize(
    ("argv", "path", "body"),
    [
        (
            (
                "library",
                "build",
                "apply",
                "--recipe-revision-id",
                "revision-1",
                "--builder-node-id",
                "spk_builder",
                "--build-input-sha256",
                "a" * 64,
            ),
            "/api/v1/recipes/builds",
            {
                "recipe_revision_id": "revision-1",
                "builder_node_id": "spk_builder",
                "build_input_sha256": "a" * 64,
                "request_key": "request-1",
            },
        ),
        (
            (
                "library",
                "distribute",
                "apply",
                "--recipe-build-id",
                "build-1",
                "--mapping-id",
                "mapping-1",
                "--mapping-generation",
                "3",
                "--plan-digest",
                "b" * 64,
            ),
            "/api/v1/recipes/image-distributions",
            {
                "recipe_build_id": "build-1",
                "mapping_id": "mapping-1",
                "mapping_generation": 3,
                "plan_digest": "b" * 64,
                "request_key": "request-1",
            },
        ),
    ],
)
def test_recipe_build_and_distribution_apply_are_plan_only_until_confirmed(
    argv: tuple[str, ...], path: str, body: dict[str, object]
) -> None:
    client = _Client()

    result, plan = _invoke(client, "--json", *argv)

    assert result == 0
    assert plan == {
        "mode": "plan",
        "apply": False,
        "method": "POST",
        "path": path,
        "body": body,
    }
    assert client.calls == []

    result, response = _invoke(client, "--json", *argv, "--apply")

    assert result == 0
    assert response == {"ok": True}
    assert client.calls == [("POST", path, body, None)]


@pytest.mark.parametrize(
    ("argv", "path", "body"),
    [
        (
            (
                "library",
                "build",
                "preview",
                "--recipe-revision-id",
                "revision-1",
                "--builder-node-id",
                "spk_builder",
            ),
            "/api/v1/recipes/build-plans/preview",
            {
                "recipe_revision_id": "revision-1",
                "builder_node_id": "spk_builder",
            },
        ),
        (
            (
                "library",
                "distribute",
                "preview",
                "--recipe-build-id",
                "build-1",
                "--mapping-id",
                "mapping-1",
                "--mapping-generation",
                "3",
            ),
            "/api/v1/recipes/image-distribution-plans/preview",
            {
                "recipe_build_id": "build-1",
                "mapping_id": "mapping-1",
                "mapping_generation": 3,
            },
        ),
    ],
)
def test_recipe_build_and_distribution_previews_use_exact_api_inputs(
    argv: tuple[str, ...], path: str, body: dict[str, object]
) -> None:
    client = _Client({("POST", path): {"preview": True}})

    result, response = _invoke(client, "--json", *argv)

    assert result == 0
    assert response == {"preview": True}
    assert client.calls == [("POST", path, body, None)]


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
                "build",
                "preview",
                "--recipe-revision-id",
                "revision",
                "--builder-node-id",
                "node",
            ),
            "POST",
            "/api/v1/recipes/build-plans/preview",
        ),
        (
            (
                "library",
                "build",
                "apply",
                "--recipe-revision-id",
                "revision",
                "--builder-node-id",
                "node",
                "--build-input-sha256",
                "digest",
                "--apply",
            ),
            "POST",
            "/api/v1/recipes/builds",
        ),
        (
            (
                "library",
                "distribute",
                "preview",
                "--recipe-build-id",
                "build",
                "--mapping-id",
                "mapping",
                "--mapping-generation",
                "3",
            ),
            "POST",
            "/api/v1/recipes/image-distribution-plans/preview",
        ),
        (
            (
                "library",
                "distribute",
                "apply",
                "--recipe-build-id",
                "build",
                "--mapping-id",
                "mapping",
                "--mapping-generation",
                "3",
                "--plan-digest",
                "digest",
                "--apply",
            ),
            "POST",
            "/api/v1/recipes/image-distributions",
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


def test_agent_upgrade_cli_previews_and_applies_without_ssh() -> None:
    node_id = "spk_" + "a" * 32
    client = _Client(
        {("POST", "/api/v1/agents/upgrades/preview"): {"plan_digest": "b" * 64}}
    )

    result, preview = _invoke(
        client,
        "--json",
        "fleet",
        "upgrade",
        "preview",
        "--node-id",
        node_id,
    )
    assert result == 0
    assert preview["plan_digest"] == "b" * 64
    assert client.calls[-1][:3] == (
        "POST",
        "/api/v1/agents/upgrades/preview",
        {"strategy": "one-at-a-time", "node_ids": [node_id]},
    )

    result, plan = _invoke(
        client,
        "--json",
        "fleet",
        "upgrade",
        "apply",
        "--node-id",
        node_id,
        "--plan-digest",
        "b" * 64,
    )
    assert result == 0
    assert plan["mode"] == "plan"
    assert client.calls[-1][1] == "/api/v1/agents/upgrades/preview"

    result, _applied = _invoke(
        client,
        "--json",
        "fleet",
        "upgrade",
        "apply",
        "--node-id",
        node_id,
        "--plan-digest",
        "b" * 64,
        "--apply",
    )
    assert result == 0
    assert client.calls[-1][:3] == (
        "POST",
        "/api/v1/agents/upgrades",
        {
            "strategy": "one-at-a-time",
            "node_ids": [node_id],
            "plan_digest": "b" * 64,
        },
    )


def test_spark3542_compatibility_recovery_requires_typed_preview_and_apply() -> None:
    endpoint = "/api/v1/agents/compatibility-recovery/spark3542-a122"
    client = _Client({("GET", f"{endpoint}/preview"): {"plan_digest": "b" * 64}})

    result, preview = _invoke(
        client,
        "--json",
        "fleet",
        "upgrade",
        "recover-spark3542",
        "preview",
    )
    assert result == 0
    assert preview["plan_digest"] == "b" * 64
    assert client.calls[-1][:2] == ("GET", f"{endpoint}/preview")

    result, plan = _invoke(
        client,
        "--json",
        "fleet",
        "upgrade",
        "recover-spark3542",
        "apply",
        "--plan-digest",
        "b" * 64,
        "--confirm",
        "retry-exact-staged-a122-package-on-spark3542",
    )
    assert result == 0
    assert plan["mode"] == "plan"

    result, _applied = _invoke(
        client,
        "--json",
        "fleet",
        "upgrade",
        "recover-spark3542",
        "apply",
        "--plan-digest",
        "b" * 64,
        "--confirm",
        "retry-exact-staged-a122-package-on-spark3542",
        "--apply",
    )
    assert result == 0
    assert client.calls[-1][:3] == (
        "POST",
        endpoint,
        {
            "plan_digest": "b" * 64,
            "confirmation": "retry-exact-staged-a122-package-on-spark3542",
        },
    )


@pytest.mark.parametrize(
    "argv",
    [
        ("library", "compare", "one", "two", "three", "four"),
        ("library", "public", "compare", "one"),
        ("fleet", "enroll", "--ttl-seconds", "901"),
        ("fleet", "re-enroll", "spk_NOT_HEX"),
        ("fleet", "profile", "node", "--display-name", "   "),
        ("fleet", "upgrade", "preview", "--node-id", "spk_NOT_HEX"),
    ],
)
def test_controller_rejects_out_of_contract_values(argv: tuple[str, ...]) -> None:
    result, payload = _invoke(_Client(), "--json", *argv)

    assert result == 2
    assert payload["error_type"] == "control_api"


def test_artifact_job_create_declares_hashed_bounded_local_inputs(
    tmp_path: Path,
) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_bytes(b"a small prompt\n")

    result, plan = _invoke(
        _Client(),
        "--json",
        "library",
        "job",
        "create",
        "run/id",
        "--interface",
        "image-job",
        "--input",
        "prompt",
        "prompt.txt",
        "text/plain",
        str(prompt),
        "--output-media-type",
        "image/png",
    )

    assert result == 0
    assert plan["mode"] == "plan"
    assert plan["steps"] == [
        {
            "method": "POST",
            "path": "/api/v1/recipes/runs/run%2Fid/artifact-jobs",
            "request_key": "request-1",
            "body": {
                "interface": "image-job",
                "parameters": {},
                "inputs": [
                    {
                        "slot": "prompt",
                        "name": "prompt.txt",
                        "media_type": "text/plain",
                        "size_bytes": len(prompt.read_bytes()),
                        "sha256": hashlib.sha256(prompt.read_bytes()).hexdigest(),
                    }
                ],
                "output_limits": {
                    "max_files": 1,
                    "max_file_bytes": 1024**3,
                    "max_total_bytes": 1024**3,
                    "allowed_media_types": ["image/png"],
                },
                "timeout_seconds": 3_600,
            },
        }
    ]


def test_artifact_job_capabilities_surface_storage_headroom() -> None:
    capabilities = _artifact_capabilities(remaining=123_456)
    client = _Client({("GET", "/api/v1/artifact-jobs/capabilities"): capabilities})

    result, payload = _invoke(client, "--json", "library", "job", "capabilities")

    assert result == 0
    assert payload == capabilities
    assert client.calls == [("GET", "/api/v1/artifact-jobs/capabilities", None, None)]


def test_artifact_job_list_finds_resumable_jobs_for_a_run() -> None:
    client = _Client()

    result, payload = _invoke(client, "--json", "library", "job", "list", "run/id")

    assert result == 0
    assert payload == {"ok": True}
    assert client.calls == [
        ("GET", "/api/v1/recipes/runs/run%2Fid/artifact-jobs", None, None)
    ]


def test_artifact_job_activation_uses_the_logical_job_run_route() -> None:
    client = _Client(
        {("POST", "/api/v1/recipes/run-plans/preview"): {"plan_digest": "a" * 64}}
    )

    result, preview = _invoke(
        client,
        "--json",
        "library",
        "job",
        "activate",
        "preview",
        "--installation-id",
        "installation-1",
        "--alias",
        "image-worker",
    )
    assert result == 0
    assert preview["plan_digest"] == "a" * 64
    assert client.calls[-1][:3] == (
        "POST",
        "/api/v1/recipes/run-plans/preview",
        {"installation_id": "installation-1", "alias": "image-worker"},
    )

    result, plan = _invoke(
        client,
        "--json",
        "library",
        "job",
        "activate",
        "apply",
        "--installation-id",
        "installation-1",
        "--alias",
        "image-worker",
        "--plan-digest",
        "a" * 64,
    )
    assert result == 0
    assert plan["mode"] == "plan"
    assert client.calls[-1][1] == "/api/v1/recipes/run-plans/preview"

    result, _response = _invoke(
        client,
        "--json",
        "library",
        "job",
        "activate",
        "apply",
        "--installation-id",
        "installation-1",
        "--alias",
        "image-worker",
        "--plan-digest",
        "a" * 64,
        "--apply",
    )
    assert result == 0
    assert client.calls[-1][:3] == (
        "POST",
        "/api/v1/recipes/job-runs",
        {
            "installation_id": "installation-1",
            "alias": "image-worker",
            "plan_digest": "a" * 64,
            "request_key": "request-1",
        },
    )


def test_artifact_job_launch_runs_the_resumable_steps_in_order(tmp_path: Path) -> None:
    source = tmp_path / "image.png"
    source.write_bytes(b"not really a png")
    job_id = "12345678-1234-4123-8123-123456789abc"
    create_path = "/api/v1/recipes/runs/run-1/artifact-jobs"
    client = _Client(
        {
            ("GET", "/api/v1/artifact-jobs/capabilities"): _artifact_capabilities(),
            ("POST", create_path): {"id": job_id, "state": "draft"},
            ("POST", f"/api/v1/artifact-jobs/{job_id}/finalize"): {
                "id": job_id,
                "state": "ready",
            },
            ("POST", f"/api/v1/artifact-jobs/{job_id}/submit"): {
                "id": job_id,
                "state": "queued",
            },
        }
    )

    result, payload = _invoke(
        client,
        "--json",
        "library",
        "job",
        "launch",
        "run-1",
        "--interface",
        "image-job",
        "--input",
        "source",
        "image.png",
        "image/png",
        str(source),
        "--output-media-type",
        "image/png",
        "--apply",
    )

    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert result == 0
    assert payload["job"]["state"] == "queued"
    assert payload["steps_completed"] == 4
    assert payload["storage_preflight"]["fits_without_server_reuse"] is True
    assert [call[:2] for call in client.calls] == [
        ("GET", "/api/v1/artifact-jobs/capabilities"),
        ("POST", create_path),
        ("POST", f"/api/v1/artifact-jobs/{job_id}/finalize"),
        ("POST", f"/api/v1/artifact-jobs/{job_id}/submit"),
    ]
    assert client.uploads == [
        (
            f"/api/v1/artifact-jobs/{job_id}/inputs/image.png",
            source,
            "image/png",
            digest,
            len(source.read_bytes()),
        )
    ]
    assert client.extra_headers[1] == {"X-Request-ID": "request-1"}


def test_artifact_job_status_result_cancel_and_download_routes(tmp_path: Path) -> None:
    job_id = "12345678-1234-4123-8123-123456789abc"
    digest = hashlib.sha256(b"result").hexdigest()
    base = f"/api/v1/artifact-jobs/{job_id}"
    metadata = {
        "id": job_id,
        "state": "succeeded",
        "output_files": [
            {
                "name": "result.png",
                "media_type": "image/png",
                "size_bytes": 6,
                "sha256": digest,
            }
        ],
    }
    client = _Client(
        {
            ("GET", base): metadata,
            ("GET", f"{base}/result"): [metadata, metadata, metadata],
        }
    )

    assert _invoke(client, "--json", "library", "job", "status", job_id)[0] == 0
    assert _invoke(client, "--json", "library", "job", "result", job_id)[0] == 0
    result, cancel_plan = _invoke(
        client,
        "--json",
        "library",
        "job",
        "cancel",
        job_id,
        "--reason",
        "  operator requested  ",
    )
    assert result == 0
    assert cancel_plan["body"] == {"reason": "operator requested"}

    result, download_plan = _invoke(
        client,
        "--json",
        "library",
        "job",
        "download",
        job_id,
        "--output-directory",
        str(tmp_path),
    )
    assert result == 0
    assert download_plan["mode"] == "plan"
    assert client.downloads == []

    result, downloaded = _invoke(
        client,
        "--json",
        "library",
        "job",
        "download",
        job_id,
        "--output-directory",
        str(tmp_path),
        "--sha256",
        digest,
        "--apply",
    )
    assert result == 0
    assert downloaded["downloads"][0]["sha256"] == digest
    assert client.downloads == [
        (
            f"{base}/results/{digest}",
            tmp_path / "result.png",
            "image/png",
            digest,
            6,
            False,
        )
    ]


def test_artifact_job_rejects_symlink_input_and_unsafe_output_metadata(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.write_text("prompt")
    symlink = tmp_path / "link"
    symlink.symlink_to(source)
    result, payload = _invoke(
        _Client(),
        "--json",
        "library",
        "job",
        "upload",
        "job-1",
        "--input",
        "prompt",
        "prompt.txt",
        "text/plain",
        str(symlink),
    )
    assert result == 2
    assert payload["error_type"] == "control_api"

    client = _Client(
        {
            ("GET", "/api/v1/artifact-jobs/job-1/result"): {
                "output_files": [
                    {
                        "name": "../escape.png",
                        "media_type": "image/png",
                        "size_bytes": 1,
                        "sha256": "a" * 64,
                    }
                ]
            }
        }
    )
    result, payload = _invoke(
        client,
        "--json",
        "library",
        "job",
        "download",
        "job-1",
        "--output-directory",
        str(tmp_path),
    )
    assert result == 2
    assert payload["error_type"] == "control_api"
