from __future__ import annotations

import hashlib
import json
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from cluster_profiles import cli
from cluster_profiles.control_client import ControlForbidden


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


def test_availability_error_json_uses_shared_failure_fields() -> None:
    error = ControlForbidden(
        403,
        "Hugging Face access is required",
        code="access_required",
        recovery=("open_model_access", "configure_hf_token", "check_access_and_resume"),
        retryable=True,
        retry_time="2026-09-06T13:45:00+00:00",
        retry_after_seconds=30,
        preserved="12 MiB of verified bytes retained",
        required_bytes=100,
        free_bytes=40,
        shortfall_bytes=60,
        log_excerpt="safe provider detail",
    )

    payload = cli._control_error(error)

    assert payload["code"] == "access_required"
    assert payload["detail"] == "Hugging Face access is required"
    assert payload["recovery_actions"] == [
        "open_model_access",
        "configure_hf_token",
        "check_access_and_resume",
    ]
    assert payload["retryable"] is True
    assert payload["retry_time"] == "2026-09-06T13:45:00+00:00"
    assert payload["retry_after_seconds"] == 30
    assert payload["preserved"] == "12 MiB of verified bytes retained"
    assert payload["shortfall_bytes"] == 60


class _StrictTaskClient(_Client):
    """Fixture transport that rejects route, query, and body drift."""

    def request(self, method, path, payload=None, *, extra_headers=None, query=None):
        allowed = {
            ("POST", "/api/v1/fleet-profiles"): {"name", "scope", "assignments"},
            ("POST", "/api/v1/fleet-profiles/profile-1/duplicate"): {"name", "scope", "request_key"},
            ("POST", "/api/v1/fleet-profiles/profile-1/preview"): set(),
            ("POST", "/api/v1/fleet-profiles/profile-1/switch"): {"plan_digest", "request_key"},
            ("POST", "/api/v1/model-cache/eviction-preview"): {"target_bytes"},
            ("POST", "/api/v1/model-cache/evict"): {"target_bytes", "plan_digest", "request_key"},
            ("POST", "/api/v1/model-cache/repair-preview"): {"artifact_set_sha256"},
            ("POST", "/api/v1/model-cache/repair"): {"artifact_set_sha256", "plan_digest", "request_key"},
            ("GET", "/api/v1/operations"): {"limit", "state"},
        }
        if (method, path) not in allowed:
            raise AssertionError(f"unexpected Controller route: {method} {path}")
        if query is not None and set(query) != allowed[(method, path)]:
            raise AssertionError(f"unexpected query for {path}: {query}")
        if method == "POST" and (
            not isinstance(payload, dict) or set(payload) != allowed[(method, path)]
        ):
            raise AssertionError(f"unexpected body for {path}: {payload}")
        self.calls.append((method, path, payload, query))
        self.extra_headers.append(extra_headers)
        if path == "/api/v1/model-cache/eviction-preview":
            return {"schema_version": 2, "plan_digest": "d" * 64}
        if path == "/api/v1/model-cache/evict":
            return {"schema_version": 2, "operation_id": "op-1"}
        if path in {"/api/v1/model-cache/repair-preview", "/api/v1/model-cache/repair"}:
            return {"schema_version": 2, "plan_digest": "d" * 64}
        return {"schema_version": 2, "id": "profile-1", "profiles": []}


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
        client, "--json", "fleet", "telemetry", "history", "spk_node", "--range", "7d"
    )

    assert result == 0
    query = client.calls[-1][3]
    assert query is not None
    assert query["resolution"] == "fifteen-minute"
    assert query["maximum_points"] == 672
    assert str(query["start"]).endswith("Z")
    assert str(query["end"]).endswith("Z")


def test_metrics_current_forwards_server_metric_units_and_provenance() -> None:
    metrics = {
        "schema_version": 2,
        "sample": {
            "metrics": [
                {
                    "key": "gpu.power",
                    "scope": "device",
                    "device_id": "gpu0",
                    "value": 185.5,
                    "unit": "W",
                    "source": "dcgm",
                    "support_status": "supported",
                    "freshness": "fresh",
                }
            ]
        },
    }
    client = _Client({("GET", "/api/v1/nodes/spk_node/telemetry/current"): metrics})

    result, payload = _invoke(
        client, "--json", "fleet", "metrics", "current", "spk_node"
    )

    assert result == 0
    assert payload == metrics
    assert client.calls == [
        ("GET", "/api/v1/nodes/spk_node/telemetry/current", None, None)
    ]


def test_telemetry_subcommands_match_metrics_routes() -> None:
    client = _Client(
        {
            ("GET", "/api/v1/nodes/spk_node/telemetry/current"): {"sample": {}},
            ("GET", "/api/v1/nodes/spk_node/telemetry/capabilities"): {},
            ("GET", "/api/v1/nodes/spk_node/telemetry/workloads"): {},
        }
    )
    assert _invoke(client, "--json", "fleet", "telemetry", "current", "spk_node")[0] == 0
    assert _invoke(client, "--json", "fleet", "telemetry", "capabilities", "spk_node")[0] == 0
    assert _invoke(
        client,
        "--json",
        "fleet",
        "telemetry",
        "workloads",
        "spk_node",
        "--run-id",
        "run-1",
    )[0] == 0
    assert [call[1] for call in client.calls] == [
        "/api/v1/nodes/spk_node/telemetry/current",
        "/api/v1/nodes/spk_node/telemetry/capabilities",
        "/api/v1/nodes/spk_node/telemetry/workloads",
    ]


def test_metrics_history_capabilities_and_workloads_use_exact_routes() -> None:
    client = _Client(
        {
            ("GET", "/api/v1/nodes/spk_node/telemetry"): {
                "samples": [{"schema_version": 2, "metrics": []}],
            },
            ("GET", "/api/v1/nodes/spk_node/telemetry/capabilities"): {
                "schema_version": 2,
                "capabilities": [],
            },
            ("GET", "/api/v1/nodes/spk_node/telemetry/workloads"): {
                "schema_version": 2,
                "workloads": [],
            },
        }
    )

    assert _invoke(
        client,
        "--json",
        "fleet",
        "metrics",
        "history",
        "spk_node",
        "--start",
        "2026-09-05T00:00:00Z",
        "--end",
        "2026-09-05T01:00:00Z",
        "--resolution",
        "raw",
        "--maximum-points",
        "100",
    )[0] == 0
    assert _invoke(client, "--json", "fleet", "metrics", "capabilities", "spk_node")[0] == 0
    assert _invoke(
        client,
        "--json",
        "fleet",
        "metrics",
        "workloads",
        "spk_node",
        "--run-id",
        "run-1",
        "--state",
        "running",
    )[0] == 0

    assert client.calls[0][3] == {
        "start": "2026-09-05T00:00:00Z",
        "end": "2026-09-05T01:00:00Z",
        "resolution": "raw",
        "maximum_points": 100,
    }
    assert client.calls[1] == (
        "GET",
        "/api/v1/nodes/spk_node/telemetry/capabilities",
        None,
        None,
    )
    assert client.calls[2][3] == {"run_id": "run-1", "state": "running"}


def test_metrics_export_writes_the_unchanged_server_projection(tmp_path: Path) -> None:
    response = {
        "samples": [{"schema_version": 2, "metrics": []}],
        "provenance": {"source": "agent"},
    }
    destination = tmp_path / "metrics.json"
    client = _Client({("GET", "/api/v1/nodes/spk_node/telemetry"): response})

    result, payload = _invoke(
        client,
        "--json",
        "fleet",
        "metrics",
        "export",
        "spk_node",
        "--file",
        str(destination),
    )

    assert result == 0
    assert payload == {"node_id": "spk_node", "file": str(destination)}
    assert json.loads(destination.read_text()) == response


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


@pytest.mark.parametrize(
    "argv",
    [
        ("library", "compare", "one", "two", "three", "four"),
        ("cache", "show", "artifact-set-1"),
        ("cache", "repair", "artifact-set-1", "preview"),
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


@pytest.mark.parametrize(
    "argv",
    [
        ("library", "public", "list"),
        ("library", "template"),
        ("library", "create"),
        ("library", "update"),
        ("library", "resolve"),
        ("library", "fork"),
        ("library", "install", "preview"),
        ("library", "map", "preview"),
        ("profiles", "prepare", "preview", "profile-1"),
    ],
)
def test_removed_legacy_command_surface_is_rejected(
    argv: tuple[str, ...],
) -> None:
    client = _Client()

    result, payload = _invoke(client, "--json", *argv)

    assert result == 2
    assert payload["error_type"] == "arguments"
    assert client.calls == []


def test_task_oriented_model_cache_and_profile_commands_use_stable_routes() -> None:
    client = _Client(
        {
            ("GET", "/api/v1/model-cache"): {"schema_version": 2, "entries": []},
            ("POST", "/api/v1/model-cache/download"): {"id": "download-1"},
            ("POST", "/api/v1/model-cache/repair"): {"id": "repair-1"},
            ("GET", "/api/v1/fleet-profiles"): {"profiles": []},
            ("POST", "/api/v1/fleet-profiles"): {"id": "profile-1"},
            ("POST", "/api/v1/fleet-profiles/profile-1/preview"): {"plan_digest": "d" * 64},
            ("POST", "/api/v1/fleet-profiles/profile-1/switch"): {"id": "application-1"},
        }
    )
    stdout = StringIO()
    with redirect_stdout(stdout):
        assert cli.main(("--json", "cache", "list"), control_client=client) == 0
        assert cli.main(
                ("--json", "cache", "download", "apply", "--input", '{"model_version_sha256":"' + "a" * 64 + '"}',
                 "--plan-digest", "d" * 64,
             "--request-key", "11111111-1111-4111-8111-111111111111", "--apply"),
            control_client=client,
        ) == 0
        assert cli.main(
            ("--json", "cache", "repair", "a" * 64, "--plan-digest", "d" * 64,
             "--request-key", "11111111-1111-4111-8111-111111111111", "--apply"),
            control_client=client,
        ) == 0
        assert cli.main(("--json", "profiles", "list"), control_client=client) == 0
        assert cli.main(
            ("--json", "profiles", "create", "--input", '{"name":"Demo","scope":{"node_ids":[]},"assignments":[]}'),
            control_client=client,
        ) == 0
        assert cli.main(("--json", "profiles", "preview", "profile-1"), control_client=client) == 0
        assert cli.main(
            ("--json", "profiles", "switch", "profile-1", "--plan-digest", "d" * 64,
             "--request-key", "11111111-1111-4111-8111-111111111111", "--apply"),
            control_client=client,
        ) == 0
    assert client.calls[0][0:2] == ("GET", "/api/v1/model-cache")
    assert ("POST", "/api/v1/model-cache/download") in [call[0:2] for call in client.calls]
    assert ("POST", "/api/v1/model-cache/repair") in [call[0:2] for call in client.calls]
    assert ("POST", "/api/v1/fleet-profiles/profile-1/switch") in [call[0:2] for call in client.calls]


def test_operations_wait_reobserves_until_terminal_without_cancelling() -> None:
    client = _Client(
        {
            ("GET", "/api/v1/operations/op-1"): [
                {"id": "op-1", "state": "running"},
                {"id": "op-1", "state": "succeeded"},
            ]
        }
    )
    stdout = StringIO()
    with redirect_stdout(stdout):
        result = cli.main(
            ("--json", "operations", "wait", "op-1", "--timeout-seconds", "1", "--interval-seconds", ".01",
            ),
            control_client=client,
        )
    assert result == 0
    assert json.loads(stdout.getvalue())["state"] == "succeeded"
    assert len(client.calls) == 2
    assert client.calls[0][3] is None


def test_models_show_uses_library_identity_projection() -> None:
    client = _Client(
        {
            ("GET", "/api/v1/library"): {
                "models": [
                    {"model": {"publisher": "acme", "slug": "demo", "content_sha256": "a" * 64}, "recipes": []}
                ],
                "unlinked_recipes": [],
            }
        }
    )
    result, payload = _invoke(client, "--json", "models", "show", "acme/demo")
    assert result == 0
    assert payload["model"]["slug"] == "demo"


def test_models_list_keeps_unlinked_models_in_a_dynamic_canonical_fixture() -> None:
    client = _Client(
        {
            ("GET", "/api/v1/library"): {
                "models": [
                    {
                        "model": {
                            "publisher": "acme",
                            "slug": "unlinked",
                            "content_sha256": "a" * 64,
                        },
                        "recipes": [],
                    },
                    {
                        "model": {
                            "publisher": "acme",
                            "slug": "served",
                            "content_sha256": "b" * 64,
                        },
                        "recipes": [
                            {
                                "recipe_id": "recipe-1",
                                "title": "Served recipe",
                                "capabilities": ["chat"],
                            }
                        ],
                    },
                ],
                "unlinked_recipes": [
                    {
                        "recipe_id": "recipe-2",
                        "title": "Unlinked recipe",
                        "capabilities": [],
                    }
                ],
            }
        }
    )

    result, payload = _invoke(client, "--json", "models", "list")

    assert result == 0
    assert [item["model"]["slug"] for item in payload["models"]] == [
        "unlinked",
        "served",
    ]
    assert payload["models"][0]["recipes"] == []
    assert payload["unlinked_recipes"][0]["recipe_id"] == "recipe-2"


def test_recipes_list_and_show_use_independent_canonical_routes() -> None:
    recipes = {
        "schema_version": 1,
        "recipes": [
            {"recipe_id": "recipe-1", "title": "Chat engine"},
            {"recipe_id": "recipe-2", "title": "Vision engine"},
        ],
        "next_cursor": None,
    }
    client = _Client(
        {
            ("GET", "/api/v1/library/recipes"): recipes,
            ("GET", "/api/v1/library/recipes/recipe%2F1"): {
                "recipe": {"recipe_id": "recipe/1", "title": "Chat engine"}
            },
        }
    )

    result, payload = _invoke(
        client, "--json", "recipes", "list", "--search", "vision"
    )

    assert result == 0
    assert [recipe["recipe_id"] for recipe in payload["recipes"]] == ["recipe-2"]
    assert client.calls[0] == (
        "GET",
        "/api/v1/library/recipes",
        None,
        {"limit": 100},
    )

    result, payload = _invoke(client, "--json", "recipes", "show", "recipe/1")

    assert result == 0
    assert payload["recipe"]["recipe_id"] == "recipe/1"
    assert client.calls[-1][:2] == (
        "GET",
        "/api/v1/library/recipes/recipe%2F1",
    )


def test_models_capability_filter_keeps_model_and_recipe_truth_separate() -> None:
    client = _Client(
        {
            ("GET", "/api/v1/library"): {
                "models": [
                    {
                        "model": {
                            "publisher": "acme",
                            "slug": "vision",
                            "content_sha256": "a" * 64,
                        },
                        "model_capabilities": {
                            "schema_version": 2,
                            "state": "declared",
                            "facts": [{"capability": "vision", "support": "supported"}],
                            "reasons": [],
                            "provenance": {"source": "catalog"},
                        },
                        "recipes": [{"capabilities": ["chat"]}],
                    },
                    {
                        "model": {
                            "publisher": "acme",
                            "slug": "text",
                            "content_sha256": "b" * 64,
                            "capabilities": ["chat"],
                        },
                        "recipes": [{"capabilities": ["vision"]}],
                    },
                ],
                "unlinked_recipes": [],
            }
        }
    )

    result, payload = _invoke(
        client, "--json", "models", "list", "--capability", "vision"
    )
    assert result == 0
    assert [item["model"]["slug"] for item in payload["models"]] == ["vision"]

    result, payload = _invoke(
        client, "--json", "models", "list", "--recipe-capability", "vision"
    )
    assert result == 0
    assert [item["model"]["slug"] for item in payload["models"]] == ["text"]

    result, payload = _invoke(
        client, "--json", "models", "list", "--capability", "chat"
    )
    assert result == 0
    assert payload["models"] == []


@pytest.mark.parametrize(
    "argv",
    [
        ("fleet", "current"),
        ("fleet", "state"),
        ("models", "discover"),
        ("models", "compare", "a", "b"),
        ("cache", "show", "a" * 64),
        ("cache", "update", "a" * 64),
        ("cache", "eviction", "preview", "--target-bytes", "100"),
        ("cache", "eviction", "apply", "--target-bytes", "100", "--plan-digest", "d" * 64,
         "--request-key", "11111111-1111-4111-8111-111111111111", "--apply"),
        ("profiles", "show", "p"),
        ("profiles", "update", "p", "--input", '{"name":"Demo","scope":{"node_ids":[]},"assignments":[]}'),
        ("profiles", "duplicate", "p", "--name", "Copy"),
        ("profiles", "capture-current", "--name", "Current"),
        ("profiles", "delete", "p"),
        ("profiles", "status", "p"),
        ("operations", "show", "o"),
        ("operations", "watch", "o"),
        ("operations", "evidence", "o"),
    ],
)
def test_task_oriented_command_parser_and_dispatch_contract(argv: tuple[str, ...]) -> None:
    response: dict[str, object] = {"models": [], "unlinked_recipes": []}
    if argv[:2] == ("models", "compare"):
        response["models"] = [
            {"model": {"publisher": "a", "slug": "model", "content_sha256": "a" * 64}, "recipes": []},
            {"model": {"publisher": "b", "slug": "model", "content_sha256": "b" * 64}, "recipes": []},
        ]
    client = _Client({("GET", "/api/v1/library"): response})
    result, _payload = _invoke(client, "--json", *argv)
    assert result == 0


@pytest.mark.parametrize(
    ("argv", "method", "path"),
    [
        (("models", "run", "preview", "--input", '{"model_version_sha256":"' + "a" * 64 + '"}'), "POST", "/api/v1/recipes/run-switch-plans/preview"),
        (("models", "run", "apply", "--input", '{}', "--plan-digest", "d" * 64, "--request-key", "11111111-1111-4111-8111-111111111111", "--apply"), "POST", "/api/v1/recipes/run-switches"),
        (("models", "run", "stop", "preview", "run-1"), "POST", "/api/v1/recipes/run-switch-stops/preview"),
        (("models", "run", "stop", "apply", "run-1", "--plan-digest", "d" * 64, "--request-key", "11111111-1111-4111-8111-111111111111", "--apply"), "POST", "/api/v1/recipes/run-switch-stops"),
    ],
)
def test_high_level_run_routes_are_plan_bound(
    argv: tuple[str, ...], method: str, path: str
) -> None:
    client = _Client()
    result, _payload = _invoke(client, "--json", *argv)
    assert result == 0
    assert client.calls[-1][0:2] == (method, path)


@pytest.mark.parametrize(
    "argv",
    [
        ("models", "run", "apply", "--input", '{}', "--plan-digest", "d" * 64,
         "--request-key", "11111111-1111-4111-8111-111111111111"),
        ("models", "run", "stop", "apply", "run-1", "--plan-digest", "d" * 64,
         "--request-key", "11111111-1111-4111-8111-111111111111"),
    ],
)
def test_high_level_apply_without_apply_flag_only_emits_plan(argv: tuple[str, ...]) -> None:
    client = _Client()
    result, payload = _invoke(client, "--json", *argv)
    assert result == 0
    assert payload["mode"] == "plan"
    assert client.calls == []


def test_simple_model_run_previews_then_applies_with_one_request_key() -> None:
    client = _Client(
        {
            ("POST", "/api/v1/recipes/run-switch-plans/preview"): {
                "schema_version": 2,
                "plan_digest": "d" * 64,
            },
            ("POST", "/api/v1/recipes/run-switches"): {
                "schema_version": 2,
                "operation_id": "op-1",
            },
            ("GET", "/api/v1/operations/op-1"): {
                "schema_version": 2,
                "operation_id": "op-1",
                "state": "succeeded",
            },
        }
    )
    request_key = "11111111-1111-4111-8111-111111111111"
    run_input = {
        "model_version_sha256": "a" * 64,
        "recipe_revision_id": "recipe-1",
        "spark_group": {
            "nodes": [
                {
                    "node_id": "spk_11111111111111111111111111111111",
                    "rank": 0,
                    "role": "coordinator",
                    "endpoint_owner": True,
                }
            ]
        },
        "alias": "demo",
        "action": "run",
        "retention": "retain-cached",
        "invocation": {"prompt": "hello"},
    }

    result, payload = _invoke(
        client,
        "--json",
        "models",
        "run",
        "--input",
        json.dumps(run_input),
        "--request-key",
        request_key,
    )

    assert result == 0
    assert payload["request_key"] == request_key
    assert [call[1] for call in client.calls] == [
        "/api/v1/recipes/run-switch-plans/preview",
        "/api/v1/recipes/run-switches",
        "/api/v1/operations/op-1",
    ]
    assert client.calls[1][2] == {**run_input, "plan_digest": "d" * 64, "request_key": request_key}


def test_simple_cache_download_previews_then_applies_exact_artifacts() -> None:
    client = _Client(
        {
            ("POST", "/api/v1/model-cache/download-preview"): {
                "schema_version": 2,
                "plan_digest": "d" * 64,
            },
            ("POST", "/api/v1/model-cache/download"): {
                "schema_version": 2,
                "operation_id": "op-1",
            },
            ("GET", "/api/v1/operations/op-1"): {
                "schema_version": 2,
                "operation_id": "op-1",
                "state": "succeeded",
            },
        }
    )
    request_key = "11111111-1111-4111-8111-111111111111"
    artifact_input = {
        "model_version_sha256": "a" * 64,
        "recipe_revision_id": "recipe-1",
    }

    result, payload = _invoke(
        client,
        "--json",
        "cache",
        "download",
        "--input",
        json.dumps(artifact_input),
        "--request-key",
        request_key,
    )

    assert result == 0
    assert payload["request_key"] == request_key
    assert client.calls[0][2] == artifact_input
    assert client.calls[1][2] == {
        **artifact_input,
        "plan_digest": "d" * 64,
        "request_key": request_key,
    }


def test_models_download_uses_the_same_exact_cache_routes() -> None:
    client = _Client(
        {
            ("POST", "/api/v1/model-cache/download-preview"): {
                "plan_digest": "d" * 64
            },
            ("POST", "/api/v1/model-cache/download"): {"operation_id": "op-1"},
            ("GET", "/api/v1/operations/op-1"): {"state": "succeeded"},
        }
    )
    result, _payload = _invoke(
        client,
        "--json",
        "models",
        "download",
        "--model-version-sha256",
        "a" * 64,
        "--recipe-revision-id",
        "recipe-1",
        "--request-key",
        "11111111-1111-4111-8111-111111111111",
    )

    assert result == 0
    assert [call[1] for call in client.calls] == [
        "/api/v1/model-cache/download-preview",
        "/api/v1/model-cache/download",
        "/api/v1/operations/op-1",
    ]
    assert client.calls[0][2] == {
        "model_version_sha256": "a" * 64,
        "recipe_revision_id": "recipe-1",
    }


def test_simple_profile_switch_previews_then_applies_without_manual_digest() -> None:
    client = _Client(
        {
            ("POST", "/api/v1/fleet-profiles/p/preview"): {
                "schema_version": 2,
                "plan_digest": "d" * 64,
            },
            ("POST", "/api/v1/fleet-profiles/p/switch"): {
                "schema_version": 2,
                "operation_id": "op-1",
            },
            ("GET", "/api/v1/operations/op-1"): {
                "schema_version": 2,
                "operation_id": "op-1",
                "state": "succeeded",
            },
        }
    )
    request_key = "11111111-1111-4111-8111-111111111111"

    result, payload = _invoke(
        client,
        "--json",
        "profiles",
        "switch",
        "p",
        "--request-key",
        request_key,
    )

    assert result == 0
    assert payload["request_key"] == request_key
    assert client.calls[1][2] == {
        "plan_digest": "d" * 64,
        "request_key": request_key,
    }


def test_simple_run_human_mode_reports_changed_operation_progress_to_stderr() -> None:
    client = _Client(
        {
            ("POST", "/api/v1/recipes/run-switch-plans/preview"): {
                "plan_digest": "d" * 64
            },
            ("POST", "/api/v1/recipes/run-switches"): {"operation_id": "op-1"},
            ("GET", "/api/v1/operations/op-1"): [
                {
                    "state": "running",
                    "progress": {
                        "phase": "copying",
                        "completed_bytes": 10,
                        "total_bytes": 100,
                        "total_bytes_known": True,
                        "members": [
                            {
                                "member_id": "spk_1",
                                "phase": "copying",
                                "completed_bytes": 10,
                                "total_bytes": 100,
                                "state": "running",
                            }
                        ],
                    },
                },
                {"state": "succeeded", "progress": {"phase": "running"}},
            ],
        }
    )
    stderr = StringIO()
    with redirect_stderr(stderr):
        result = cli.main(
            (
                "models",
                "run",
                "--input",
                '{"model_version_sha256":"' + "a" * 64 + '"}',
                "--request-key",
                "11111111-1111-4111-8111-111111111111",
            ),
            control_client=client,
        )

    assert result == 0
    assert "phase: copying | bytes: 10/100 | sparks: spk_1" in stderr.getvalue()


def test_model_download_reports_canonical_cache_progress_and_terminal_error() -> None:
    client = _Client(
        {
            ("POST", "/api/v1/model-cache/download-preview"): {
                "plan_digest": "d" * 64
            },
            ("POST", "/api/v1/model-cache/download"): {"operation_id": "op-1"},
            ("GET", "/api/v1/operations/op-1"): [
                {
                    "state": "running",
                    "progress": {
                        "phase": "downloading",
                        "downloaded_bytes": 12,
                        "expected_bytes": 34,
                        "current_artifact_key": "weights.safetensors",
                    },
                },
                {
                    "state": "failed",
                    "last_error": "source unavailable",
                    "progress": {
                        "phase": "failed",
                        "downloaded_bytes": 12,
                        "expected_bytes": 34,
                        "current_artifact_key": "weights.safetensors",
                    },
                },
            ],
        }
    )
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        result = cli.main(
            (
                "models",
                "download",
                "--model-version-sha256",
                "a" * 64,
                "--request-key",
                "11111111-1111-4111-8111-111111111111",
            ),
            control_client=client,
        )

    assert result == 0
    assert "phase: downloading | bytes: 12/34 | artifact: weights.safetensors" in stderr.getvalue()
    assert '"last_error":"source unavailable"' in stdout.getvalue()


def test_forced_model_download_uses_repair_without_changing_exact_identity() -> None:
    artifact_set = "b" * 64
    client = _Client(
        {
            ("POST", "/api/v1/model-cache/download-preview"): {
                "plan_digest": "d" * 64,
                "artifact_set_sha256": artifact_set,
            },
            ("POST", "/api/v1/model-cache/repair-preview"): {
                "plan_digest": "e" * 64,
            },
            ("POST", "/api/v1/model-cache/repair"): {"operation_id": "op-1"},
            ("GET", "/api/v1/operations/op-1"): {"state": "succeeded"},
        }
    )
    result, payload = _invoke(
        client,
        "--json",
        "models",
        "download",
        "--model-version-sha256",
        "a" * 64,
        "--force",
        "--request-key",
        "11111111-1111-4111-8111-111111111111",
    )
    assert result == 0
    assert payload["force"] is True
    assert client.calls[1][2] == {"artifact_set_sha256": artifact_set}
    assert client.calls[2][2] == {"artifact_set_sha256": artifact_set, "plan_digest": "e" * 64, "request_key": "11111111-1111-4111-8111-111111111111"}


def test_recipe_availability_start_and_status_use_the_durable_exact_route() -> None:
    client = _Client(
        {
            ("POST", "/api/v1/library/recipe-image-availability"): {
                "schema_version": 2,
                "id": "op-1",
                "state": "queued",
            },
            ("GET", "/api/v1/library/recipe-image-availability/op-1"): {
                "schema_version": 2,
                "id": "op-1",
                "state": "succeeded",
                "progress": {"phase": "available", "completed_bytes": 9, "total_bytes": 9},
                "result": {"image_digest": "sha256:" + "a" * 64},
            },
        }
    )
    result, payload = _invoke(
        client,
        "--json",
        "recipes",
        "available",
        "start",
        "recipe-revision-1",
        "--force",
    )
    assert result == 0
    assert payload["state"] == "succeeded"
    assert client.calls == [
        (
            "POST",
            "/api/v1/library/recipe-image-availability",
            {"request_key": "request-1", "recipe_revision_id": "recipe-revision-1", "force": True},
            None,
        ),
        ("GET", "/api/v1/library/recipe-image-availability/op-1", None, None),
    ]


def test_recipe_availability_retry_requires_apply_and_returns_json_plan() -> None:
    client = _Client()
    result, payload = _invoke(
        client,
        "--json",
        "recipes",
        "availability",
        "retry",
        "op-1",
    )
    assert result == 0
    assert payload == {
        "apply": False,
        "body": {"request_key": "request-1"},
        "method": "POST",
        "mode": "plan",
        "path": "/api/v1/library/recipe-image-availability/op-1/retry",
    }


def test_model_cache_check_access_and_resume_uses_exact_identity_and_json_plan() -> None:
    client = _Client({
        ("POST", "/api/v1/model-cache/operations/op-1/check-access-and-resume"): {
            "schema_version": 2,
            "id": "op-1",
            "state": "queued",
        }
    })
    artifact_set = "a" * 64
    plan_digest = "b" * 64
    result, payload = _invoke(
        client,
        "--json",
        "library",
        "operation",
        "check-access",
        "op-1",
        "--artifact-set-sha256",
        artifact_set,
        "--plan-digest",
        plan_digest,
        "--request-key",
        "request-1",
        "--apply",
    )
    assert result == 0
    assert payload["state"] == "queued"
    assert client.calls == [(
        "POST",
        "/api/v1/model-cache/operations/op-1/check-access-and-resume",
        {"request_key": "request-1", "artifact_set_sha256": artifact_set, "plan_digest": plan_digest},
        None,
    )]


def test_operation_progress_projects_run_switch_subphase_and_node_identity() -> None:
    from cluster_profiles.controller_cli import _operation_progress_line

    line = _operation_progress_line(
        {
            "progress": {
                "phase": "transfer",
                "subphase": "target-copy",
                "completed_bytes": 16,
                "total_bytes": 32,
                "members": [
                    {
                        "node_id": "spk_0123456789abcdef0123456789abcdef",
                        "state": "running",
                    }
                ],
            }
        }
    )
    assert line == (
        "phase: transfer | subphase: target-copy | bytes: 16/32 | "
        "sparks: spk_0123456789abcdef0123456789abcdef (running)"
    )


def test_uncertain_run_apply_error_preserves_request_key_for_reconciliation() -> None:
    from cluster_profiles.control_client import ControlTransportError

    class _UncertainClient(_Client):
        def request(self, *args: object, **kwargs: object) -> dict[str, object]:
            raise ControlTransportError("connection lost")

    request_key = "11111111-1111-4111-8111-111111111111"
    client = _UncertainClient()
    result, payload = _invoke(
        client,
        "--json",
        "models", "run", "apply", "--input", '{}',
        "--plan-digest", "d" * 64, "--request-key", request_key, "--apply",
    )
    assert result == 2
    assert payload["request_key"] == request_key
    assert payload["reconcile"]["request_key"] == request_key


def test_strict_fixture_rejects_route_query_and_body_drift() -> None:
    client = _StrictTaskClient()
    profile = '{"name":"Demo","scope":{"node_ids":["spk_' + "1" * 32 + '"]},"assignments":[]}'
    request_key = "11111111-1111-4111-8111-111111111111"
    commands = (
        ("profiles", "create", "--input", profile),
        ("profiles", "duplicate", "profile-1", "--name", "Copy", "--input", '{"scope":{"node_ids":[]}}', "--request-key", "11111111-1111-4111-8111-111111111111", "--apply"),
        ("profiles", "preview", "profile-1"),
        ("profiles", "switch", "profile-1", "--plan-digest", "d" * 64, "--request-key", request_key, "--apply"),
        ("cache", "eviction", "preview", "--target-bytes", "100"),
        ("cache", "eviction", "apply", "--target-bytes", "100", "--plan-digest", "d" * 64, "--request-key", request_key, "--apply"),
        ("cache", "repair", "a" * 64, "preview"),
        ("cache", "repair", "a" * 64, "apply", "--plan-digest", "d" * 64, "--request-key", request_key, "--apply"),
        ("operations", "list", "--status", "running"),
    )
    for command in commands:
        result, _payload = _invoke(client, "--json", *command)
        assert result == 0


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
