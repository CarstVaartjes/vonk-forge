import sys
from io import BytesIO, TextIOWrapper
from pathlib import Path

import pytest

from cluster_profiles.cli_render.activity import (
    activity_continuation,
    unknown_membership,
)
from cluster_profiles.cli_render.artifact_jobs import (
    ResultFiles,
    job_reconnects,
    result_files,
)
from cluster_profiles.cli_render.common import _text
from cluster_profiles.cli_render.dispatch import render_payload
from cluster_profiles.cli_render.fleet import (
    NO_SPARKS,
    RunProblem,
    _fleet_status_line,
    _needs_attention,
    clock_metric,
    run_problem,
)
from cluster_profiles.cli_render.library import (
    _applied_filters,
    _needs_download,
    _qualification_status,
)
from cluster_profiles.cli_render.operations import stalls
from cluster_profiles.cli_render.profile import EndpointListing, endpoint_listing
from cluster_profiles.cli_render.progress import progress_line
from cluster_profiles.cli_render.review import ReviewVerdict, review_verdict
from cluster_profiles.cli_states_generated import GpuClockMetric, QualificationStatus
from cluster_profiles.control_client.errors import ControlMalformedResponse
from cluster_profiles.generated_control.models.gpu_clock_observation import (
    GpuClockObservation,
)
from cluster_profiles.generated_control.models.gpu_clock_unavailable import (
    GpuClockUnavailable,
)
from vonk_agent_protocol.reason_codes.profiles import ProjectionCode


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
    assert "transfer" in line
    assert "0 B" in line
    assert "total unknown" in line
    assert "%" not in line


def test_ascii_terminal_escapes_operator_names_instead_of_failing(monkeypatch):
    # Break caught: a raw name on an ASCII terminal raises UnicodeEncodeError,
    # which the renderer turns into "unavailable" for the whole document.
    monkeypatch.setattr(sys, "stdout", TextIOWrapper(BytesIO(), encoding="ascii"))
    assert _text("模型") == "\\u6a21\\u578b"


def _assessment(*, readiness, cache, fit="ready", reasons=()):
    return {
        "cache": {"state": cache, "reasons": list(reasons)},
        "fleet_fit": {"state": fit, "reasons": []},
        "readiness": {"state": readiness, "reasons": list(reasons)},
    }


def test_a_recipe_that_only_needs_its_download_is_not_called_blocked():
    # Break caught: "blocked" for every recipe on an idle fleet hides that the
    # only step missing is a download.
    missing = [{"code": "library.cache_missing", "detail": "model missing"}]
    other = [{"code": "library.insufficient_nodes", "detail": "needs 2 Sparks"}]
    assert _needs_download(
        _assessment(readiness="blocked", cache="blocked", reasons=missing)
    )
    assert not _needs_download(
        _assessment(readiness="blocked", cache="ready", reasons=other)
    )
    assert not _needs_download(
        _assessment(
            readiness="blocked", cache="blocked", fit="blocked", reasons=missing
        )
    )


def test_recipe_library_reads_any_unreadable_published_status_as_not_yet_verified():
    # Break caught: a missing, unknown or malformed published status breaking
    # the library listing instead of reading as "Not yet verified".
    assert _qualification_status({"status": "Working", "required_sparks": 2}) == (
        QualificationStatus.WORKING
    )
    for unreadable in ({"status": "Something newer", "required_sparks": 2}, "x", None):
        assert _qualification_status(unreadable) == (
            QualificationStatus.NOT_YET_VERIFIED
        )


def test_every_controller_command_has_a_registered_presentation() -> None:
    import argparse
    import re

    import cluster_profiles
    from cluster_profiles.cli_presentations import PRESENTATIONS
    from cluster_profiles.controller_cli.parser import add_controller_commands

    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command")
    add_controller_commands(commands)
    expected = set()
    for noun, command in commands.choices.items():
        expected.add((noun, None))
        for action in command._actions:
            if isinstance(action, argparse._SubParsersAction):
                expected.update((noun, name) for name in action.choices)
    # Handlers rewrite ``<noun>_action`` after parsing (e.g. remove --review);
    # every such assigned action is emitted through PRESENTATIONS too.
    assigned = set()
    for path in Path(cluster_profiles.__file__).parent.rglob("*.py"):
        assigned.update(
            re.findall(r'args\.(\w+)_action = "([\w-]+)"', path.read_text())
        )
    expected.update(assigned)
    assert expected == set(PRESENTATIONS)
    assert all(callable(presenter) for presenter in PRESENTATIONS.values())


def test_gpu_partial_readings_and_typed_failure_survive_fleet_render():
    # Catches discarding temperature when utilisation is unsupported.
    from cluster_profiles.cli_render.fleet import _gpu
    from vonk_agent_protocol.telemetry import GpuUnavailableReason

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


# The renderer's decisions, each tested on the typed value it prints from.

_SPARK = "spk_" + "1" * 32


def test_an_empty_fleet_has_no_sparks_and_a_malformed_one_is_refused():
    # Break caught: a malformed roster read as an empty fleet.
    assert _fleet_status_line({"nodes": []}) == NO_SPARKS
    for malformed in ({}, {"nodes": "offline"}, {"nodes": [{}, None]}):
        with pytest.raises(ControlMalformedResponse):
            _fleet_status_line(malformed)


def test_a_run_problem_is_the_controller_verdict_never_a_client_derivation():
    # Break caught: notes derived from route or rank state the Controller did not flag.
    assert run_problem([(None, None)], route_reason=None) is None
    assert run_problem([(None, "rank-stale")], "x") == RunProblem("rank-stale", "x")
    assert run_problem([("unreadable", None), (None, "rank-stale")], "x") == (
        RunProblem("unreadable")
    )


def test_a_spark_whose_gpu_needs_a_reset_needs_attention():
    # Break caught: a broken GPU shown as an ordinary idle, healthy Spark.
    spark = {
        "display_name": "atlas",
        "connection": {"online_state": "online"},
        "telemetry": {"freshness": "live"},
        "inventory": {"freshness": "fresh"},
        "warnings": [],
    }
    assert not _needs_attention(spark)
    reset = {"code": ProjectionCode.NODE_GPU_REQUIRES_RESET, "detail": "GPU reset"}
    assert _needs_attention(spark | {"warnings": [reset]})


def test_a_gpu_clock_keeps_zero_and_names_a_missing_reading():
    # Break caught: an idle zero hidden as missing, or a driver N/A shown as a
    # plain unknown.
    missing = GpuClockUnavailable(
        metric="sm_mhz",
        reason="not-supported",
        detail="driver returned [N/A]",
    )
    clock = GpuClockObservation(
        graphics_mhz=0, max_graphics_mhz=2400, unavailable=[missing]
    )
    assert clock_metric(clock, clock.graphics_mhz, GpuClockMetric.GRAPHICS) == 0
    assert (
        clock_metric(clock, clock.max_graphics_mhz, GpuClockMetric.MAX_GRAPHICS) == 2400
    )
    assert clock_metric(clock, clock.sm_mhz, GpuClockMetric.SM) == missing
    assert clock_metric(clock, clock.max_sm_mhz, GpuClockMetric.MAX_SM) is None


def test_a_review_is_ready_only_when_allowed_and_waiting_only_for_preparation():
    assert review_verdict(True, None) is ReviewVerdict.READY
    assert review_verdict(False, True) is ReviewVerdict.READY_AFTER_PREPARATION
    assert review_verdict(False, None) is ReviewVerdict.BLOCKED
    assert review_verdict(None, None) is ReviewVerdict.BLOCKED


def test_unreadable_activity_is_unknown_not_empty():
    # Break caught: unreadable membership shown as "0 of 0" or "no activity".
    issue = "Stored observations are unreadable; membership is unknown."
    assert unknown_membership(None, None, issue) == issue
    assert unknown_membership([], 0, None) is None


def test_the_next_activity_page_keeps_every_filter_of_this_one():
    # Break caught: a continuation that drops a filter and pages a different query.
    command = activity_continuation(
        "opaque+token", limit=1, state="running", target=_SPARK, request_id="r"
    )
    assert command is not None
    assert command[3:] == [
        "--limit",
        "1",
        "--cursor",
        "opaque+token",
        "--state",
        "running",
        "--target",
        _SPARK,
        "--request-id",
        "r",
    ]
    for unreadable in ("", 7):
        assert activity_continuation(unreadable) is None


def test_only_the_controller_stall_alarm_marks_an_application_stalled():
    alarm = {"code": "progress.stalled", "detail": "No progress across 6 attempts"}
    retry = {"code": "profile.retry_conflict", "detail": "payload unavailable"}
    assert stalls([retry, alarm]) == ["No progress across 6 attempts"]
    assert stalls([retry]) == []
    assert stalls(None) == []


def test_invalid_endpoint_history_is_unavailable_not_an_empty_profile():
    # Break caught: unreadable history read as "no endpoint assignments".
    invalid = {"detail": "stored document is invalid"}
    assert endpoint_listing(None, None, []) is EndpointListing.NOT_LOADED
    assert endpoint_listing("a", None, []) is EndpointListing.EMPTY
    assert endpoint_listing("a", invalid, None) is EndpointListing.HISTORY_INVALID
    assert endpoint_listing("a", None, [{}]) is EndpointListing.ASSIGNED


def test_an_empty_library_page_names_the_flags_that_filtered_it():
    filters = {"filters": {"cached": True, "usage": ["code"], "search": ""}}
    assert _applied_filters(filters) == ["--cached", "--usage code"]
    assert _applied_filters({"filters": {}}) == []


def test_an_empty_job_result_is_a_result_and_only_live_jobs_reconnect():
    # Break caught: a succeeded job with no files reported as "unavailable".
    assert result_files("succeeded", []) is ResultFiles.EMPTY
    assert result_files("succeeded", [{}]) is ResultFiles.FILES
    assert result_files("running", []) is ResultFiles.UNAVAILABLE
    assert job_reconnects("running", "op")
    assert not job_reconnects("succeeded", "op")
    assert not job_reconnects("running", None)


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

    render_payload(_cpu_spark(low_clock=True), "fleet", action="detail")
    assert capsys.readouterr().out != output

    render_payload(_spark("atlas", 1), "fleet", action="detail")
    assert "GHz" not in capsys.readouterr().out
