import os
from contextlib import redirect_stdout
from io import BytesIO, TextIOWrapper

import pytest

from cluster_profiles.cli_render import progress_line, render_payload


@pytest.mark.parametrize("width", [60, 80, 120])
def test_library_keeps_canonical_cache_evidence_and_exact_selection(
    width, monkeypatch, capsys
):
    monkeypatch.setattr(
        "cluster_profiles.cli_render.shutil.get_terminal_size",
        lambda fallback: os.terminal_size((width, 24)),
    )
    selector = "publisher/模型-" + "a" * 150
    render_payload(
        {
            "models": [
                {
                    "selector": selector,
                    "identity": {"slug": "模型", "publisher": "publisher"},
                    "family": "Model",
                    "variant": "small",
                    "usage": ["code"],
                    "local": {
                        "controller": "preparing",
                        "running_on": [],
                        "preparation": {
                            "state": "running",
                            "phase": "download",
                            "completed_bytes": 0,
                            "total_bytes": None,
                        },
                    },
                    "resources": {"disk_bytes": 0, "memory_bytes": None},
                }
            ],
            "next_cursor": "cursor-on-page-two",
            "generated_at": "2026-09-22T12:00:00Z",
        },
        "model",
        action="library",
    )
    output = capsys.readouterr().out
    assert f"USE {selector}" in output
    assert "preparing" in output and "download" in output
    assert "0 B" in output and "unavailable" in output
    assert "cursor-on-page-two" in output and "more results" in output
    assert "'controller'" not in output and '"controller"' not in output


def test_empty_is_distinct_from_missing_or_malformed_fleet(capsys):
    render_payload({"nodes": []}, "fleet")
    assert "No Sparks" in capsys.readouterr().out
    for payload in ({}, {"nodes": "offline"}, {"nodes": [{}, None]}):
        with pytest.raises(ValueError, match="nodes"):
            render_payload(payload, "fleet")


def test_node_state_and_blocker_survive_narrow_output(monkeypatch, capsys):
    monkeypatch.setattr(
        "cluster_profiles.cli_render.shutil.get_terminal_size",
        lambda fallback: os.terminal_size((60, 24)),
    )
    reason = "Inventory is stale; " + "refresh evidence before placement " * 8
    render_payload(
        {
            "id": "spk_" + "a" * 32,
            "display_name": "Atlas\x1b[2J",
            "connection": {
                "online_state": "offline",
                "offline_reason": "stale",
                "last_seen_at": "2026-09-22T10:00:00Z",
            },
            "inventory": None,
            "telemetry": None,
            "loaded": [],
            "installed": [],
            "warnings": [{"code": "inventory.stale", "detail": reason}],
        },
        "fleet",
        action="detail",
    )
    captured = capsys.readouterr()
    assert "offline" in captured.out and "unavailable" in captured.out
    assert "spk_" + "a" * 32 in captured.out
    assert reason in captured.err
    assert "\x1b" not in captured.out + captured.err


def test_profile_shows_saved_intent_beside_observation(capsys):
    render_payload(
        {
            "number": 3,
            "name": "Coding",
            "revision": 5,
            "status": "changed",
            "loaded_revision": 4,
            "installation_policy": "keep-cached",
            "definition": {
                "assignments": [
                    {
                        "recipe_selector": "exact-recipe",
                        "spark_ids": ["Atlas"],
                        "desired_state": "installed",
                    }
                ]
            },
            "assignments": [
                {
                    "recipe_selector": "exact-recipe",
                    "display_name": "Mia",
                    "spark_ids": ["Atlas"],
                    "observed_state": "running",
                }
            ],
            "warnings": [],
            "next_actions": [],
        },
        "profile",
    )
    output = capsys.readouterr().out
    assert "Desired: installed" in output
    assert "Observed: running" in output
    assert "Revision: 5" in output and "Loaded revision: 4" in output


def test_preview_exposes_idle_scope_full_digest_and_blocking_reason(capsys):
    reason = "Missing exact model archive " + "a" * 64
    render_payload(
        {
            "allowed": False,
            "profile_name": "Coding",
            "plan_digest": "b" * 64,
            "scope": {"node_ids": ["Atlas", "Boreal"], "idle_node_ids": ["Boreal"]},
            "reasons": [
                {"code": "cache_missing", "detail": reason, "severity": "error"}
            ],
            "steps": [
                {
                    "index": 0,
                    "kind": "stop",
                    "label": "Stop previous workload",
                    "node_ids": ["Boreal"],
                }
            ],
            "assignments": [],
            "preparations": [],
            "preparation_decisions": [],
            "assessments": [],
            "admission_decisions": [],
            "effects": {"runs": [], "installations": [], "superseded": []},
            "summary": {"starts": 0, "stops": 1},
        },
        "profile",
        action="preview",
    )
    captured = capsys.readouterr()
    assert "Blocked" in captured.out and "Idle Sparks: Boreal" in captured.out
    assert "b" * 64 in captured.out
    assert "Stop previous workload" in captured.out
    assert reason in captured.err


def test_unknown_transfer_total_does_not_hide_measured_work_or_invent_percent():
    line = progress_line(
        {
            "state": "running",
            "progress": {
                "phase": "transfer",
                "completed_bytes": 0,
                "total_bytes": 100,
                "total_bytes_known": False,
            },
        }
    )
    assert "transfer" in line and "0 B" in line and "total unknown" in line
    assert "%" not in line


@pytest.mark.parametrize(
    "failure",
    [
        {
            "error_code": "image_missing",
            "summary": "Pinned image is unavailable",
            "uncertain": False,
        },
        {
            "error_code": "agent_failed",
            "reason": "Pinned image is unavailable",
            "stage": "prepare",
        },
    ],
)
def test_job_exposes_agent_failure_and_owner_recovery(failure, capsys):
    render_payload(
        {
            "id": "job-1",
            "state": "failed",
            "targets": ["Atlas"],
            "progress": {"completed": 0, "failed": 1, "total": 1},
            "operations": [
                {
                    "id": "operation-1",
                    "node_id": "Atlas",
                    "state": "failed",
                    "failure": failure,
                    "recovery": {
                        "actions": ["inspect"],
                        "explanation": "Inspect the exact image digest before retrying",
                    },
                }
            ],
        },
        "fleet",
        action="progress",
    )
    captured = capsys.readouterr()
    assert "Pinned image is unavailable" in captured.err
    assert "Inspect the exact image digest" in captured.out
    assert "Next: inspect" in captured.out


def test_ascii_terminal_does_not_fail_on_operator_names():
    buffer = BytesIO()
    stream = TextIOWrapper(buffer, encoding="ascii")
    with redirect_stdout(stream):
        render_payload(
            {
                "profiles": [
                    {"number": 1, "name": "模型", "status": "draft", "revision": 1}
                ]
            },
            "profile",
            action="list",
        )
    stream.flush()
    text = buffer.getvalue().decode("ascii")
    assert "\\u6a21\\u578b" in text


def test_recipe_update_progress_keeps_parent_child_failure_and_reconnect(capsys):
    payload = {
        "kind": "recipe.cache.update.v2",
        "action": "update",
        "id": "parent-id",
        "request_id": "original-key",
        "state": "partial",
        "children": [
            {
                "recipe_name": "publisher/first",
                "recipe_revision_id": "exact-revision",
                "state": "failed",
                "failure": {
                    "code": "recipe_image.recipe_unavailable",
                    "detail": "Recipe was withdrawn",
                    "retryable": False,
                },
            },
            {
                "recipe_name": "publisher/second",
                "recipe_revision_id": "second-revision",
                "operation_id": "second-child",
                "state": "succeeded",
            },
        ],
        "progress": {"completed_items": 2, "total_items": 2},
    }
    render_payload(payload, "recipe", action="progress")
    captured = capsys.readouterr()
    output = captured.out
    assert "Recipe was withdrawn" in captured.err
    for evidence in (
        "parent-id",
        "original-key",
        "exact-revision",
        "second-child",
        "partial",
        "vonkctl recipe progress parent-id",
    ):
        assert evidence in output
    render_payload(
        payload | {"state": "succeeded", "children": []}, "recipe", action="update"
    )
    assert "No cached recipes to update" in capsys.readouterr().out
