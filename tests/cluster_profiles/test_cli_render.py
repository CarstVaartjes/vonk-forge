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


def test_fleet_detail_surfaces_invalid_history_without_hiding_online_state(capsys):
    render_payload(
        {
            "id": "spk_" + "a" * 32,
            "display_name": "Atlas",
            "connection": {"online_state": "online"},
            "inventory": None,
            "telemetry": None,
            "loaded": [],
            "installed": [],
            "warnings": [],
            "provenance": {
                "invalid_operation_evidence": [{"document": "payload"}],
                "invalid_operation_evidence_omitted_count": 2,
            },
        },
        "fleet",
        action="detail",
    )
    output = capsys.readouterr()
    assert "Connection: online" in output.out
    assert "3 invalid historical evidence records" in output.err
    assert "--json" in output.err


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


def test_profile_review_shows_unmeasured_run_capacity_as_an_exact_range(capsys):
    run_id = "11111111-1111-4111-8111-111111111111"
    node_id = "spk_" + "a" * 32
    sample_time = "2026-09-24T10:00:00Z"
    sample_digest = "c" * 64
    reason = {
        "code": "run-switch.resource.resident_usage_unknown",
        "detail": (
            "Capacity is unverified by aggregate inventory 2026-09-24T10:00:00+00:00 "
            f"({sample_digest}): 1 exact active run claim may retain 0..7800 bytes, "
            "and the safe upper bound does not fit. Reconcile the exact run claims "
            "and retry against fresh inventory."
        ),
        "severity": "error",
        "scope": "node",
        "node_ids": [node_id],
        "stale": False,
    }
    render_payload(
        {
            "allowed": False,
            "profile_name": "Capacity review",
            "profile_revision": 3,
            "plan_digest": "b" * 64,
            "scope": {"node_ids": [node_id], "idle_node_ids": []},
            "summary": {},
            "assignments": [],
            "steps": [],
            "effects": {"runs": [], "installations": [], "superseded": []},
            "preparation_decisions": [],
            "assessments": [
                {
                    "assignment_id": "assignment-1",
                    "assessment": {
                        "alias": "retained",
                        "fit_current": {
                            "allowed": False,
                            "nodes": [
                                {
                                    "node_id": node_id,
                                    "ports_required": [],
                                    "memory_kind": "unified",
                                    "memory_pool": "shared",
                                    "memory_required_bytes": 60,
                                    "memory_floor_bytes": 5,
                                    "memory_capacity_bytes": 100,
                                    "memory_available_bytes": 70,
                                    "memory_free_after_bytes": None,
                                    "memory_usage_uncertainty": {
                                        "source": "aggregate_inventory_without_run_usage",
                                        "inventory_observed_at": sample_time,
                                        "inventory_evidence_digest": sample_digest,
                                        "residual_ranges": [
                                            {
                                                "run_id": run_id,
                                                "run_generation": 4,
                                                "reservation_kind": "unified-memory",
                                                "maximum_bytes": 7800,
                                            }
                                        ],
                                    },
                                    "blockers": [reason],
                                    "warnings": [],
                                }
                            ],
                        },
                        "fit_after_stop": None,
                        "post_stop_memory_check": None,
                        "blockers": [reason],
                        "warnings": [],
                        "stop_before_prepare": False,
                        "stop_before_transfer": False,
                    },
                }
            ],
            "preparations": [],
            "reasons": [],
        },
        "profile",
        action="preview",
    )
    captured = capsys.readouterr()
    visible = captured.out + captured.err
    for evidence in (
        "Resident usage evidence",
        "Per-run usage is unavailable",
        "aggregate_inventory_without_run_usage",
        sample_time,
        sample_digest,
        run_id,
        "generation 4",
        "not measured",
        "Memory after placement: unavailable",
        "Capacity is unverified",
    ):
        assert evidence in visible
    assert "leaves -" not in visible


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


def test_activity_renders_profile_cancellation_receipt_without_raw_json(capsys):
    application_id = "11111111-1111-4111-8111-111111111111"
    load_key = "22222222-2222-4222-8222-222222222222"
    cancel_key = "33333333-3333-4333-8333-333333333333"
    child_id = "44444444-4444-4444-8444-444444444444"
    render_payload(
        {
            "operations": [
                {
                    "id": application_id,
                    "kind": "fleet-profile.apply",
                    "state": "cancelling",
                    "created_at": "2026-09-24T10:00:00Z",
                    "node_ids": ["spk_" + "a" * 32],
                    "attempt": 1,
                    "owner": {
                        "kind": "fleet-profile-application",
                        "id": application_id,
                        "request_id": load_key,
                    },
                    "cancellation": {
                        "request_key": cancel_key,
                        "actor": "administrator",
                        "requested_at": "2026-09-24T10:01:00Z",
                        "state": "cancelling",
                        "cause": "operator",
                        "completed_effects": [],
                        "pending_effects": [
                            {
                                "effect_id": child_id,
                                "kind": "agent-operation",
                                "label": "Issued agent effect awaiting its receipt",
                                "operation_id": child_id,
                                "outcome": "pending",
                            }
                        ],
                        "cancelled_effects": [
                            {
                                "effect_id": "step:2",
                                "kind": "profile-step",
                                "label": "Not issued profile step 3: Start service",
                                "operation_id": None,
                                "outcome": "not-issued",
                            }
                        ],
                        "owner": "agent-operation-reconciliation",
                        "dependency": child_id,
                        "deadline_at": "2026-09-24T10:10:00Z",
                    },
                }
            ],
            "total": 1,
            "next_cursor": None,
        },
        "fleet",
        action="activity",
    )
    output = capsys.readouterr().out
    for evidence in (
        "cancelling (operator)",
        cancel_key,
        load_key,
        child_id,
        "pending: agent-operation",
        "not-issued: profile-step step:2",
        "Cancellation deadline: 2026-09-24 10:10:00+00:00",
    ):
        assert evidence in output
    assert "'pending_effects'" not in output
    assert '"effect_id"' not in output


def test_activity_omits_unsupplied_profile_cancellation_deadline(capsys):
    render_payload(
        {
            "operations": [
                {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "kind": "fleet-profile.apply",
                    "state": "cancelled",
                    "created_at": "2026-09-24T10:00:00Z",
                    "node_ids": [],
                    "cancellation": {
                        "request_key": "33333333-3333-4333-8333-333333333333",
                        "actor": "administrator",
                        "requested_at": "2026-09-24T10:01:00Z",
                        "state": "cancelled",
                        "cause": "operator",
                        "completed_effects": [],
                        "pending_effects": [],
                        "cancelled_effects": [],
                        "owner": None,
                        "dependency": None,
                        "deadline_at": None,
                    },
                }
            ],
            "total": 1,
            "next_cursor": None,
        },
        "fleet",
        action="activity",
    )
    output = capsys.readouterr().out
    assert "Cancellation: cancelled (operator)" in output
    assert "Cancellation deadline" not in output
