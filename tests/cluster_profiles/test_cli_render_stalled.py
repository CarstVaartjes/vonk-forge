"""vonkctl shows a stalled profile load prominently, in its progress and the platform."""

from __future__ import annotations

from cluster_profiles.cli_render import render_payload

ALARM = {
    "code": "progress.stalled",
    "detail": "No progress across 6 observations since 2026-10-14T02:00:00+00:00: "
    "profile.retry_conflict: recipe start payload observation is unavailable",
    "severity": "error",
    "node_ids": [],
}


def test_profile_progress_names_the_stall_next_to_the_state(capsys) -> None:
    application = {
        "id": "11111111-1111-4111-8111-111111111111",
        "state": "running",
        "blockers": [
            {
                "code": "profile.retry_conflict",
                "detail": "recipe start payload observation is unavailable",
                "severity": "warning",
                "node_ids": [],
            },
            ALARM,
        ],
        "progress": {"completed_steps": 0, "total_steps": 1},
    }
    render_payload(application, "profile", action="progress")
    lines = capsys.readouterr().out.splitlines()
    assert f"STALLED: {ALARM['detail']}" in lines

    render_payload({**application, "blockers": []}, "profile", action="progress")
    assert "STALLED" not in capsys.readouterr().out


def test_platform_lists_stalled_loads(capsys) -> None:
    from cluster_profiles.generated_control.models.platform_observation import (
        PlatformObservation,
    )

    payload = {
        "observed_at": "2026-10-14T03:00:00Z",
        "api": {"source_sha": "a" * 40, "control_contract_sha256": "b" * 64},
        "workers": None,
        "worker_issue": None,
        "stalled_applications": [
            {
                "application_id": "11111111-1111-4111-8111-111111111111",
                "profile_id": "22222222-2222-4222-8222-222222222222",
                "state": "running",
                "detail": "profile.retry_conflict: manifest is unavailable",
                "repeats": 6,
                "first_seen_at": "2026-10-14T02:00:00Z",
            }
        ],
    }
    render_payload(PlatformObservation.from_dict(payload).to_dict(), "platform")
    output = capsys.readouterr().out
    assert "STALLED profile loads" in output
    assert "11111111-1111-4111-8111-111111111111" in output
    assert "manifest is unavailable" in output

    render_payload(
        PlatformObservation.from_dict(
            {**payload, "stalled_applications": []}
        ).to_dict(),
        "platform",
    )
    assert "STALLED" not in capsys.readouterr().out
