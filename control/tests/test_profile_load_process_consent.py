"""Packaged profile loads never treat redirected or interrupted input as consent."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytest_plugins = ("tests.test_profile_load_installed_cli",)

from sqlalchemy import select
from vonk_control.models import FleetProfileApplication

from .test_profile_load_installed_cli import (
    KEY,
    _https_api_peer,
    _process_environment,
    _run_pty,
)
from .test_profile_load_submission import _profile_api


@pytest.mark.lane
@pytest.mark.parametrize(
    "mode,expected_status", [("redirected", 2), ("eof", 2), ("interrupt", 130)]
)
def test_installed_load_requires_terminal_consent_and_never_mutates_on_abort(
    installed_vonkctl: Path,
    postgres_engine,
    tmp_path: Path,
    mode: str,
    expected_status: int,
) -> None:
    sessions, api, _codec, headers, preview = _profile_api(postgres_engine)
    digest = preview.get("plan_digest")
    assert isinstance(digest, str)
    with _https_api_peer(tmp_path, api, headers) as (url, certificate, state):
        environment = _process_environment(tmp_path, url, certificate, headers)
        if mode == "redirected":
            completed = subprocess.run(
                [
                    str(installed_vonkctl),
                    "--profile",
                    "1",
                    "profile",
                    "load",
                    "--expected-plan",
                    digest,
                    "--request-key",
                    KEY,
                    "--detach",
                ],
                env=environment,
                cwd=tmp_path,
                input="yes\n",
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            status, stdout, stderr = (
                completed.returncode,
                completed.stdout,
                completed.stderr,
            )
        else:
            status, stdout, stderr = _run_pty(
                installed_vonkctl,
                (
                    "--profile",
                    "1",
                    "profile",
                    "load",
                    "--request-key",
                    KEY,
                    "--detach",
                ),
                environment,
                tmp_path,
                answer=None if mode == "eof" else "yes",
                interrupt=mode == "interrupt",
            )

    assert status == expected_status
    assert not stdout
    assert "Request key" not in stderr
    assert "Reconnect" not in stderr
    assert headers["Authorization"].removeprefix("Bearer ") not in stderr
    if mode == "redirected":
        assert "Pass --yes to confirm in noninteractive mode" in stderr
        assert state.calls == []
    else:
        assert "Ready for review" in stderr
        assert stderr.count("[y/N]") == 1
        assert state.calls == [("POST", "/api/profile/1/preview", None)]
        if mode == "eof":
            assert "action was not confirmed" in stderr
        else:
            assert "command interrupted" in stderr

    with sessions() as session:
        assert not list(session.scalars(select(FleetProfileApplication)))
