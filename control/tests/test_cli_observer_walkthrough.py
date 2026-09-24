"""Disposable installed-CLI U5 setup using the real queued Profile owner."""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from vonk_control.fleet_profile_contract import (
    FleetProfileApplicationView,
    FleetProfileView,
)
from vonk_control.fleet_profiles import FleetProfileService
from vonk_control.models import FleetProfileApplication as FleetProfileApplicationRow

from .test_cli_operator_walkthrough import _AuthorizationHeaders, _session_environment
from .test_fleet_profile_api import _client, _headers
from .test_fleet_profile_cancel import _application
from .test_profile_follow_interrupt_installed import _add_disjoint_profile_node
from .test_profile_load_installed_cli import (
    _build_installed_vonkctl,
    _https_api_peer,
)

pytest_plugins = ("tests.test_profile_load_installed_cli",)
pytestmark = [
    pytest.mark.lane,
    pytest.mark.skipif(
        "VONK_OBSERVER_WALKTHROUGH_MODE" not in os.environ,
        reason="set VONK_OBSERVER_WALKTHROUGH_MODE=smoke or interactive to opt in",
    ),
]


class _Secret(str):
    def __repr__(self) -> str:
        return "<redacted>"


def _accept_newer_disjoint_application(
    *,
    sessions: sessionmaker[Session],
    service: FleetProfileService,
    profile: FleetProfileView,
    original: FleetProfileApplicationView,
    source_node_id: str,
    api: TestClient,
    administrator_headers: dict[str, str],
) -> str:
    new_node_id = f"spk_{uuid4().hex}"
    recipe_slug = _add_disjoint_profile_node(sessions, source_node_id, new_node_id)
    service._clock = lambda: original.created_at + timedelta(seconds=1)

    definition = profile.definition.model_dump(mode="json")
    definition["expected_revision"] = profile.revision
    definition["assignments"] = [
        {
            "recipe_selector": f"vonk-forge/{recipe_slug}",
            "spark_ids": [new_node_id],
            "assignment_name": "later-disjoint",
            "desired_state": "running",
        }
    ]
    saved = api.put(
        f"/api/profile/{profile.number}",
        json=definition,
        headers=administrator_headers,
    )
    assert saved.status_code == 200, saved.text
    preview_response = api.post(
        f"/api/profile/{profile.number}/preview",
        headers=administrator_headers,
    )
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["allowed"] is True
    accepted = api.post(
        f"/api/profile/{profile.number}/load",
        json={
            "request_key": str(uuid4()),
            "plan_digest": preview["plan_digest"],
        },
        headers=administrator_headers,
    )
    assert accepted.status_code == 202, accepted.text
    newer_id = accepted.json().get("id")
    assert isinstance(newer_id, str)
    return newer_id


def _connect_newer_after_first_progress_read(
    *,
    sessions: sessionmaker[Session],
    api: TestClient,
    profile_number: int,
    service: FleetProfileService,
    profile: FleetProfileView,
    original: FleetProfileApplicationView,
    source_node_id: str,
    administrator_headers: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    latest_path = f"/api/profile/{profile_number}/progress"
    original_request = api.request
    newer: dict[str, str] = {}
    errors: list[str] = []

    def create_newer_after_read(method, url, **kwargs):
        response = original_request(method, url, **kwargs)
        if method == "GET" and url == latest_path and not newer and not errors:
            try:
                newer["id"] = _accept_newer_disjoint_application(
                    sessions=sessions,
                    service=service,
                    profile=profile,
                    original=original,
                    source_node_id=source_node_id,
                    api=api,
                    administrator_headers=administrator_headers,
                )
            except Exception as error:  # noqa: BLE001 - retain only safe failure type
                errors.append(type(error).__name__)
        return response

    api.request = create_newer_after_read
    return newer, errors


def _assert_queued_owner_state(
    sessions: sessionmaker[Session],
    service: FleetProfileService,
    profile_number: int,
    original_id: str,
    newer_id: str,
) -> None:
    original = service.application(original_id)
    newer = service.application(newer_id)
    assert original.id == original_id and original.state == "queued"
    assert newer.id == newer_id and newer.state == "queued"
    assert service.progress_number(profile_number).id == newer_id
    with sessions() as session:
        for application_id in (original_id, newer_id):
            row = session.get(FleetProfileApplicationRow, application_id)
            assert row is not None and row.state == "queued"


def _follow_arguments(executable: Path, profile_number: int) -> tuple[str, ...]:
    return (
        str(executable),
        "--profile",
        str(profile_number),
        "--json",
        "profile",
        "progress",
        "--follow",
        "--interval-seconds",
        "0.02",
    )


def _smoke(
    *,
    executable: Path,
    environment: dict[str, str],
    cwd: Path,
    profile_number: int,
    original: FleetProfileApplicationView,
    service: FleetProfileService,
    sessions: sessionmaker[Session],
    peer_calls: list[tuple[str, str, object]],
    newer: dict[str, str],
    errors: list[str],
    token: str,
) -> None:
    exact_path = f"/api/profile/applications/{original.id}"
    process = subprocess.Popen(
        _follow_arguments(executable, profile_number),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        cwd=cwd,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while True:
            if process.poll() is not None:
                pytest.fail(
                    "installed observer exited before interruption", pytrace=False
                )
            if errors:
                pytest.fail(
                    f"newer application admission failed: {errors[0]}", pytrace=False
                )
            exact_reads = sum(
                method == "GET" and path == exact_path
                for method, path, _document in peer_calls
            )
            if newer.get("id") and exact_reads >= 2:
                break
            if time.monotonic() >= deadline:
                pytest.fail(
                    "installed observer did not pin the accepted application",
                    pytrace=False,
                )
            time.sleep(0.02)
        process.send_signal(signal.SIGINT)
        interrupted_stdout, interrupted_stderr = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)

    assert process.returncode == 130
    assert interrupted_stderr == ""
    assert interrupted_stdout.count("\n") == 1
    interrupted = json.loads(interrupted_stdout)
    assert interrupted["observation"]["status"] == "interrupted"
    assert original.id in interrupted["observation"]["reconnect_command"]
    assert interrupted["result"]["id"] == original.id
    assert interrupted["result"]["request_key"] == original.request_key
    assert token not in interrupted_stdout + interrupted_stderr

    newer_id = newer.get("id")
    assert newer_id is not None
    assert newer_id != original.id
    _assert_queued_owner_state(sessions, service, profile_number, original.id, newer_id)

    # A separate installed process follows the original ID with a bounded
    # timeout. The newer application is now the Profile's latest request.
    fresh = subprocess.run(
        (
            str(executable),
            "--json",
            "profile",
            "progress",
            "--application",
            original.id,
            "--follow",
            "--timeout-seconds",
            "0.20",
            "--interval-seconds",
            "0.02",
        ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env=environment,
        cwd=cwd,
        text=True,
        timeout=20,
        check=False,
    )
    assert fresh.returncode == 2, fresh.stdout + fresh.stderr
    assert fresh.stderr == ""
    assert fresh.stdout.count("\n") == 1
    timed_out = json.loads(fresh.stdout)
    assert timed_out["observation"]["status"] == "timed_out"
    assert original.id in timed_out["observation"]["reconnect_command"]
    assert timed_out["result"]["id"] == original.id
    assert timed_out["result"]["state"] == "queued"
    assert token not in fresh.stdout + fresh.stderr
    assert peer_calls[0][0:2] == ("GET", f"/api/profile/{profile_number}/progress")
    assert all(
        method == "GET" and path == exact_path for method, path, _ in peer_calls[1:]
    )
    assert service.application(newer_id).state == "queued"
    print(
        "U5 smoke passed: installed follow interrupted, a newer queued Profile "
        "application became latest, and a fresh installed process timed out on "
        "the original exact ID. Both durable applications remain queued.",
        flush=True,
    )


def _run_walkthrough(postgres_engine, mode: str) -> None:
    temporary_path: Path
    with tempfile.TemporaryDirectory(prefix="vonk-cli-observer-") as temporary:
        workspace = Path(temporary)
        temporary_path = workspace
        workspace.chmod(0o700)
        sessions, _run_switch, service, profile, original, _adapter, nodes = (
            _application(workspace, engine=postgres_engine)
        )
        assert original.state == "queued"
        api, codec = _client(sessions, profiles=service)
        operator_headers = _AuthorizationHeaders(**_headers(codec, "viewer"))
        administrator_headers = _AuthorizationHeaders(
            **_headers(codec, "administrator")
        )
        executable = _build_installed_vonkctl(workspace / "installed-cli")
        operator_cwd = workspace / "operator-cwd"
        operator_cwd.mkdir(mode=0o700)
        with api:
            newer, errors = _connect_newer_after_first_progress_read(
                sessions=sessions,
                api=api,
                profile_number=profile.number,
                service=service,
                profile=profile,
                original=original,
                source_node_id=nodes[0],
                administrator_headers=administrator_headers,
            )
            denied_cancel = api.post(
                f"/api/profile/applications/{original.id}/cancel",
                json={
                    "profile_number": profile.number,
                    "request_key": str(uuid4()),
                },
                headers=operator_headers,
            )
            assert denied_cancel.status_code == 403
            assert service.application(original.id).state == "queued"
            with _https_api_peer(workspace, api, operator_headers) as (
                url,
                certificate,
                peer,
            ):
                environment = _session_environment(
                    installed_vonkctl=executable,
                    workspace=workspace,
                    url=url,
                    certificate=certificate,
                    headers=operator_headers,
                )
                token_file = Path(environment["VONK_CONTROL_TOKEN_FILE"])
                assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
                token = _Secret(token_file.read_text(encoding="utf-8"))
                if operator_headers["Authorization"] != f"Bearer {token}":
                    pytest.fail("local observer token was not preserved", pytrace=False)
                if mode == "smoke":
                    _smoke(
                        executable=executable,
                        environment=environment,
                        cwd=operator_cwd,
                        profile_number=profile.number,
                        original=original,
                        service=service,
                        sessions=sessions,
                        peer_calls=peer.calls,
                        newer=newer,
                        errors=errors,
                        token=token,
                    )
                else:
                    print(f"Disposable Controller: {url}", flush=True)
                    print(f"Installed CLI: {executable}", flush=True)
                    print(f"Profile number: {profile.number}", flush=True)
                    print(
                        "An accepted Profile application is queued with no worker. "
                        "The first Profile progress read will admit a newer "
                        "disjoint queued application through the real owner. "
                        "The session token is in a private 0600 file; no token "
                        "value is displayed.",
                        flush=True,
                    )
                    print(
                        "This is a local-only API setup, not an OS network sandbox; "
                        "it starts no worker and has no physical Spark target. "
                        "Keep the local Controller URL and disposable credential. "
                        "Use the U5 outcome card and shipped runbook. Type `exit` "
                        "or press Ctrl-D to close the shell and clean up.",
                        flush=True,
                    )
                    shell = shutil.which("bash") or "/bin/bash"
                    completed = subprocess.run(
                        [shell, "--noprofile", "--norc", "-i"],
                        env=environment,
                        cwd=operator_cwd,
                        check=False,
                    )
                    print(
                        f"Operator shell exited with status {completed.returncode}.",
                        flush=True,
                    )
                    if errors:
                        pytest.fail(
                            f"newer application admission failed: {errors[0]}",
                            pytrace=False,
                        )
                    newer_id = newer.get("id")
                    if newer_id is not None:
                        assert newer_id != original.id
                        _assert_queued_owner_state(
                            sessions, service, profile.number, original.id, newer_id
                        )
                    else:
                        assert service.application(original.id).state == "queued"
                        print(
                            "No Profile progress read occurred; no newer application "
                            "was admitted.",
                            flush=True,
                        )
    assert not temporary_path.exists()
    print(
        "U5 temporary wheel, local HTTPS server, token, key, and storage files "
        "were cleaned. The PostgreSQL fixture drops this run's database and "
        "stops its own disposable container during pytest teardown.",
        flush=True,
    )


@pytest.mark.skipif(
    os.environ.get("VONK_OBSERVER_WALKTHROUGH_MODE") != "smoke",
    reason="set VONK_OBSERVER_WALKTHROUGH_MODE=smoke to run U5 smoke",
)
def test_disposable_cli_observer_walkthrough_smoke(postgres_engine) -> None:
    _run_walkthrough(postgres_engine, "smoke")


@pytest.mark.skipif(
    os.environ.get("VONK_OBSERVER_WALKTHROUGH_MODE") != "interactive",
    reason="set VONK_OBSERVER_WALKTHROUGH_MODE=interactive to start U5 shell",
)
def test_disposable_cli_observer_walkthrough_interactive(postgres_engine) -> None:
    _run_walkthrough(postgres_engine, "interactive")
