"""The installed CLI feeds one safe Fleet JSON result to a real pipe consumer."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from .test_cli_operator_walkthrough import (
    _effect_counts,
    _session_environment,
    _walkthrough_app,
)
from .test_profile_load_installed_cli import _https_api_peer

pytest_plugins = ("tests.test_profile_load_installed_cli",)
pytestmark = pytest.mark.lane

_JSON_CONSUMER = r"""
import json
import sys

payload = sys.stdin.read()
if not payload.endswith("\n") or payload.count("\n") != 1:
    print("expected exactly one newline-delimited JSON result", file=sys.stderr)
    raise SystemExit(2)
try:
    document = json.loads(payload)
except ValueError as error:
    print("input was not one JSON document", file=sys.stderr)
    raise SystemExit(3) from error
if not isinstance(document, dict):
    print("Fleet result must be an object", file=sys.stderr)
    raise SystemExit(4)
nodes = document.get("nodes")
if not isinstance(nodes, list) or not nodes:
    print("Fleet result must contain at least one node", file=sys.stderr)
    raise SystemExit(5)
print(json.dumps({"node_count": len(nodes)}, separators=(",", ":")))
"""


def test_installed_no_input_fleet_json_pipeline_is_read_only(
    installed_vonkctl: Path, postgres_engine, tmp_path: Path
) -> None:
    sessions, app, headers, *_identities = _walkthrough_app(
        postgres_engine, tmp_path / "owner-services"
    )
    before_effects = _effect_counts(sessions)

    with (
        TestClient(app) as api,
        _https_api_peer(tmp_path, api, headers) as (url, certificate, peer),
    ):
        environment = _session_environment(
            installed_vonkctl=installed_vonkctl,
            workspace=tmp_path,
            url=url,
            certificate=certificate,
            headers=headers,
        )
        consumer_environment = {
            **os.environ,
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": "",
        }

        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as cli_stderr_file:
            cli = subprocess.Popen(
                [str(installed_vonkctl), "--no-input", "--json", "fleet"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=cli_stderr_file,
                cwd=tmp_path,
                env=environment,
                text=True,
            )
            assert cli.stdout is not None
            consumer = subprocess.Popen(
                [sys.executable, "-c", _JSON_CONSUMER],
                stdin=cli.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=tmp_path,
                env=consumer_environment,
                text=True,
            )
            cli.stdout.close()
            try:
                consumer_stdout, consumer_stderr = consumer.communicate(timeout=30)
                cli_status = cli.wait(timeout=30)
            except subprocess.TimeoutExpired:
                consumer.kill()
                cli.kill()
                consumer.communicate(timeout=5)
                cli.wait(timeout=5)
                raise

            cli_stderr_file.seek(0)
            cli_stderr = cli_stderr_file.read()

    assert cli_status == 0, cli_stderr
    assert consumer.returncode == 0, consumer_stderr
    assert cli_stderr == ""
    assert consumer_stderr == ""
    assert consumer_stdout.count("\n") == 1
    summary = json.loads(consumer_stdout)
    assert isinstance(summary, dict)
    assert summary["node_count"] == 1
    assert len(peer.calls) == 1
    method, path, _body = peer.calls[0]
    assert method == "GET"
    assert path.partition("?")[0] == "/api/fleet"
    assert _effect_counts(sessions) == before_effects
