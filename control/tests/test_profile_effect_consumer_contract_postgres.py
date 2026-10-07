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
from pathlib import Path
from typing import cast
from uuid import uuid4
from zipfile import ZipFile

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from vonk_agent_protocol import (
    AgentResult,
    AgentResultState,
    OutcomeDone,
    RecipeStopResult,
)
from vonk_control.fleet_profiles import RunSwitchFleetProfileAdapter
from vonk_control.models import AgentOperation, Job

from .subprocess_environment import install_cli_wheel, isolated_environment
from .test_fleet_profile_api import _client, _headers
from .test_profile_load_installed_cli import _https_api_peer, _process_environment
from .test_profile_stop_effect_adoption_postgres import _claims
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
    source = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
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
    provenance = {
        "platform_source_sha": source,
        "recipes_source_sha": RECIPES_SHA,
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "bundled_openapi_sha256": hashlib.sha256(bundled).hexdigest(),
    }
    output = Path(os.environ["VONK_EFFECT_PROOF_OUTPUT"])
    output.mkdir(parents=True, exist_ok=True)
    (output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
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
        jobs,
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
    assert (
        subprocess.check_output(
            ["git", "-C", str(recipes), "rev-parse", "HEAD"], text=True
        ).strip()
        == RECIPES_SHA
    )
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

    @cast(FastAPI, api.app).middleware("http")
    async def schema_fault(request: Request, call_next):
        response = await call_next(request)
        if corrupt_projection[0] and request.url.path == path:
            assert isinstance(response, StreamingResponse)
            body = b"".join([chunk async for chunk in response.body_iterator])
            document = json.loads(body)
            effect = next(
                item
                for item in document["progress"]["effects"]
                if item["kind"] == "stop"
            )
            del effect["request_key"]
            altered = json.dumps(document).encode()
            response_headers = dict(response.headers)
            response_headers.pop("content-length", None)
            response_headers["X-Content-SHA256"] = hashlib.sha256(altered).hexdigest()
            return Response(
                altered,
                status_code=response.status_code,
                headers=response_headers,
                media_type="application/json",
            )
        return response

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
        assert not malformed.stdout.strip()
        assert "schema" in malformed.stderr.lower()
        corrupt_projection[0] = False
        assert _claims(sessions, nodes[0]) == before_claims

        # The real fenced receipt establishes Stop. Neither absence nor a
        # successful unrelated load is sufficient for the consumer decision.
        receipt = AgentResult(
            fence=claim.fence,
            state=AgentResultState.SUCCEEDED,
            result=OutcomeDone(result=RecipeStopResult()),
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
        assert any(
            method == "GET" and endpoint == path for method, endpoint, _ in peer.calls
        )
        output = Path(os.environ["VONK_EFFECT_PROOF_OUTPUT"])
        (output / "handoff.json").write_text(
            json.dumps(
                {
                    "pending": pending_document,
                    "review": review,
                    "selected": selected_document,
                    "completed": completed_document,
                },
                indent=2,
            )
            + "\n"
        )
