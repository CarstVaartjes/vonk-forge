"""Hosted proof of Controller → installed CLI → immutable recipes cleanup.

Only the disjoint healthy physical lane is simulated by the shared pending Stop
fixture. The Stop producer, SQL authority, grant, public projection, TLS peer,
installed wheel and recipes consumer are actual implementations.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from vonk_agent_protocol import (
    AgentResult,
    AgentResultState,
    OutcomeDone,
    OutcomeKind,
    RecipeStopResult,
)
from vonk_control.agent_jobs import AgentJobService
from vonk_control.fleet_profiles import RunSwitchFleetProfileAdapter
from vonk_control.models import AgentOperation, Job

from .agent_fences import fenced_attempt, fenced_operation
from .profile_stop_readmission_support import start_on_released_gang
from .subprocess_environment import install_cli_wheel, isolated_environment
from .test_fleet_profile_api import _client, _headers
from .test_profile_load_installed_cli import _https_api_peer, _process_environment
from .test_profile_stop_effect_adoption_postgres import _claims
from .test_recipe_operations import _issue_exact_stop_grant
from .test_run_switch_operations import RecordingArtifactExecutor, _service

pytest_plugins = ("tests.test_profile_stop_effect_adoption_postgres",)

RECIPES_SHA = "a3343cac7ec1747c0ed1a4950d96bc13fc1d6a60"
pytestmark = [
    pytest.mark.skipif(
        not os.environ.get("VONK_RECIPE_EFFECT_CONSUMER_SOURCE"),
        reason="requires the dedicated hosted cross-repository proof checkout",
    ),
    pytest.mark.needs_cli_dependencies,
    pytest.mark.slow(60),
]


@pytest.fixture
def installed_vonkctl(tmp_path_factory):
    """Build the exact checked-out wheel, then install without Controller imports."""
    root = Path(__file__).resolve().parents[2]
    workspace = tmp_path_factory.mktemp("effect-consumer-cli")
    source = os.environ["VONK_EFFECT_PLATFORM_SOURCE_SHA"]
    assert len(source) == 40 and all(c in "0123456789abcdef" for c in source)
    environment = isolated_environment(
        workspace / "build-home",
        extra={"VONK_BUILD_SOURCE_SHA": source, "VONK_BUILD_RELEASE_VERSION": "0.1.1"},
    )
    wheels = workspace / "wheel"
    built = subprocess.run(
        [
            sys.executable,
            "-m",
            "hatchling",
            "build",
            "--target",
            "wheel",
            "--directory",
            str(wheels),
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    [wheel] = wheels.glob("vonk_cluster_profiles-*-py3-none-any.whl")
    with ZipFile(wheel) as archive:
        assert (
            json.loads(archive.read("cluster_profiles/build-identity.json"))[
                "source_sha"
            ]
            == source
        )
        bundled = archive.read("cluster_profiles/schemas/control-openapi.json")
        assert (
            bundled
            == (root / "src/cluster_profiles/schemas/control-openapi.json").read_bytes()
        )
        admin_schema = json.loads(bundled)
        controller_schema = json.loads((root / "control/openapi.json").read_bytes())
        # The browser schema has additional auth routes. Every bundled CLI
        # model still comes from this exact Controller source, unchanged.
        for name, definition in admin_schema["components"]["schemas"].items():
            assert controller_schema["components"]["schemas"][name] == definition
        for path, operation in admin_schema["paths"].items():
            if path.startswith("/api/profile/"):
                assert controller_schema["paths"][path] == operation
    uv = shutil.which("uv")
    assert uv is not None
    venv = workspace / "venv"
    python = install_cli_wheel(uv, venv, wheel, environment)
    isolated = subprocess.run(
        [
            str(python),
            "-c",
            "import importlib.util; assert importlib.util.find_spec('vonk_control') is None; assert importlib.util.find_spec('pydantic') is None",
        ],
        cwd=workspace,
        env={**environment, "PYTHONPATH": ""},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert isolated.returncode == 0, isolated.stderr
    return venv / "bin/vonkctl"


def test_real_pending_stop_crosses_installed_cli_and_recipes_cleanup(
    pending_stop, installed_vonkctl, tmp_path, monkeypatch
):
    (
        sessions,
        profiles,
        profile,
        clock,
        original,
        lifecycle,
        _jobs,
        nodes,
        stop_id,
        request_key,
        stop_index,
        native_id,
        claim,
        plan_digest,
        ordinal,
        healthy_child,
    ) = pending_stop
    recipes = Path(os.environ["VONK_RECIPE_EFFECT_CONSUMER_SOURCE"]).resolve()
    spec = importlib.util.spec_from_file_location(
        "frozen_recipes_cleanup", recipes / "spark_sweep/cleanup.py"
    )
    assert spec is not None and spec.loader is not None
    cleanup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cleanup)
    before_claims = _claims(sessions, nodes[0])
    replacement = profile("installed-consumer-replacement")
    api, tokens = _client(sessions, profiles=profiles)
    headers = _headers(tokens, "administrator")
    path = f"/api/profile/applications/{original.id}"
    corrupt_projection = [False]

    injected_responses: list[tuple[int, bytes]] = []

    class SchemaFaultPeer:
        """Alter complete ASGI bytes, independent of middleware response classes."""

        def __init__(self, app: ASGIApp) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if not (
                corrupt_projection[0]
                and scope["type"] == "http"
                and scope["path"] == path
            ):
                await self.app(scope, receive, send)
                return
            starts: list[Message] = []
            body = bytearray()

            async def fault_send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    starts.append(message)
                elif message["type"] == "http.response.body":
                    body.extend(message.get("body", b""))
                    assert len(body) <= 1_048_576
                    if message.get("more_body", False):
                        return
                    [start] = starts
                    assert start["status"] == 200
                    document = json.loads(body)
                    effect = next(
                        item
                        for item in document["progress"]["effects"]
                        if item["kind"] == "stop"
                    )
                    assert effect["request_key"] == request_key
                    del effect["request_key"]
                    altered = json.dumps(document).encode()
                    headers = [
                        (name, value)
                        for name, value in start["headers"]
                        if name.lower() not in {b"content-length", b"x-content-sha256"}
                    ]
                    headers.extend(
                        [
                            (b"content-length", str(len(altered)).encode()),
                            (
                                b"x-content-sha256",
                                hashlib.sha256(altered).hexdigest().encode(),
                            ),
                        ]
                    )
                    injected_responses.append((start["status"], altered))
                    await send({**start, "headers": headers})
                    await send({"type": "http.response.body", "body": altered})
                else:
                    await send(message)

            await self.app(scope, receive, fault_send)

    api = TestClient(SchemaFaultPeer(api.app))

    with _https_api_peer(tmp_path, api, headers) as (url, certificate, peer):
        environment = _process_environment(tmp_path, url, certificate, headers)

        def run(*args):
            return subprocess.run(
                [str(installed_vonkctl), *args],
                cwd=tmp_path,
                env=environment,
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )

        def observe(application_id=original.id):
            result = run(
                "profile", "progress", "--application", application_id, "--json"
            )
            assert result.returncode == 0, result.stdout + result.stderr
            return json.loads(result.stdout)

        pending_document = observe()
        assert pending_document["id"] == original.id
        pending_effects = cleanup.stop_effects(pending_document)
        assert pending_effects is not None
        [pending] = pending_effects
        assert pending["application_id"] == original.id
        assert pending["plan_digest"] == plan_digest
        assert pending["workload_intent_ordinal"] == ordinal
        assert pending["queue_index"] == stop_index
        assert pending["operation_id"] == stop_id
        assert pending["request_key"] == request_key
        assert pending["node_ids"] == [nodes[0]]
        assert pending["stop_effect"]["run_id"] == pending["target_id"]
        assert not cleanup.stopped(pending)
        assert cleanup.stop_effects({}) is None
        review_result = run(
            "--profile",
            str(replacement.number),
            "profile",
            "load",
            "--review",
            "--json",
        )
        assert review_result.returncode == 0, (
            review_result.stdout + review_result.stderr
        )
        review = json.loads(review_result.stdout)
        assert cleanup.adopted(review, pending)
        selected = profiles.apply(
            replacement.id, request_key=str(uuid4()), actor="admin"
        )
        selected_document = observe(selected.id)
        selected_effects = cleanup.stop_effects(selected_document)
        assert selected_effects is not None
        [selected_pending] = selected_effects
        assert cleanup.same_effect(pending, selected_pending)
        assert selected_pending["application_id"] == original.id
        assert not cleanup.stopped(selected_pending)
        assert _claims(sessions, nodes[0]) == before_claims

        # A valid TLS/JSON response with missing canonical authority is rejected
        # by the installed raw OpenAPI boundary, before recipes sees any output.
        corrupt_projection[0] = True
        malformed = run("profile", "progress", "--application", original.id, "--json")
        assert malformed.returncode != 0
        [injected] = injected_responses
        assert injected[0] == 200
        bad_document = json.loads(injected[1])
        bad_effect = next(
            item
            for item in bad_document["progress"]["effects"]
            if item["kind"] == "stop"
        )
        assert "request_key" not in bad_effect
        problem = json.loads(malformed.stdout)
        assert problem["code"] == "controller.protocol_invalid"
        assert problem["source"] == "protocol"
        assert problem["decision"] == "exit"
        assert "OpenAPI schema" in problem["detail"]
        assert "request_key" in problem["detail"]
        assert "progress" not in problem and "id" not in problem
        assert cleanup.stop_effects(problem) is None
        corrupt_projection[0] = False
        assert _claims(sessions, nodes[0]) == before_claims

        # Reconnect the offline lane through transport reconciliation and the
        # exact-plan signer. The retained request/native operation stay fixed;
        # an expired receipt cannot release their original capacity claims.
        clock[0] += timedelta(hours=1)
        jobs = AgentJobService(sessions, clock=lambda: clock[0])
        jobs.set_result_consumer(lifecycle.consume_agent_result)
        jobs.reconcile_orders()
        with sessions() as session:
            native = session.get(AgentOperation, native_id)
            assert native is not None
            if native.next_action_at is not None:
                clock[0] = max(clock[0], native.next_action_at)
        fresh, exact_stop, _grant = _issue_exact_stop_grant(
            sessions,
            node_id=nodes[0],
            certificate_serial="serial-0",
            grant_now=clock[0],
        )
        assert fresh.fence != claim.fence
        assert fresh.payload == claim.payload
        assert fenced_operation(sessions, fresh).id == native_id
        assert (
            fenced_attempt(sessions, fresh).attempt
            > fenced_attempt(sessions, claim).attempt
        )
        assert exact_stop.run_id == pending["stop_effect"]["run_id"]
        stale_receipt = AgentResult(
            fence=claim.fence,
            state=AgentResultState.SUCCEEDED,
            result=OutcomeDone(kind=OutcomeKind.DONE, result=RecipeStopResult()),
        )
        assert jobs.record_late_result(
            AgentResult.model_validate_json(stale_receipt.model_dump_json())
        )
        assert fenced_operation(sessions, fresh).state == "running"
        assert _claims(sessions, nodes[0]) == before_claims
        still_pending = cleanup.stop_effects(observe())
        assert still_pending is not None and len(still_pending) == 1
        assert cleanup.same_effect(pending, still_pending[0])
        assert not cleanup.stopped(still_pending[0])

        # Only the fresh canonical receipt establishes Stop.
        receipt = AgentResult(
            fence=fresh.fence,
            state=AgentResultState.SUCCEEDED,
            result=OutcomeDone(kind=OutcomeKind.DONE, result=RecipeStopResult()),
        )
        jobs.record_result(AgentResult.model_validate_json(receipt.model_dump_json()))
        coordinator = _service(
            sessions, clock[0], lifecycle, RecordingArtifactExecutor()
        )
        adapter = RunSwitchFleetProfileAdapter(sessions, coordinator)
        native_observe = adapter._observed_child
        monkeypatch.setattr(
            adapter,
            "_observed_child",
            lambda identity: (
                healthy_child
                if identity == healthy_child.operation_id
                else native_observe(identity)
            ),
        )
        for _ in range(3):
            coordinator.tick()
            adapter.advance(original.id)
        completed_document = observe()
        completed_effects = cleanup.stop_effects(completed_document)
        assert completed_effects is not None
        [completed] = completed_effects
        assert cleanup.same_effect(pending, completed)
        assert completed["operation_id"] == stop_id
        assert cleanup.stopped(completed)
        with sessions() as session:
            assert (
                session.scalar(select(Job.id).where(Job.request_id == request_key))
                == stop_id
            )
            assert {
                item.id
                for item in session.scalars(
                    select(AgentOperation).where(AgentOperation.kind == "recipe.stop")
                )
            } == {native_id}
        start_on_released_gang(
            sessions,
            lifecycle,
            pending["stop_effect"]["run_id"],
            nodes,
            clock=lambda: clock[0],
            request_id="00000000-0000-4000-8000-000000018730",
        )
        assert any(
            method == "GET" and endpoint == path for method, endpoint, _ in peer.calls
        )
