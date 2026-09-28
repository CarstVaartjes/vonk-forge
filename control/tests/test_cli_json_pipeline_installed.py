"""The installed CLI feeds one safe Fleet JSON result to a real pipe consumer."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker
from vonk_control.api import create_app
from vonk_control.auth import Actor, TokenCodec
from vonk_control.fleet_projection import FleetProjection
from vonk_control.jobs import JobService
from vonk_control.models import AgentNode, Base, FleetProfileApplication, Job, RecipeRun

from .test_cli_first_connection_endpoints_installed import _Authority
from .test_profile_load_installed_cli import _https_api_peer, _process_environment

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


def _effect_counts(sessions) -> tuple[int, int, int]:
    with sessions() as session:
        return (
            session.scalar(select(func.count()).select_from(Job)) or 0,
            session.scalar(select(func.count()).select_from(FleetProfileApplication))
            or 0,
            session.scalar(select(func.count()).select_from(RecipeRun)) or 0,
        )


def test_installed_no_input_fleet_json_pipeline_is_read_only(
    installed_vonkctl: Path, postgres_engine, tmp_path: Path
) -> None:
    now = datetime(2026, 9, 24, tzinfo=UTC)
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id="spk_" + "1" * 32,
                state="active",
                architecture="linux-arm64",
                capabilities=[],
                last_seen_at=now,
            )
        )
    tokens = TokenCodec(b"installed-fleet-json-pipeline-key-32")
    app = create_app(
        jobs=JobService(sessions, clock=lambda: now),
        tokens=tokens,
        fleet_projection=FleetProjection(_Authority(), sessions, clock=lambda: now),
        now=lambda: 100,
    )
    headers = {
        "Authorization": "Bearer "
        + tokens.issue(Actor("operator", "viewer"), ttl_seconds=1_000, now=0)
    }
    before_effects = _effect_counts(sessions)

    with (
        TestClient(app) as api,
        _https_api_peer(tmp_path, api, headers) as (url, certificate, peer),
    ):
        environment = _process_environment(tmp_path, url, certificate, headers)
        from tests.subprocess_environment import isolated_environment

        consumer_environment = isolated_environment(
            tmp_path / "consumer-home",
            extra={
                "PYTHONNOUSERSITE": "1",
                "PYTHONPATH": "",
            },
        )

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
