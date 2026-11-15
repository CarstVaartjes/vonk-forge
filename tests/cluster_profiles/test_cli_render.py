import sys
from io import BytesIO, TextIOWrapper
from pathlib import Path

from cluster_profiles.cli_render import progress_line
from cluster_profiles.cli_render.common import _text
from cluster_profiles.cli_render.library import _needs_download, _qualification_status
from cluster_profiles.cli_states_generated import QualificationStatus


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
