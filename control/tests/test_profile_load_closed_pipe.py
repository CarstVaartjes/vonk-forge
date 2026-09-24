"""A closed result pipe cannot cancel accepted profile work."""

from __future__ import annotations

import json
import os
import subprocess
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
def test_closed_stdout_reconnects_to_the_single_accepted_load(
    installed_vonkctl: Path,
    postgres_engine,
    tmp_path: Path,
) -> None:
    sessions, api, _codec, headers, preview = _profile_api(postgres_engine)
    digest = preview.get("plan_digest")
    assert isinstance(digest, str)
    with _https_api_peer(tmp_path, api, headers, drop_load_answer=True) as (
        url,
        certificate,
        state,
    ):
        environment = _process_environment(tmp_path, url, certificate, headers)
        read_fd, write_fd = os.pipe()
        process = subprocess.Popen(
            [
                str(installed_vonkctl),
                "--profile",
                "1",
                "--json",
                "profile",
                "load",
                "--expected-plan",
                digest,
                "--yes",
                "--request-key",
                KEY,
                "--detach",
            ],
            env=environment,
            cwd=tmp_path,
            stdin=subprocess.DEVNULL,
            stdout=write_fd,
            stderr=subprocess.PIPE,
        )
        os.close(read_fd)
        os.close(write_fd)
        assert process.stderr is not None
        stderr = process.stderr.read()
        status = process.wait(timeout=45)
        assert status == 141, stderr.decode(errors="replace")
        assert stderr == b""
        assert state.accepted is not None
        assert state.profile_edit_status == 200

        with sessions() as session:
            applications = list(session.scalars(select(FleetProfileApplication)))
            assert len(applications) == 1
            assert applications[0].request_key == KEY

        lookup = api.get(f"/api/profile/1/requests/{KEY}", headers=headers)
        assert lookup.status_code == 200, lookup.text
        accepted = lookup.json()
        application_id = state.accepted.get("id")
        assert isinstance(application_id, str)
        assert accepted["id"] == application_id
        assert accepted["request_key"] == KEY

        reconnected = subprocess.run(
            [
                str(installed_vonkctl),
                "--profile",
                "1",
                "--json",
                "profile",
                "progress",
                "--request-key",
                KEY,
            ],
            env=environment,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    assert reconnected.returncode == 0, reconnected.stderr
    assert reconnected.stdout.count("\n") == 1
    assert not reconnected.stderr
    receipt = json.loads(reconnected.stdout)
    assert receipt["id"] == application_id
    assert receipt["request_key"] == KEY
    assert [(method, path) for method, path, _ in state.calls] == [
        ("POST", "/api/profile/1/load"),
        ("GET", f"/api/profile/1/requests/{KEY}"),
        ("GET", f"/api/profile/1/requests/{KEY}"),
    ]
    assert [path for method, path, _ in state.calls if method == "POST"] == [
        "/api/profile/1/load"
    ]
    assert state.calls[0][2] == {
        "request_key": KEY,
        "plan_digest": digest,
    }
