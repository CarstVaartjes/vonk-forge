"""The installed CLI load applies the latest profile when its review went stale."""

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
def test_installed_stale_review_loads_the_latest_saved_profile(
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
    with _https_api_peer(tmp_path, api, headers) as (url, certificate, _state):
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

        _run_pty(
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
        # The latest request leads: an edit after review does not refuse the
        # load; the Controller applies the current saved profile instead. (How
        # the CLI renders a receipt for a newer plan than it reviewed is owned
        # by the CLI; this test pins the Controller decision.)

        refreshed = api.post("/api/profile/1/preview", headers=headers)
        assert refreshed.status_code == 200, refreshed.text
        fresh_digest = refreshed.json().get("plan_digest")
        assert isinstance(fresh_digest, str) and fresh_digest != old_digest

    with sessions() as session:
        applications = list(session.scalars(select(FleetProfileApplication)))
        assert [application.request_key for application in applications] == [KEY]
        intended = applications[0].progress["intended_profile"]
        assert intended["reviewed_plan_digest"] == fresh_digest
