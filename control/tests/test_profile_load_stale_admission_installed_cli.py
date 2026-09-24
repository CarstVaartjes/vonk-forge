"""The installed CLI refuses a profile load when its reviewed plan goes stale."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_control.models import (
    AgentOperation,
    FleetProfileApplication,
    Job,
    RecipeInstallation,
    RecipeRun,
    ResourceReservation,
)

pytest_plugins = ("tests.test_profile_load_installed_cli",)

from .test_profile_load_installed_cli import (
    KEY,
    _https_api_peer,
    _process_environment,
    _run_pty,
)
from .test_profile_load_submission import _profile_api

FRESH_KEY = "22222222-2222-4222-8222-222222222222"


def _side_effect_ids(sessions) -> tuple[tuple[str, ...], ...]:
    owned_tables = (
        FleetProfileApplication,
        AgentOperation,
        Job,
        RecipeInstallation,
        RecipeRun,
        ResourceReservation,
    )
    with sessions() as session:
        return tuple(
            tuple(session.scalars(select(model.id).order_by(model.id)))
            for model in owned_tables
        )


@pytest.mark.lane
def test_installed_stale_review_is_shown_and_never_admitted_or_replayed(
    installed_vonkctl: Path,
    postgres_engine,
    monkeypatch,
    tmp_path: Path,
) -> None:
    sessions, api, _codec, headers, _initial_preview = _profile_api(postgres_engine)
    original_request = api.request
    edit_status: int | None = None

    def edit_before_load(method, path, **kwargs):
        nonlocal edit_status
        if method == "POST" and path == "/api/profile/1/load" and edit_status is None:
            changed = original_request(
                "PUT",
                "/api/profile/1",
                headers=headers,
                json={"name": "Edited after review", "expected_revision": 1},
            )
            edit_status = changed.status_code
            assert edit_status == 200, changed.text
        return original_request(method, path, **kwargs)

    monkeypatch.setattr(api, "request", edit_before_load)
    with _https_api_peer(tmp_path, api, headers) as (url, certificate, state):
        environment = _process_environment(tmp_path, url, certificate, headers)
        first_review = subprocess.run(
            [
                str(installed_vonkctl),
                "--no-input",
                "--json",
                "--profile",
                "1",
                "profile",
                "load",
                "--dry-run",
            ],
            env=environment,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert first_review.returncode == 0, first_review.stderr
        reviewed = json.loads(first_review.stdout)
        old_digest = reviewed.get("plan_digest")
        assert reviewed.get("allowed") is True
        assert isinstance(old_digest, str) and len(old_digest) == 64

        before = _side_effect_ids(sessions)
        stale_status, stale_stdout, stale_stderr = _run_pty(
            installed_vonkctl,
            (
                "--profile",
                "1",
                "profile",
                "load",
                "--expected-plan",
                old_digest,
                "--request-key",
                KEY,
                "--detach",
            ),
            environment,
            tmp_path,
            answer="yes",
        )
        assert edit_status == 200
        assert stale_status == 2
        assert not stale_stdout
        assert "stale" in stale_stderr.casefold()
        assert "review" in stale_stderr.casefold()
        assert "Next: vonkctl --profile 1 profile load --dry-run" in stale_stderr
        assert (
            f"Next: vonkctl --profile 1 profile progress --request-key {KEY}"
            not in stale_stderr
        )
        assert _side_effect_ids(sessions) == before
        assert [(method, path) for method, path, _ in state.calls] == [
            ("POST", "/api/profile/1/preview"),
            ("POST", "/api/profile/1/preview"),
            ("POST", "/api/profile/1/load"),
            ("POST", "/api/profile/1/preview"),
        ]

        refreshed = api.post("/api/profile/1/preview", headers=headers)
        assert refreshed.status_code == 200, refreshed.text
        fresh_digest = refreshed.json().get("plan_digest")
        assert isinstance(fresh_digest, str) and fresh_digest != old_digest
        assert fresh_digest in stale_stderr
        assert "Edited after review" in stale_stderr

        fresh_review = subprocess.run(
            [
                str(installed_vonkctl),
                "--no-input",
                "--json",
                "--profile",
                "1",
                "profile",
                "load",
                "--dry-run",
            ],
            env=environment,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert fresh_review.returncode == 0, fresh_review.stderr
        fresh_payload = json.loads(fresh_review.stdout)
        assert fresh_payload.get("plan_digest") == fresh_digest

        accepted = subprocess.run(
            [
                str(installed_vonkctl),
                "--no-input",
                "--json",
                "--profile",
                "1",
                "profile",
                "load",
                "--expected-plan",
                fresh_digest,
                "--yes",
                "--request-key",
                FRESH_KEY,
                "--detach",
            ],
            env=environment,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )

    assert accepted.returncode == 0, accepted.stderr
    assert accepted.stdout.count("\n") == 1
    assert not accepted.stderr
    receipt = json.loads(accepted.stdout)
    assert receipt.get("request_key") == FRESH_KEY
    progress = receipt.get("progress")
    assert isinstance(progress, dict)
    intended_profile = progress.get("intended_profile")
    assert isinstance(intended_profile, dict)
    assert intended_profile.get("reviewed_plan_digest") == fresh_digest
    assert [(method, path) for method, path, _ in state.calls] == [
        ("POST", "/api/profile/1/preview"),
        ("POST", "/api/profile/1/preview"),
        ("POST", "/api/profile/1/load"),
        ("POST", "/api/profile/1/preview"),
        ("POST", "/api/profile/1/preview"),
        ("POST", "/api/profile/1/load"),
    ]
    with sessions() as session:
        applications = list(
            session.scalars(
                select(FleetProfileApplication).order_by(FleetProfileApplication.id)
            )
        )
        assert len(applications) == 1
        assert applications[0].request_key == FRESH_KEY
