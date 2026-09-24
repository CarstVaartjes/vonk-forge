"""Installed profile editing preserves authoring intent and rejects stale saves."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytest_plugins = ("tests.test_profile_load_installed_cli",)

from .test_fleet_profiles_canonical import NODE_1
from .test_profile_load_installed_cli import (
    _https_api_peer,
    _process_environment,
)
from .test_profile_load_submission import _profile_api


def test_installed_profile_edit_preserves_definition_and_rejects_concurrent_save(
    installed_vonkctl: Path,
    postgres_engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sessions, api, _codec, headers, _preview = _profile_api(postgres_engine)
    initial = {
        "name": "Installed draft",
        "description": "Keep this description",
        "favorite": True,
        "labels": {"purpose": "evaluation"},
        "installation_policy": "exact",
        "assignments": [
            {
                "recipe_selector": "vonk-forge/synthetic-tiny-image",
                "spark_ids": [NODE_1],
                "assignment_name": "installed-draft",
                "model_variant": "precise-variant",
                "desired_state": "installed",
            }
        ],
    }
    created = api.put(
        "/api/profile/1",
        headers=headers,
        json={**initial, "expected_revision": 1},
    )
    assert created.status_code == 200, created.text
    assert created.json()["revision"] == 2

    with _https_api_peer(tmp_path, api, headers) as (url, certificate, state):
        environment = _process_environment(tmp_path, url, certificate, headers)
        edited = subprocess.run(
            [
                str(installed_vonkctl),
                "--profile",
                "1",
                "profile",
                "configure",
                "--favorite",
                "false",
                "--json",
            ],
            env=environment,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        assert edited.returncode == 0, edited.stderr
        assert edited.stdout.count("\n") == 1
        assert not edited.stderr
        saved = json.loads(edited.stdout)
        expected = {**initial, "favorite": False}
        assert saved["revision"] == 3
        assert saved["definition"] == expected

        current = api.get("/api/profile/1/definition", headers=headers)
        assert current.status_code == 200
        assert current.json()["definition"] == expected
        assert current.json()["revision"] == 3

        original_request = state.api.request
        changed_once = False
        concurrent_write_status: list[int] = []

        def write_after_definition_read(method: str, path: str, *args, **kwargs):
            nonlocal changed_once
            response = original_request(method, path, *args, **kwargs)
            if (
                method == "GET"
                and path == "/api/profile/1/definition"
                and not changed_once
            ):
                changed_once = True
                concurrent = {
                    **expected,
                    "name": "Concurrent edit",
                    "expected_revision": 3,
                }
                competing_save = original_request(
                    "PUT",
                    "/api/profile/1",
                    headers=headers,
                    json=concurrent,
                )
                concurrent_write_status.append(competing_save.status_code)
            return response

        monkeypatch.setattr(state.api, "request", write_after_definition_read)
        stale = subprocess.run(
            [
                str(installed_vonkctl),
                "--profile",
                "1",
                "profile",
                "name",
                "Stale CLI edit",
                "--json",
            ],
            env=environment,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )

    assert stale.returncode == 2
    assert stale.stdout.count("\n") == 1
    refusal = json.loads(stale.stdout)
    assert refusal["error_type"] == "control_api"
    assert concurrent_write_status == [200]
    assert state.calls == [
        ("GET", "/api/profile/1/definition", None),
        ("PUT", "/api/profile/1", expected | {"expected_revision": 2}),
        ("GET", "/api/profile/1/definition", None),
        (
            "PUT",
            "/api/profile/1",
            expected | {"name": "Stale CLI edit", "expected_revision": 3},
        ),
    ]
    final = api.get("/api/profile/1/definition", headers=headers)
    assert final.status_code == 200
    assert final.json()["revision"] == 4
    assert final.json()["definition"] == {
        **expected,
        "name": "Concurrent edit",
    }
