"""The installed CLI resumes profile intent after its preview changes."""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_control.models import FleetProfileApplication

pytest_plugins = ("tests.test_profile_load_installed_cli",)

from .test_profile_load_installed_cli import (
    KEY,
    _https_api_peer,
    _process_environment,
)
from .test_profile_load_submission import _profile_api


@pytest.mark.lane
def test_installed_cli_retries_a_changed_profile_plan_automatically(
    installed_vonkctl: Path,
    postgres_engine,
    monkeypatch,
    tmp_path: Path,
) -> None:
    sessions, api, _codec, headers, _preview = _profile_api(postgres_engine)
    original_request = api.request
    edit_status: int | None = None

    def edit_before_load(method, path, **kwargs):
        nonlocal edit_status
        if method == "POST" and path == "/api/profile/1/load" and edit_status is None:
            changed = original_request(
                "PUT",
                "/api/profile/1",
                headers=headers,
                json={"name": "Edited during load", "expected_revision": 1},
            )
            edit_status = changed.status_code
            assert edit_status == 200, changed.text
        return original_request(method, path, **kwargs)

    monkeypatch.setattr(api, "request", edit_before_load)
    with _https_api_peer(tmp_path, api, headers) as (url, certificate, state):
        environment = _process_environment(tmp_path, url, certificate, headers)
        completed = subprocess.run(
            [
                str(installed_vonkctl),
                "--no-input",
                "--json",
                "--profile",
                "1",
                "profile",
                "load",
                "--yes",
                "--request-key",
                KEY,
                "--detach",
            ],
            env=environment,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )

    assert edit_status == 200
    assert completed.returncode == 0, completed.stderr
    receipt = json.loads(completed.stdout)
    assert receipt.get("request_key") == KEY
    # The Controller applies the latest saved plan, so a mid-load edit no
    # longer forces a refused submission and a client-side retry.
    paths = Counter(path for _method, path, _ in state.calls)
    assert paths["/api/profile/1/load"] >= 1
    with sessions() as session:
        applications = list(session.scalars(select(FleetProfileApplication)))
        assert len(applications) == 1
        assert applications[0].request_key == KEY
