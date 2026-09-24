"""Disposable installed-CLI U7 setup for Profile routes and artifact results."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    AgentResult,
    RecipeJobFile,
    RecipeJobRunResult,
    recipe_job_manifest_document,
    recipe_job_manifest_sha256,
)
from vonk_control.agent_jobs import AgentJobService
from vonk_control.api import create_app
from vonk_control.artifact_jobs import ArtifactJobService
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.fleet_profiles import FleetProfileService
from vonk_control.fleet_projection import FleetProjection
from vonk_control.jobs import JobService
from vonk_control.models import AgentOperation, ArtifactJob
from vonk_control.operation_api import durable_operation_services

from .runtime_identity_support import claim_agent
from .test_artifact_job_installed_cli import OUTPUT_LIMITS, _artifact_api
from .test_cli_first_connection_endpoints_installed import (
    _Authority,
    _seed_profile_application_for_run,
)
from .test_cli_operator_walkthrough import _AuthorizationHeaders, _session_environment
from .test_profile_load_installed_cli import (
    _build_installed_vonkctl,
    _https_api_peer,
)
from .test_recipe_operations import NOW as ARTIFACT_NOW
from .test_recipe_routes import NOW as ROUTE_NOW
from .test_recipe_routes import atomic_service
from .test_recipe_routes import setup as setup_routes

pytest_plugins = ("tests.test_profile_load_installed_cli",)
pytestmark = [
    pytest.mark.lane,
    pytest.mark.skipif(
        "VONK_RESULTS_WALKTHROUGH_MODE" not in os.environ,
        reason="set VONK_RESULTS_WALKTHROUGH_MODE=smoke or interactive to opt in",
    ),
]


@contextmanager
def _disposable_database(server_engine: Engine) -> Iterator[Engine]:
    database = f"vonk_u7_{uuid4().hex}"
    with server_engine.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{database}"')
    isolated = create_engine(
        server_engine.url.set(database=database), pool_pre_ping=True
    )
    try:
        yield isolated
    finally:
        isolated.dispose()
        with server_engine.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')


def _registered_api(
    *,
    route_sessions: sessionmaker[Session],
    route_root: Path,
    profiles: FleetProfileService,
    artifact_jobs: ArtifactJobService,
) -> tuple[TestClient, _AuthorizationHeaders]:
    codec = TokenCodec(os.urandom(32))
    now = int(datetime.now(UTC).timestamp())
    clock = lambda: ROUTE_NOW
    operations = durable_operation_services(
        route_sessions,
        route_root,
        clock=clock,
        cursors=codec.cursor_codec(),
        profile_endpoint_intent=profiles.endpoint_intent,
    )
    app = create_app(
        jobs=JobService(route_sessions, clock=clock),
        tokens=codec,
        audits=MemoryAuditStore(),
        fleet_projection=FleetProjection(_Authority(), route_sessions, clock=clock),
        operations=operations,
        fleet_profiles=profiles,
        artifact_jobs=artifact_jobs,
        now=lambda: now,
    )
    token = codec.issue(
        Actor("u7-facilitator", "administrator"),
        ttl_seconds=14_400,
        now=now,
    )
    return TestClient(app), _AuthorizationHeaders(Authorization=f"Bearer {token}")


def _run_cli(
    executable: Path,
    environment: dict[str, str],
    cwd: Path,
    *arguments: str,
    json_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [
            str(executable),
            *(["--json"] if json_output else []),
            *arguments,
        ],
        env=environment,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    token = Path(environment["VONK_CONTROL_TOKEN_FILE"]).read_text(encoding="utf-8")
    if token in result.stdout or token in result.stderr:
        pytest.fail("installed CLI exposed the private U7 credential", pytrace=False)
    return result


def _create_artifact_job(
    *,
    executable: Path,
    environment: dict[str, str],
    cwd: Path,
    run_id: str,
    binding: Path,
) -> str:
    created = _run_cli(
        executable,
        environment,
        cwd,
        "recipe",
        "job",
        "create",
        "--run",
        run_id,
        "--file",
        str(binding),
        "--request-key",
        str(uuid4()),
    )
    assert created.returncode == 0, created.stdout + created.stderr
    record = json.loads(created.stdout)
    job_id = record.get("id")
    assert isinstance(job_id, str) and record["state"] == "ready"
    return job_id


def _submit_artifact_job(
    *,
    executable: Path,
    environment: dict[str, str],
    cwd: Path,
    job_id: str,
) -> None:
    submitted = _run_cli(
        executable,
        environment,
        cwd,
        "recipe",
        "job",
        "submit",
        job_id,
        "--request-key",
        str(uuid4()),
    )
    assert submitted.returncode == 0, submitted.stdout + submitted.stderr
    record = json.loads(submitted.stdout)
    assert record["id"] == job_id and record["state"] == "queued"


def _configure_deterministic_executor(
    service: ArtifactJobService,
    sessions: sessionmaker[Session],
    node_id: str,
) -> AgentJobService:
    recipe_operations = service._recipe_operations
    agent_jobs = AgentJobService(sessions, clock=lambda: ARTIFACT_NOW)

    def consume(session, operation, attempt, message) -> None:
        service.consume_agent_result(session, operation, attempt, message)
        recipe_operations.consume_agent_result(session, operation, attempt, message)

    agent_jobs.set_result_consumer(consume)
    recipe_operations._agent_jobs = agent_jobs
    assert claim_agent(agent_jobs, node_id, "serial-0", 3_600) is None
    return agent_jobs


def _execute_one_deterministic_result(
    *,
    agent_jobs: AgentJobService,
    service: ArtifactJobService,
    node_id: str,
    job_id: str,
    output_content: bytes | None,
) -> None:
    claim = claim_agent(agent_jobs, node_id, "serial-0", 3_600)
    assert claim is not None
    view = service.get(job_id)
    assert view.operation_id == claim.job_id
    if output_content is None:
        outputs: tuple[RecipeJobFile, ...] = ()
    else:
        digest = hashlib.sha256(output_content).hexdigest()
        service.put_output(
            job_id,
            node_id=node_id,
            name="result.png",
            media_type="image/png",
            expected_sha256=digest,
            content=output_content,
        )
        outputs = (
            RecipeJobFile(
                name="result.png",
                media_type="image/png",
                size_bytes=len(output_content),
                sha256=digest,
            ),
        )
    result = AgentResult(
        schema_version=1,
        job_id=claim.job_id,
        operation_id=claim.operation_id,
        attempt=claim.attempt,
        fence=claim.fence,
        node_id=claim.node_id,
        deadline=claim.deadline,
        state="succeeded",
        result=RecipeJobRunResult.model_validate(
            {
                "schema_version": 1,
                "job_id": job_id,
                "run_id": view.run_id,
                "exit_code": 0,
                "output_manifest": {
                    **recipe_job_manifest_document(outputs),
                    "manifest_sha256": recipe_job_manifest_sha256(outputs),
                },
                "evidence": {
                    "elapsed_milliseconds": 10,
                    "peak_memory_bytes": None,
                },
            }
        ),
    )
    # The deterministic fixture crosses the real AgentJobService result
    # consumer and ArtifactJobService verification/publication owner.
    agent_jobs.record_result(result)
    assert service.result_metadata(job_id).state == "succeeded"


def _seed_scenarios(
    *,
    executable: Path,
    environment: dict[str, str],
    cwd: Path,
    artifact_run_id: str,
    artifact_service: ArtifactJobService,
    artifact_sessions: sessionmaker[Session],
    artifact_node_id: str,
) -> tuple[str, str, str, bytes]:
    input_path = cwd / "source.png"
    input_path.write_bytes(b"deterministic U7 input")
    binding_path = cwd / "artifact-binding.json"
    binding_path.write_text(
        json.dumps(
            {
                "create": {
                    "interface": "image-job",
                    "parameters": {"prompt": "U7 fixture", "seed": 0},
                    "inputs": [
                        {
                            "slot": "input",
                            "name": "source.png",
                            "media_type": "image/png",
                        }
                    ],
                    "output_limits": OUTPUT_LIMITS,
                    "timeout_seconds": 300,
                },
                "input_paths": {"source.png": str(input_path)},
            }
        ),
        encoding="utf-8",
    )

    # A finalized, not-submitted draft has no result. It does not reserve the
    # running RecipeRun, so the two completed deterministic jobs can follow.
    unavailable_id = _create_artifact_job(
        executable=executable,
        environment=environment,
        cwd=cwd,
        run_id=artifact_run_id,
        binding=binding_path,
    )
    verified_id = _create_artifact_job(
        executable=executable,
        environment=environment,
        cwd=cwd,
        run_id=artifact_run_id,
        binding=binding_path,
    )
    agent_jobs = _configure_deterministic_executor(
        artifact_service, artifact_sessions, artifact_node_id
    )
    _submit_artifact_job(
        executable=executable,
        environment=environment,
        cwd=cwd,
        job_id=verified_id,
    )
    result_bytes = b"verified deterministic U7 result bytes"
    _execute_one_deterministic_result(
        agent_jobs=agent_jobs,
        service=artifact_service,
        node_id=artifact_node_id,
        job_id=verified_id,
        output_content=result_bytes,
    )

    empty_id = _create_artifact_job(
        executable=executable,
        environment=environment,
        cwd=cwd,
        run_id=artifact_run_id,
        binding=binding_path,
    )
    _submit_artifact_job(
        executable=executable,
        environment=environment,
        cwd=cwd,
        job_id=empty_id,
    )
    _execute_one_deterministic_result(
        agent_jobs=agent_jobs,
        service=artifact_service,
        node_id=artifact_node_id,
        job_id=empty_id,
        output_content=None,
    )
    return unavailable_id, verified_id, empty_id, result_bytes


def _smoke(
    *,
    executable: Path,
    environment: dict[str, str],
    cwd: Path,
    route_profile_id: str,
    route_application_id: str,
    route_run_id: str,
    artifact_run_id: str,
    artifact_sessions: sessionmaker[Session],
    unavailable_id: str,
    verified_id: str,
    empty_id: str,
    result_bytes: bytes,
    peer_calls: list[tuple[str, str, object]],
) -> None:
    endpoint = _run_cli(
        executable,
        environment,
        cwd,
        "--profile",
        "1",
        "profile",
        "endpoint",
    )
    assert endpoint.returncode == 0, endpoint.stdout + endpoint.stderr
    endpoint_view = json.loads(endpoint.stdout)
    assert endpoint_view["profile_id"] == route_profile_id
    assert endpoint_view["application_id"] == route_application_id
    assignments = endpoint_view.get("assignments")
    assert isinstance(assignments, list) and len(assignments) == 1
    assignment = assignments[0]
    assert assignment["state"] == "published"
    assert assignment["alias"] == "qwen"
    assert assignment["endpoint"]["api_base"] == "http://10.0.0.2:8000/v1"
    assert "Authorization" not in endpoint.stdout

    detail_unavailable = _run_cli(
        executable,
        environment,
        cwd,
        "recipe",
        "job",
        "detail",
        unavailable_id,
        json_output=False,
    )
    assert detail_unavailable.returncode == 0, detail_unavailable.stderr
    assert "Result files: unavailable until job succeeds (state: ready)" in (
        detail_unavailable.stdout
    )
    unavailable_directory = cwd / "unavailable-results"
    unavailable_directory.mkdir()
    unavailable_download = _run_cli(
        executable,
        environment,
        cwd,
        "recipe",
        "job",
        "download",
        unavailable_id,
        "--output",
        str(unavailable_directory),
    )
    assert unavailable_download.returncode == 2
    assert list(unavailable_directory.iterdir()) == []

    listed = _run_cli(
        executable,
        environment,
        cwd,
        "recipe",
        "job",
        "list",
        "--run",
        artifact_run_id,
    )
    assert listed.returncode == 0, listed.stdout + listed.stderr
    listing = json.loads(listed.stdout)
    jobs = listing.get("jobs")
    assert isinstance(jobs, list) and len(jobs) == 3
    by_id = {item["id"]: item for item in jobs}
    assert by_id[unavailable_id]["state"] == "ready"
    assert by_id[verified_id]["state"] == "succeeded"
    assert by_id[empty_id]["state"] == "succeeded"
    assert by_id[verified_id]["output_files"][0]["name"] == "result.png"
    assert by_id[empty_id]["output_files"] == []

    empty_detail = _run_cli(
        executable,
        environment,
        cwd,
        "recipe",
        "job",
        "detail",
        empty_id,
        json_output=False,
    )
    assert empty_detail.returncode == 0, empty_detail.stderr
    assert "Result files: none (job succeeded with an empty result)." in (
        empty_detail.stdout
    )
    empty_directory = cwd / "empty-results"
    empty_directory.mkdir()
    empty_download = _run_cli(
        executable,
        environment,
        cwd,
        "recipe",
        "job",
        "download",
        empty_id,
        "--output",
        str(empty_directory),
        json_output=False,
    )
    assert empty_download.returncode == 0, empty_download.stdout + empty_download.stderr
    assert "job succeeded with an empty result" in empty_download.stdout
    assert list(empty_directory.iterdir()) == []

    result_directory = cwd / "verified-results"
    result_directory.mkdir()
    downloaded = _run_cli(
        executable,
        environment,
        cwd,
        "recipe",
        "job",
        "download",
        verified_id,
        "--output",
        str(result_directory),
    )
    assert downloaded.returncode == 0, downloaded.stdout + downloaded.stderr
    transfer = json.loads(downloaded.stdout)
    assert transfer["job_id"] == verified_id
    assert transfer["files"][0]["name"] == "result.png"
    assert transfer["files"][0]["size_bytes"] == len(result_bytes)
    assert transfer["files"][0]["sha256"] == hashlib.sha256(result_bytes).hexdigest()
    saved_file = result_directory / "result.png"
    assert saved_file.read_bytes() == result_bytes
    assert (
        hashlib.sha256(saved_file.read_bytes()).hexdigest()
        == transfer["files"][0]["sha256"]
    )

    with artifact_sessions() as session:
        for job_id in (verified_id, empty_id):
            job = session.get(ArtifactJob, job_id)
            assert job is not None and job.state == "succeeded"
            assert job.output_manifest_sha256 is not None
        successful_job = session.get(ArtifactJob, verified_id)
        assert successful_job is not None and successful_job.operation_id is not None
        operation = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == successful_job.operation_id
            )
        )
        assert operation is not None and operation.state == "succeeded"

    assert peer_calls.count(("GET", "/api/profile/1/endpoints", None)) == 1
    assert any(
        method == "GET" and path == f"/api/recipe/runs/{artifact_run_id}/artifact-jobs"
        for method, path, _ in peer_calls
    )
    assert any(
        method == "GET"
        and path.startswith(f"/api/artifact-jobs/{verified_id}/results/")
        for method, path, _ in peer_calls
    )
    assert route_run_id != artifact_run_id
    print(
        "U7 smoke passed: installed CLI used the current Profile endpoint and "
        "distinguished a ready job without results, a succeeded empty result, "
        "and a succeeded file whose downloaded bytes match the verified digest. "
        "Artifact success crossed the registered result-consumption owner; no "
        "Spark execution is claimed.",
        flush=True,
    )


def _walkthrough(postgres_server_engine: Engine, mode: str) -> None:
    temporary_path: Path
    with tempfile.TemporaryDirectory(prefix="vonk-cli-results-") as temporary:
        workspace = Path(temporary)
        temporary_path = workspace
        workspace.chmod(0o700)
        with _disposable_database(postgres_server_engine) as route_engine:  # noqa: SIM117 - dispose artifact DB before route DB
            with _disposable_database(postgres_server_engine) as artifact_engine:
                route_root = workspace / "route-bundles"
                route_base, _publisher, _applied, route_run_id = setup_routes(
                    workspace / "route-fixture", ranks=2, engine=route_engine
                )
                routes = atomic_service(route_base, route_root, lambda: ROUTE_NOW)
                routes.publish_run(route_run_id)
                route_sessions = sessionmaker(route_engine, expire_on_commit=False)
                profiles, profile_id, application_id, _assignment_id = (
                    _seed_profile_application_for_run(route_sessions, route_run_id)
                )

                artifact_root = workspace / "artifact-fixture"
                artifact_root.mkdir(mode=0o700)
                (
                    artifact_sessions,
                    unused_client,
                    artifact_service,
                    artifact_run_id,
                    node_id,
                ) = _artifact_api(artifact_root, artifact_engine, output_min_files=0)
                unused_client.close()
                api, headers = _registered_api(
                    route_sessions=route_sessions,
                    route_root=route_root,
                    profiles=profiles,
                    artifact_jobs=artifact_service,
                )
                executable = _build_installed_vonkctl(workspace / "installed-cli")
                operator_cwd = workspace / "operator-cwd"
                operator_cwd.mkdir(mode=0o700)
                with api:  # noqa: SIM117 - stop HTTPS peer before its ASGI client
                    with _https_api_peer(workspace, api, headers) as (
                        url,
                        certificate,
                        peer,
                    ):
                        environment = _session_environment(
                            installed_vonkctl=executable,
                            workspace=workspace,
                            url=url,
                            certificate=certificate,
                            headers=headers,
                        )
                        token_file = Path(environment["VONK_CONTROL_TOKEN_FILE"])
                        assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
                        if headers["Authorization"] != (
                            "Bearer " + token_file.read_text(encoding="utf-8")
                        ):
                            pytest.fail(
                                "private U7 token file did not match the local API identity",
                                pytrace=False,
                            )
                        unavailable_id, verified_id, empty_id, result_bytes = (
                            _seed_scenarios(
                                executable=executable,
                                environment=environment,
                                cwd=operator_cwd,
                                artifact_run_id=artifact_run_id,
                                artifact_service=artifact_service,
                                artifact_sessions=artifact_sessions,
                                artifact_node_id=node_id,
                            )
                        )
                        if mode == "smoke":
                            _smoke(
                                executable=executable,
                                environment=environment,
                                cwd=operator_cwd,
                                route_profile_id=profile_id,
                                route_application_id=application_id,
                                route_run_id=route_run_id,
                                artifact_run_id=artifact_run_id,
                                artifact_sessions=artifact_sessions,
                                unavailable_id=unavailable_id,
                                verified_id=verified_id,
                                empty_id=empty_id,
                                result_bytes=result_bytes,
                                peer_calls=peer.calls,
                            )
                        else:
                            print(f"Disposable Controller: {url}", flush=True)
                            print(f"Installed CLI: {executable}", flush=True)
                            print("Profile number: 1", flush=True)
                            print(f"Artifact run ID: {artifact_run_id}", flush=True)
                            print(
                                "The run has one ready draft with no result and two "
                                "completed jobs, one with a verified file and one "
                                "with an empty successful result. Find them through "
                                "the installed CLI and shipped runbook.",
                                flush=True,
                            )
                            print(
                                "The bearer credential is in a private 0600 file; "
                                "its value is not displayed. Use the U7 outcome card "
                                "and shipped runbook. The setup uses local PostgreSQL "
                                "owners and a loopback HTTPS API; it starts no Spark "
                                "worker and changes no external Fleet. This is not "
                                "an OS network sandbox. Type `exit` or press Ctrl-D "
                                "to close the session and clean up.",
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
    assert not temporary_path.exists()
    print(
        "U7 temporary CLI wheel, loopback HTTPS setup, credentials, route and "
        "artifact storage were cleaned. Both per-run PostgreSQL databases were "
        "dropped; the session-scoped disposable PostgreSQL container is stopped "
        "by pytest teardown.",
        flush=True,
    )


@pytest.mark.skipif(
    os.environ.get("VONK_RESULTS_WALKTHROUGH_MODE") != "smoke",
    reason="set VONK_RESULTS_WALKTHROUGH_MODE=smoke to run U7 smoke",
)
def test_disposable_cli_results_walkthrough_smoke(
    postgres_server_engine: Engine,
) -> None:
    _walkthrough(postgres_server_engine, "smoke")


@pytest.mark.skipif(
    os.environ.get("VONK_RESULTS_WALKTHROUGH_MODE") != "interactive",
    reason="set VONK_RESULTS_WALKTHROUGH_MODE=interactive to start U7 shell",
)
def test_disposable_cli_results_walkthrough_interactive(
    postgres_server_engine: Engine,
) -> None:
    _walkthrough(postgres_server_engine, "interactive")
