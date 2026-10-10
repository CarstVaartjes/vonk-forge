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
        "cluster_profiles.cli_render.common.shutil.get_terminal_size",
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
            "library": {
                "version": "2.0.0",
                "updated_at": "2026-09-20T08:00:00Z",
                "commit": "a" * 40,
            },
        },
        "model",
        action="library",
    )
    output = capsys.readouterr().out
    assert "Recipe library v2.0.0" in output
    assert selector in output
    assert "download" in output and "0 B" in output
    assert "cursor-on-page-two" in output
    assert "'controller'" not in output and '"controller"' not in output


def test_empty_is_distinct_from_missing_or_malformed_fleet(capsys):
    render_payload({"nodes": []}, "fleet")
    assert "No Sparks" in capsys.readouterr().out
    for payload in ({}, {"nodes": "offline"}, {"nodes": [{}, None]}):
        render_payload(payload, "fleet")
        output = capsys.readouterr().out
        assert "unavailable" in output
        assert "No Sparks" not in output
    render_payload({"nodes": []}, "fleet")
    assert "No Sparks" in capsys.readouterr().out


def _spark(name, number, *, online=True, loaded=()):
    return {
        "id": f"spk_{number:032x}",
        "display_name": name,
        "lifecycle": "ready",
        "connection": {
            "online_state": "online" if online else "offline",
            "offline_reason": None if online else "heartbeat missed",
            "last_seen_at": "2026-09-28T10:00:00Z",
        },
        "inventory": {
            "freshness": "fresh",
            "host_memory_total_bytes": 128 << 30,
            "host_memory_free_bytes": 32 << 30,
            "disk_free_bytes": 1 << 40,
        },
        "telemetry": None,
        "loaded": list(loaded),
        "installed": [],
        "warnings": [],
    }


def _rank(rank, *, group="healthy"):
    return {
        "run_id": "run-1",
        "installation_id": "installation-1",
        "title": "GLM two Sparks",
        "alias": "glm-dual",
        "run_state": "running",
        "group_state": group,
        "route_state": "published",
        "expected_rank_count": 2,
        "present_ranks": [0, 1],
        "member_node_ids": [f"spk_{1:032x}", f"spk_{2:032x}"],
        "rank": rank,
        "role": "entrypoint" if rank == 0 else "worker",
        "rank_fresh": True,
    }


def test_fleet_overview_names_workload_members_and_what_needs_attention(capsys):
    healthy = {
        "nodes": [
            _spark("atlas", 1, loaded=[_rank(0)]),
            _spark("boreas", 2, loaded=[_rank(1)]),
        ]
    }
    render_payload(healthy, "fleet")
    output = capsys.readouterr().out
    assert "glm-dual" in output and "75% of 128.0 GiB" in output
    workload = output[output.index("Workloads") :]
    assert "atlas" in workload and "boreas" in workload
    assert "Needs attention" not in output

    degraded = {
        "nodes": [
            _spark("atlas", 1, loaded=[_rank(0, group="degraded")]),
            _spark("boreas", 2, online=False),
        ]
    }
    render_payload(degraded, "fleet")
    output = capsys.readouterr().out
    attention = output[output.index("Needs attention") :]
    assert "boreas" in attention and "heartbeat missed" in attention
    assert "glm-dual" in attention and "degraded" in attention


def test_rename_reports_the_new_name_of_the_renamed_spark(capsys):
    identity = {"id": "spk_" + "a" * 32, "display_name": "Studio", "hostname": "h"}
    render_payload(identity, "fleet", action="rename")
    output = capsys.readouterr().out
    assert "Studio" in output and identity["id"] in output


def test_node_state_and_blocker_survive_narrow_output(monkeypatch, capsys):
    monkeypatch.setattr(
        "cluster_profiles.cli_render.common.shutil.get_terminal_size",
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


def _recipe(selector, *, readiness, cache, fit="ready", reasons=()):
    return {
        "selector": selector,
        "node_count": 1,
        "identity": {"title": selector, "publisher": "p", "slug": "s"},
        "local": {"controller": "not_cached", "running_on": []},
        "resources": {"disk_bytes": 0, "memory_bytes": None},
        "usage": [],
        "assessment": {
            "cache": {"state": cache, "reasons": list(reasons)},
            "fleet_fit": {"state": fit, "reasons": []},
            "readiness": {"state": readiness, "reasons": list(reasons)},
        },
    }


def test_a_recipe_that_only_needs_its_download_is_not_called_blocked(capsys):
    # Break caught: "blocked" for every recipe on an idle fleet hides that the
    # only step missing is a download.
    missing = [{"code": "library.cache_missing", "detail": "model missing"}]
    other = [{"code": "library.insufficient_nodes", "detail": "needs 2 Sparks"}]
    render_payload(
        {
            "recipes": [
                _recipe(
                    "p/download", readiness="blocked", cache="blocked", reasons=missing
                ),
                _recipe("p/too-big", readiness="blocked", cache="ready", reasons=other),
            ],
            "next_cursor": None,
        },
        "recipe",
        action="library",
    )
    rows = {
        line.split()[0]: line
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("p/")
    }
    assert "needs download" in rows["p/download"] and "not cached" in rows["p/download"]
    assert "needs download" not in rows["p/too-big"] and "blocked" in rows["p/too-big"]


def test_an_empty_library_page_names_the_filters_that_emptied_it(capsys):
    render_payload(
        {
            "recipes": [],
            "next_cursor": None,
            "filters": {"cached": True, "usage": ["code"]},
        },
        "recipe",
        action="library",
    )
    output = capsys.readouterr().out
    assert "--cached" in output and "--usage code" in output
    render_payload(
        {"recipes": [], "next_cursor": None, "filters": {}}, "recipe", action="library"
    )
    output = capsys.readouterr().out
    assert "--" not in output.split("Next:")[0]


def test_waiting_operations_show_what_they_wait_for(capsys):
    spark = "spk_" + "1" * 32
    blockers = [
        {
            "code": "run-switch.inventory-unknown",
            "detail": "No authenticated Spark inventory is available.",
            "severity": "error",
            "node_ids": [spark],
        },
        {
            "code": "recipe_image.preparing",
            "detail": "Preparing the model and runtime image (prepare).",
            "severity": "info",
            "node_ids": [],
        },
    ]
    waiting = {
        "state": "queued",
        "blockers": blockers,
        "next_attempt_at": "2026-09-29T12:00:00+00:00",
        "progress": {"phase": "prepare", "completed_bytes": 0},
    }

    render_payload(
        waiting | {"id": "app", "updated_at": "2026-09-29T11:59:00+00:00"},
        "profile",
        action="progress",
    )
    application = capsys.readouterr().out
    render_payload(
        waiting | {"kind": "recipe.image.availability.v2", "id": "op", "children": []},
        "recipe",
        action="progress",
    )
    operation = capsys.readouterr().out
    render_payload(
        {
            "operations": [
                waiting
                | {
                    "id": "a",
                    "kind": "fleet-profile.apply",
                    "created_at": "2026-09-29T11:00:00+00:00",
                    "node_ids": [spark],
                }
            ],
            "total": 1,
            "next_cursor": None,
        },
        "fleet",
        action="activity",
    )
    activity = capsys.readouterr().out

    for output in (application, operation, activity):
        assert spark in output
        assert "2026-09-29 12:00:00+00:00" in output
    assert "%" not in progress_line(waiting)


def test_review_lists_preparation_and_says_nothing_is_blocked(capsys):
    render_payload(
        {
            "allowed": False,
            "waits_for_preparation": True,
            "plan_digest": "a" * 64,
            "scope": {"node_ids": [], "idle_node_ids": []},
            "summary": {},
            "assignments": [],
            "steps": [{"index": 0, "label": "Switch profile Fresh", "node_ids": []}],
            "preparation_steps": [
                {
                    "index": 0,
                    "label": "Download the model files for Qwen",
                    "node_ids": [],
                },
                {
                    "index": 1,
                    "label": "Build the runtime image for Qwen",
                    "node_ids": [],
                },
            ],
            "effects": {"runs": [], "installations": [], "superseded": []},
            "preparation_decisions": [],
            "assessments": [],
            "preparations": [],
            "reasons": [],
        },
        "profile",
        action="preview",
    )
    output = capsys.readouterr().out
    assert "Ready after preparation" in output
    assert "Blocked" not in output
    assert "Qwen" in output


def _cpu_spark(*, low_clock):
    spark = _spark("atlas", 1)
    spark["telemetry"] = {
        "freshness": "live",
        "sample": {
            "cpu_frequency_avg_mhz": 2100,
            "cpu_frequency_max_mhz": 3900,
            "gpu_temperature_c": 84,
        },
    }
    if low_clock:
        spark["warnings"] = [
            {
                "code": "cpu.low-clock",
                "detail": "CPU clock is low",
                "severity": "warning",
            }
        ]
    return spark


def test_fleet_detail_shows_cpu_clock_with_temperature_and_throttle_hint(capsys):
    render_payload(_cpu_spark(low_clock=False), "fleet", action="detail")
    output = capsys.readouterr().out
    assert "2.1 of 3.9 GHz" in output and "84" in output
    assert "throttled?" not in output

    render_payload(_cpu_spark(low_clock=True), "fleet", action="detail")
    assert "throttled?" in capsys.readouterr().out

    render_payload(_spark("atlas", 1), "fleet", action="detail")
    assert "GHz" not in capsys.readouterr().out


def test_fleet_overview_shows_cpu_clock_only_through_attention_and_wide(capsys):
    payload = {"nodes": [_cpu_spark(low_clock=True)]}
    render_payload(payload, "fleet")
    output = capsys.readouterr().out
    assert "GHz" not in output.split("Needs attention")[0]
    assert "CPU clock is low" in output.split("Needs attention")[1]

    render_payload(payload, "fleet", wide=True)
    assert "2.1 of 3.9 GHz" in capsys.readouterr().out


@pytest.mark.parametrize(
    "application",
    [
        # The observed cancelled application and the accepted receipt that
        # `--detach` returns are applications, never the saved profile.
        {
            "id": "11111111-1111-4111-8111-111111111111",
            "state": "cancelled",
            "status_reason": "Cancelled by the operator",
            "cancellation": {"state": "cancelled", "cause": "operator"},
            "progress": {"completed_steps": 1, "total_steps": 3},
        },
        {
            "id": "11111111-1111-4111-8111-111111111111",
            "state": "running",
            "cancellation": {"state": "cancelling", "cause": "operator"},
        },
    ],
)
def test_profile_cancel_renders_the_application(application, capsys):
    render_payload(application, "profile", action="cancel")
    output = capsys.readouterr().out
    assert application["id"] in output
    assert str(application["state"]) in output
    assert str(application["cancellation"]["state"]) in output


def _wifi_spark():
    spark = _spark("atlas", 1)
    spark["inventory"] = {
        "nas_route_interface": "wlP9s9",
        "network_interfaces": [
            {"name": "enP7s7", "kind": "wired", "carrier": False},
            {"name": "wlP9s9", "kind": "wifi", "carrier": True},
        ],
    }
    spark["warnings"] = [
        {
            "code": "network.nas-route-wifi-wired-port-down",
            "detail": "Reaches the NAS over Wi-Fi (wlP9s9, unknown speed link).",
            "severity": "warning",
            "recommendation": "Wired port enP7s7 has no link; connect it.",
        }
    ]
    return spark


def test_fleet_shows_the_wifi_nas_route_warning_and_its_recommendation(capsys):
    render_payload(_wifi_spark(), "fleet", action="detail")
    captured = capsys.readouterr()
    assert "NAS route: wlP9s9 (wifi, link up)" in captured.out
    assert "Wired ports: enP7s7 (no link)" in captured.out
    assert "Reaches the NAS over Wi-Fi" in captured.err
    assert "connect it" in captured.err


def test_fleet_locks_renders_holders_and_open_transactions(capsys):
    render_payload(
        {
            "held": [
                {
                    "node_id": "spk_" + "a" * 32,
                    "namespace": "node",
                    "holder": "run-admission",
                    "state": "idle in transaction",
                    "transaction_age_seconds": 12.34,
                    "query": "SELECT 1",
                }
            ],
            "open_transactions": [
                {
                    "application_name": "vonk-worker",
                    "state": "active",
                    "transaction_age_seconds": 1.0,
                    "query": "UPDATE jobs",
                }
            ],
        },
        "fleet",
        action="locks",
    )
    output = capsys.readouterr().out
    assert "run-admission" in output and "12.3" in output
    assert "Open transactions:" in output and "vonk-worker" in output
    render_payload({"held": [], "open_transactions": []}, "fleet", action="locks")
    assert "No admission locks are held." in capsys.readouterr().out


@pytest.mark.parametrize("action", ["progress", "activity"])
def test_unreadable_operation_membership_is_rendered_as_unknown(action, capsys):
    render_payload(
        {
            "id": "job-1",
            "state": "running",
            "targets": ["Atlas"],
            "operations": None,
            "total": None,
            "progress": None,
            "projection_issue": "Stored observations are unreadable; membership is unknown.",
        },
        "fleet",
        action=action,
    )
    output = capsys.readouterr().out
    assert "membership is unknown" in output
    assert "0 of 0" not in output
    assert "No activity" not in output


def test_every_controller_command_has_a_registered_presentation() -> None:
    import argparse

    from cluster_profiles.cli_presentations import PRESENTATIONS
    from cluster_profiles.controller_cli import add_controller_commands

    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command")
    add_controller_commands(commands)
    expected = set()
    for noun, command in commands.choices.items():
        expected.add((noun, None))
        for action in command._actions:
            if isinstance(action, argparse._SubParsersAction):
                expected.update((noun, name) for name in action.choices)
    assert expected == set(PRESENTATIONS)
    assert all(callable(presenter) for presenter in PRESENTATIONS.values())


@pytest.mark.parametrize("workers_available", [True, False])
def test_platform_presents_current_response_contract(workers_available, capsys):
    from typing import get_args

    from cluster_profiles.generated_control.models.platform_observation import (
        PlatformObservation,
    )
    from cluster_profiles.generated_control.models.platform_observation_worker_issue_type_0 import (
        PlatformObservationWorkerIssueType0,
    )

    payload = {
        "observed_at": "2026-10-08T12:00:00Z",
        "api": {"source_sha": "a" * 40, "control_contract_sha256": "b" * 64},
        "workers": [
            {
                "process_instance_id": "c" * 64,
                "source_sha": "d" * 40,
                "worker_contract_sha256": "e" * 64,
                "loop_sequence": 3,
                "completed_at": "2026-10-08T11:59:59Z",
            }
        ]
        if workers_available
        else None,
        "worker_issue": None
        if workers_available
        else get_args(PlatformObservationWorkerIssueType0)[0],
    }
    contract = PlatformObservation.from_dict(payload)
    render_payload(contract.to_dict(), "platform")
    output = capsys.readouterr().out
    assert contract.api.source_sha in output
    assert contract.api.control_contract_sha256 in output
    if workers_available:
        assert "d" * 40 in output and "e" * 64 in output
    else:
        assert "Workers: unavailable" in output
        assert contract.worker_issue in output


def test_gpu_partial_readings_and_typed_failure_survive_fleet_render():
    # Catches discarding temperature when utilisation is unsupported.
    from vonk_agent_protocol.telemetry import GpuUnavailableReason

    from cluster_profiles.cli_render import _gpu

    assert (
        _gpu(
            {
                "telemetry": {
                    "freshness": "live",
                    "sample": {
                        "gpu_utilization_percent": 0.0,
                        "gpu_temperature_c": 61,
                    },
                }
            }
        )
        == "0%, 61 °C"
    )
    assert (
        _gpu(
            {
                "telemetry": {
                    "freshness": "live",
                    "sample": {
                        "gpu_temperature_c": 61,
                    },
                }
            }
        )
        == "61 °C"
    )
    reason = GpuUnavailableReason.COMMAND_FAILED
    assert str(reason) in _gpu(
        {
            "telemetry": {
                "freshness": "live",
                "sample": {
                    "gpu_unavailable_reason": reason,
                },
            }
        }
    )
