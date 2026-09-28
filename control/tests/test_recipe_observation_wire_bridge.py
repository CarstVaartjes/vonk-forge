from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, overload

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    RecipeRunObservationsWire,
    RecipeRunObservationWire,
    canonical_message,
)
from vonk_control.agent_api import AgentApiServices
from vonk_control.agent_jobs import AgentJobService
from vonk_control.api import create_app
from vonk_control.auth import TokenCodec
from vonk_control.bounded_json import require_mapping, require_sequence
from vonk_control.distributed_recovery import DistributedRecoveryCoordinator
from vonk_control.host_helper_authority import (
    HostHelperGrantIssuer,
    HostRuntimeAuthorityService,
)
from vonk_control.install_admission import InstallAdmissionService
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    AgentPresence,
    Job,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from vonk_control.presence import AgentPresenceService, ManagementAddressPolicy
from vonk_control.recipe_operation_worker import RecipeOperationWorker
from vonk_control.recipe_operations import RecipeOperationService
from vonk_control.recipe_routes import RecipeRouteService
from vonk_control.run_admission import RunAdmissionService
from vonk_control.source_bundles import SourceBundleStore

from tests.wire_probes import prebuilt_probe

from .test_recipe_operations import (
    NOW,
    ConcurrentPublisher,
    RecordingQueue,
    _issue_exact_stop_grant,
    bind_route_publications,
    installed_recipe,
    setup_services,
)


class _UnusedJobs:
    """Structural ``JobQueue`` for agent routes that never touch a Controller Job.

    ``test_agent_api.Jobs`` predates the ``list_page`` member of the protocol,
    so this suite supplies its own conforming stub instead of editing another
    suite's double.
    """

    def enqueue(
        self,
        kind: str,
        actor: str,
        authority_revision: str,
        targets: Sequence[str],
        payload: Mapping[str, object],
        *,
        request_id: str,
    ) -> object:
        raise AssertionError("the observation wire bridge must not enqueue work")

    def get(self, job_id: str) -> object:
        raise KeyError(job_id)

    def list(self, *, limit: int = 100) -> list[object]:
        return []

    def list_page(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
        status: str | None = None,
        target: str | None = None,
    ) -> tuple[list[object], str | None, int]:
        return [], None, 0


def _singleton_recovery_process_tick(database_url: str, now_value: str) -> None:
    """Reconstruct the Controller worker in a new process against PostgreSQL."""

    from vonk_control.agent_jobs import AgentJobService

    now = datetime.fromisoformat(now_value)
    engine = create_engine(database_url)
    sessions = sessionmaker(engine, expire_on_commit=False)
    jobs = AgentJobService(sessions, clock=lambda: now)
    routes = RecipeRouteService(
        sessions,
        publisher=ConcurrentPublisher(),
        management_policy=ManagementAddressPolicy.parse("192.168.1.0/24"),
        clock=lambda: now,
        maximum_age_seconds=120,
    )
    lifecycle = RecipeOperationService(
        sessions,
        install_admission=InstallAdmissionService(sessions),
        run_admission=RunAdmissionService(sessions),
        agent_jobs=jobs,
        clock=lambda: now,
        route_publications=routes,
    )
    recovery = DistributedRecoveryCoordinator(
        sessions,
        routes=routes,
        agent_jobs=jobs,
        clock=lambda: now,
        recovery_run_stops=lifecycle,
        singleton_start_timeout_seconds=60,
    )
    worked = recovery.tick()
    engine.dispose()
    os._exit(23 if worked else 24)


@pytest.fixture(scope="session")
def recipe_observation_wire_probe() -> Path:
    return prebuilt_probe("VONK_RECIPE_OBSERVATION_WIRE_PROBE")


@overload
def _production_controller_app(
    tmp_path: Path,
    *,
    nodes: int,
    producer: Path,
    clock: Callable[[], datetime] = lambda: NOW,
    engine: Engine | None = None,
    include_recovery: Literal[True],
    recipe_transform: Callable[[dict[str, object]], None] | None = None,
) -> tuple[
    FastAPI,
    sessionmaker[Session],
    str,
    str,
    str,
    bytes,
    Mapping[str, object],
    RecipeOperationService,
    RecordingQueue,
    str,
    tuple[str, ...],
]: ...


@overload
def _production_controller_app(
    tmp_path: Path,
    *,
    nodes: int,
    producer: Path,
    clock: Callable[[], datetime] = lambda: NOW,
    engine: Engine | None = None,
    include_recovery: Literal[False] = False,
    recipe_transform: Callable[[dict[str, object]], None] | None = None,
) -> tuple[
    FastAPI,
    sessionmaker[Session],
    str,
    str,
    str,
    bytes,
    Mapping[str, object],
]: ...


def _production_controller_app(
    tmp_path: Path,
    *,
    nodes: int,
    producer: Path,
    clock: Callable[[], datetime] = lambda: NOW,
    engine: Engine | None = None,
    include_recovery: bool = False,
    recipe_transform: Callable[[dict[str, object]], None] | None = None,
):
    sessions, service, _queue, mapping_id, build_id, node_ids = setup_services(
        tmp_path,
        nodes=nodes,
        distributed_lifecycle=nodes > 1,
        recipe_transform=recipe_transform,
        engine=engine,
    )
    installation = installed_recipe(
        service, mapping_id, build_id, node_ids, request_id="1" * 36
    )
    run_plan = service.preview_run(installation.owner_id, f"wire-{nodes}")
    started = service.start(
        run_plan,
        plan_digest=run_plan.plan_digest,
        actor="admin",
        request_id="2" * 36,
    )
    completed: set[str] = set()
    captured: dict[str, Mapping[str, object]] = {}
    while service.get(started.id).state == "running":
        with sessions() as session:
            pending = tuple(
                child
                for child in session.scalars(
                    select(AgentOperation).where(
                        AgentOperation.parent_job_id == started.id
                    )
                )
                if child.id not in completed
            )
        assert pending
        for child in pending:
            assert child.payload.get("run_generation") is not None
            plan_document = require_mapping(
                child.payload["compiled_execution_plan"], "compiled execution plan"
            )
            placement = require_mapping(
                require_mapping(plan_document["runtime"], "compiled runtime")[
                    "placement"
                ],
                "compiled runtime placement",
            )
            if (
                nodes > 1
                and child.payload["local_address"] != child.payload["master_address"]
            ):
                assert placement["endpoint_address"] is None
                assert (
                    child.payload["endpoint_address"] == child.payload["local_address"]
                )
            with sessions() as session:
                run = session.get(RecipeRun, started.owner_id)
                assert run is not None
                installation_row = session.get(RecipeInstallation, run.installation_id)
                assert installation_row is not None
                installation_plan = require_mapping(
                    installation_row.plan, "installation plan"
                )
                compiled = require_mapping(
                    require_mapping(
                        installation_plan["compiled_execution_plans"],
                        "compiled execution plans",
                    )[child.node_id],
                    "compiled execution plan",
                )
            compiled_identity = require_mapping(
                compiled["identity"], "compiled plan identity"
            )
            persisted = subprocess.run(
                [str(producer), "persist-binding"],
                input=json.dumps(
                    {
                        "request": child.payload,
                        "artifact_set_digest": compiled_identity[
                            "model_artifact_set_sha256"
                        ],
                        "data_root": str(tmp_path / "runtime" / child.node_id),
                    },
                    separators=(",", ":"),
                )
                + "\n",
                text=True,
                capture_output=True,
                check=False,
            )
            assert persisted.returncode == 0, persisted.stderr
            produced = require_mapping(
                json.loads(persisted.stdout), "observation wire probe output"
            )
            evidence = require_mapping(
                require_mapping(produced["evidence"], "start result")["evidence"],
                "start evidence",
            )
            previous = captured.get(child.node_id)
            if previous is not None:
                assert previous["binding"] == produced["binding"]
            else:
                captured[child.node_id] = produced
            service.record_node_result(
                started.id, child.node_id, succeeded=True, evidence=evidence
            )
            completed.add(child.id)
    with sessions.begin() as session:
        node = session.get(AgentNode, node_ids[0])
        assert node is not None
        node.capabilities = list(node.capabilities or ()) + [
            "recipe.run.inspect.exact.v1",
            "recipe.stop",
        ]
    grant_seed = ed25519.Ed25519PrivateKey.from_private_bytes(bytes([29]) * 32)
    authority = HostRuntimeAuthorityService(
        sessions,
        HostHelperGrantIssuer(grant_seed, clock=clock),
        clock=clock,
    )
    presence = AgentPresenceService(
        sessions, ManagementAddressPolicy.parse("10.0.0.0/24"), clock=clock
    )
    operations = AgentJobService(sessions, clock=clock)
    roots = {name: tmp_path / name for name in ("artifacts", "source-bundles")}
    for root in roots.values():
        root.mkdir()
    services = AgentApiServices(
        enrollment=None,
        operations=operations,
        sessions=sessions,
        clock=clock,
        presence=presence,
        artifact_root=roots["artifacts"],
        source_bundles=SourceBundleStore(roots["source-bundles"]),
        fabric_policy=ManagementAddressPolicy.parse("192.168.100.0/24"),
        host_runtime_authority=authority,
    )
    app = create_app(
        jobs=_UnusedJobs(),
        tokens=TokenCodec(b"k" * 32),
        now=lambda: 0,
        agent=services,
        trusted_agent_proxy_auth=b"p" * 32,
    )
    assert set(captured) == set(node_ids)
    with sessions() as session:
        run = session.get(RecipeRun, started.owner_id)
        assert run is not None and isinstance(run.plan, dict)
        run_plan_document = require_mapping(run.plan, "run plan")
        plan_nodes = {
            item["node_id"]: item
            for item in require_sequence(run_plan_document["nodes"], "run plan nodes")
            if isinstance(item, dict)
        }
    assert set(plan_nodes) == set(node_ids)
    for node_id, produced in captured.items():
        binding = require_mapping(produced["binding"], "recipe run observation")
        assert binding["run_id"] == started.owner_id
        assert binding["endpoint_owner"] is plan_nodes[node_id]["endpoint_owner"]
    result = (
        app,
        sessions,
        started.owner_id,
        started.id,
        node_ids[0],
        grant_seed.public_key().public_bytes_raw(),
        captured[node_ids[0]],
    )
    if include_recovery:
        return (*result, service, _queue, mapping_id, node_ids)
    return result


def _submit_observation(
    app,
    sessions,
    *,
    identity,
    recipe_observation_wire_probe,
    observed_at,
    process_running: bool = True,
    endpoint_ready: bool | None = None,
    unassigned_run_ids: tuple[str, ...] = (),
):
    from fastapi.testclient import TestClient

    headers = {
        "x-vonk-agent-node": identity["node_id"],
        "x-vonk-agent-serial": "serial-0",
        "x-vonk-agent-fingerprint": "fingerprint-0",
        "x-vonk-agent-verified": "1",
        "x-vonk-agent-proxy-auth": "p" * 32,
        "x-vonk-agent-source": "10.0.0.42",
    }
    run = {
        "run_id": identity["run_id"],
        "run_generation": identity["run_generation"],
        "process_running": process_running,
        "endpoint_ready": (
            endpoint_ready
            if endpoint_ready is not None
            else True
            if identity["endpoint_owner"]
            else None
        ),
    }
    # A report can race a run leaving this node; such an item is not
    # evidence for any current run and must not discard its siblings.
    runs = [run, *({**run, "run_id": other} for other in unassigned_run_ids)]
    rust = subprocess.run(
        [str(recipe_observation_wire_probe), "serialize"],
        input=json.dumps(
            {"observed_at": observed_at.isoformat(), "runs": runs},
            separators=(",", ":"),
        )
        + "\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert rust.returncode == 0, rust.stderr
    envelope = json.loads(rust.stdout)
    assert RecipeRunObservationsWire.parse(envelope).runs[0].run_id == run["run_id"]
    with TestClient(app) as client:
        consumed = client.post(
            "/agent/recipe-runs/observations", headers=headers, json=envelope
        )
        assert consumed.status_code == 204, consumed.text
    return envelope, headers


def _persisted_recipe_start_evidence(
    sessions,
    *,
    producer: Path,
    node_id: str,
    payload: Mapping[str, object],
    data_root: Path,
) -> Mapping[str, object]:
    with sessions() as session:
        run = session.get(RecipeRun, payload["run_id"])
        assert run is not None
        installation = session.get(RecipeInstallation, run.installation_id)
        assert installation is not None
        installation_plan = require_mapping(installation.plan, "installation plan")
        compiled = require_mapping(
            require_mapping(
                installation_plan["compiled_execution_plans"],
                "compiled execution plans",
            )[node_id],
            "compiled execution plan",
        )
    compiled_identity = require_mapping(compiled["identity"], "compiled identity")
    persisted = subprocess.run(
        [str(producer), "persist-binding"],
        input=json.dumps(
            {
                "request": payload,
                "artifact_set_digest": compiled_identity["model_artifact_set_sha256"],
                "data_root": str(data_root),
            },
            separators=(",", ":"),
        )
        + "\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert persisted.returncode == 0, persisted.stderr
    return require_mapping(json.loads(persisted.stdout), "persisted start binding")


def _absent_singleton(
    tmp_path: Path,
    *,
    recipe_observation_wire_probe: Path,
    engine: Engine,
    hook_phase: Literal["pre_start", "post_stop"] | None = None,
):
    now = [NOW]

    def persistent_lifecycle(document: dict[str, object]) -> None:
        runtime = document["runtime"]
        assert isinstance(runtime, dict)
        runtime["lifecycle"] = {
            "pre_start": [["/bin/true"]] if hook_phase == "pre_start" else [],
            "post_stop": [["/bin/true"]] if hook_phase == "post_stop" else [],
            "stop_timeout_seconds": 30,
        }

    (
        app,
        sessions,
        run_id,
        original_start_id,
        node_id,
        grant_public_key,
        produced,
        service,
        queue,
        mapping_id,
        node_ids,
    ) = _production_controller_app(
        tmp_path,
        nodes=1,
        producer=recipe_observation_wire_probe,
        clock=lambda: now[0],
        engine=engine,
        include_recovery=True,
        recipe_transform=persistent_lifecycle,
    )
    binding = dict(
        require_mapping(produced["binding"], "recipe run inspection binding")
    )
    identity = {"node_id": node_id, **binding}
    now[0] = NOW + timedelta(seconds=1)
    _submit_observation(
        app,
        sessions,
        identity=identity,
        recipe_observation_wire_probe=recipe_observation_wire_probe,
        observed_at=now[0],
    )
    bound_service, routes = bind_route_publications(
        sessions, service, ConcurrentPublisher()
    )
    bound_service._clock = lambda: now[0]
    routes._clock = lambda: now[0]
    now[0] = NOW + timedelta(seconds=2)
    routes.publish_run(run_id)
    now[0] = NOW + timedelta(seconds=3)
    _submit_observation(
        app,
        sessions,
        identity=identity,
        recipe_observation_wire_probe=recipe_observation_wire_probe,
        observed_at=now[0],
        process_running=False,
        endpoint_ready=False,
    )
    return (
        now,
        app,
        sessions,
        run_id,
        original_start_id,
        node_id,
        grant_public_key,
        binding,
        service,
        queue,
        mapping_id,
        node_ids,
        bound_service,
        routes,
    )


@pytest.mark.parametrize("nodes", [1, 2])
def test_production_start_rust_observation_and_controller_consume(
    tmp_path: Path,
    recipe_observation_wire_probe: Path,
    nodes: int,
) -> None:
    """Connect the Controller start evidence, the Rust report and the consume route."""
    from fastapi.testclient import TestClient

    app, sessions, run_id, start_job_id, node_id, _grant_public_key, produced = (
        _production_controller_app(
            tmp_path, nodes=nodes, producer=recipe_observation_wire_probe
        )
    )
    binding = require_mapping(produced["binding"], "recipe run observation")
    identity = {"node_id": node_id, **binding}
    with sessions() as session:
        job = session.get(Job, start_job_id)
        assert job is not None and isinstance(job.result, dict)
        launch = require_mapping(
            require_mapping(job.result["launch_evidence"], "launch evidence")[node_id],
            "node launch evidence",
        )
        assert (
            launch == require_mapping(produced["evidence"], "start result")["evidence"]
        )
        run = session.get(RecipeRun, run_id)
        assert run is not None
        assert binding["run_id"] == run.id
        assert binding["run_generation"] == run.run_generation
    observed_at = NOW + timedelta(seconds=1)
    envelope, headers = _submit_observation(
        app,
        sessions,
        identity=identity,
        recipe_observation_wire_probe=recipe_observation_wire_probe,
        observed_at=observed_at,
        unassigned_run_ids=(str(uuid.uuid4()),),
    )
    with sessions() as session:
        node = session.query(RunNode).filter_by(run_id=run_id, node_id=node_id).one()
        assert node.observed_run_generation == identity["run_generation"]
        assert node.state == "running"
        assert node.observation_observed_at is not None
        assert node.observation_observed_at.replace(tzinfo=UTC) == observed_at
    with TestClient(app) as client:
        stale_generation = json.loads(json.dumps(envelope))
        stale_generation["runs"] = [
            {**envelope["runs"][0], "run_generation": identity["run_generation"] + 1}
        ]
        rejected = client.post(
            "/agent/recipe-runs/observations", headers=headers, json=stale_generation
        )
        assert rejected.status_code == 422
        assert "generation is stale" in rejected.json()["detail"]
    with sessions.begin() as session:
        node = session.query(RunNode).filter_by(run_id=run_id, node_id=node_id).one()
        node.state = "failed"
    with TestClient(app) as client:
        client.post("/agent/recipe-runs/observations", headers=headers, json=envelope)
    with sessions() as session:
        node = session.query(RunNode).filter_by(run_id=run_id, node_id=node_id).one()
        assert node.state == "failed"


def test_singleton_absence_reboots_through_new_controller_processes(
    tmp_path: Path,
    recipe_observation_wire_probe: Path,
    postgres_engine,
) -> None:
    """A stored exact absence drives one Stop then an exact next-generation Start."""

    from vonk_control.distributed_recovery import _proves_fresh_absence
    from vonk_control.models import ResourceReservation

    (
        now,
        app,
        sessions,
        run_id,
        original_start_id,
        node_id,
        _grant_public_key,
        binding,
        _service,
        queue,
        _mapping_id,
        node_ids,
        bound_service,
        routes,
    ) = _absent_singleton(
        tmp_path,
        recipe_observation_wire_probe=recipe_observation_wire_probe,
        engine=postgres_engine,
    )
    identity = {"node_id": node_id, **binding}
    with sessions() as session:
        stored_run = session.get(RecipeRun, run_id)
        stored_node = session.query(RunNode).filter_by(run_id=run_id).one()
        assert stored_run is not None
        assert stored_node.observation_process_running is False
        assert stored_node.observation_observed_at == now[0]
        assert _proves_fresh_absence(stored_run, stored_node, now[0])
        assert not _proves_fresh_absence(
            stored_run, stored_node, now[0] + timedelta(hours=1)
        )
        accepted_plan_digest = stored_run.plan_digest
        accepted_installation_id = stored_run.installation_id
        original_start = session.get(Job, original_start_id)
        assert original_start is not None and original_start.state == "succeeded"
        original_start_operation = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == original_start_id,
                AgentOperation.node_id == node_id,
            )
        )
        assert original_start_operation is not None
        accepted_start_payload = dict(original_start_operation.payload)
        assert stored_node.state == "failed"

    # Withdraw the route before restarting the two controller worker processes.
    now[0] = NOW + timedelta(seconds=4)
    routes.withdraw_run(run_id)
    database_url = postgres_engine.url.render_as_string(hide_password=False)
    workers = [
        multiprocessing.get_context("spawn").Process(
            target=_singleton_recovery_process_tick,
            args=(database_url, now[0].isoformat()),
        )
        for _ in range(2)
    ]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=30)
        assert all(not worker.is_alive() for worker in workers)
        exitcodes = tuple(worker.exitcode for worker in workers)
        assert all(exitcode is not None for exitcode in exitcodes)
        assert sorted(exitcode for exitcode in exitcodes if exitcode is not None) == [
            23,
            24,
        ]
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
            worker.close()

    with sessions() as session:
        recovery_stops = tuple(
            session.scalars(
                select(Job).where(
                    Job.kind == "recipe.stop",
                    Job.payload["owner_id"].as_string() == run_id,
                    Job.payload["recovery"].is_not(None),
                )
            )
        )
        run = session.get(RecipeRun, run_id)
        node = session.query(RunNode).filter_by(run_id=run_id).one()
        assert len(recovery_stops) == 1
        stop = recovery_stops[0]
        assert stop.actor == "system:singleton-recovery"
        assert stop.payload["workload_intent_ordinal"] == 2
        assert run is not None and run.state == "stopping"
        assert run.run_generation == 2
        assert node.observed_run_generation is None
        assert node.observation_process_running is None
        claims = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == run_id,
                )
            )
        )
        assert claims and all(claim.state == "active" for claim in claims)

    bound_service.record_node_result(
        stop.id, node_ids[0], succeeded=True, evidence={"stopped": True}
    )
    with sessions() as session:
        completed_stop = session.get(Job, stop.id)
        assert completed_stop is not None and completed_stop.state == "succeeded"
        recovery_context = require_mapping(
            completed_stop.payload["recovery"], "singleton Stop continuation"
        )
        expected_stop_request_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "vonk:singleton-recovery-stop:"
                f"{run_id}:2:{recovery_context['deadline']}",
            )
        )
        assert completed_stop.request_id == expected_stop_request_id
        assert (
            completed_stop.payload_digest
            == hashlib.sha256(canonical_message(completed_stop.payload)).hexdigest()
        )
        recovery_starts = tuple(
            session.scalars(
                select(Job).where(
                    Job.kind == "recipe.start",
                    Job.payload["owner_id"].as_string() == run_id,
                    Job.payload["recovery"].is_not(None),
                )
            )
        )
        assert len(recovery_starts) == 1
        recovery_start = recovery_starts[0]
        assert recovery_start.state == "running"
        start_child = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == recovery_start.id,
                AgentOperation.node_id == node_ids[0],
            )
        )
        assert start_child is not None
        for field in (
            "installation_id",
            "recipe_revision_id",
            "recipe_content_sha256",
            "image_digest",
            "plan_digest",
            "compiled_execution_plan",
        ):
            assert start_child.payload[field] == accepted_start_payload[field]
    recovery_produced = _persisted_recipe_start_evidence(
        sessions,
        producer=recipe_observation_wire_probe,
        node_id=node_ids[0],
        payload=start_child.payload,
        data_root=tmp_path / "runtime" / "recovery-gen-2" / node_ids[0],
    )
    recovery_evidence = require_mapping(
        require_mapping(recovery_produced["evidence"], "recovery start result")[
            "evidence"
        ],
        "recovery start evidence",
    )
    bound_service.record_node_result(
        recovery_start.id,
        node_ids[0],
        succeeded=True,
        evidence=recovery_evidence,
    )
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.state == "running"
        assert run.run_generation == 2
        assert run.route_state == "pending"
        assert run.installation_id == accepted_installation_id
        assert run.plan_digest == accepted_plan_digest
        claims = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == run_id,
                )
            )
        )
        assert claims and all(claim.state == "active" for claim in claims)

    # A current-generation running observation allows the route to
    # publish again after the exact accepted image and plan restart.
    binding = dict(
        require_mapping(recovery_produced["binding"], "recovery inspection binding")
    )
    identity = {"node_id": node_id, **binding}
    now[0] = NOW + timedelta(seconds=5)
    _submit_observation(
        app,
        sessions,
        identity=identity,
        recipe_observation_wire_probe=recipe_observation_wire_probe,
        observed_at=now[0],
    )
    now[0] = NOW + timedelta(seconds=6)
    routes.publish_run(run_id)
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        node = session.query(RunNode).filter_by(run_id=run_id).one()
        assert run is not None and run.route_state == "published"
        assert run.state == "running" and run.run_generation == 2
        assert node.observation_process_running is True

    now[0] = NOW + timedelta(seconds=7)
    _submit_observation(
        app,
        sessions,
        identity=identity,
        recipe_observation_wire_probe=recipe_observation_wire_probe,
        observed_at=now[0],
        process_running=False,
        endpoint_ready=False,
    )
    now[0] = NOW + timedelta(seconds=8)
    recovery = DistributedRecoveryCoordinator(
        sessions,
        routes=routes,
        agent_jobs=queue,
        clock=lambda: now[0],
        recovery_run_stops=bound_service,
        singleton_start_timeout_seconds=60,
    )
    assert recovery.tick() is True
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        node = session.query(RunNode).filter_by(run_id=run_id).one()
        recovery_stops = tuple(
            session.scalars(
                select(Job).where(
                    Job.kind == "recipe.stop",
                    Job.payload["owner_id"].as_string() == run_id,
                    Job.payload["recovery"].is_not(None),
                )
            )
        )
        claims = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == run_id,
                )
            )
        )
        assert len(recovery_stops) == 2
        assert sum(stop.state == "running" for stop in recovery_stops) == 1
        assert run is not None and run.state == "stopping"
        assert run.run_generation == 3
        assert node.observed_run_generation is None
        assert node.observation_process_running is None
    assert claims and all(claim.state == "active" for claim in claims)


def test_singleton_recovery_stop_grant_survives_start_deadline(
    tmp_path: Path,
    recipe_observation_wire_probe: Path,
    postgres_engine,
) -> None:
    """An elapsed future-Start deadline cannot refuse exact old-run cleanup."""

    (
        now,
        _app,
        sessions,
        run_id,
        _start_id,
        node_id,
        _grant_public_key,
        _binding,
        _service,
        queue,
        _mapping_id,
        _node_ids,
        bound_service,
        routes,
    ) = _absent_singleton(
        tmp_path,
        recipe_observation_wire_probe=recipe_observation_wire_probe,
        engine=postgres_engine,
    )
    now[0] = NOW + timedelta(seconds=4)
    recovery = DistributedRecoveryCoordinator(
        sessions,
        routes=routes,
        agent_jobs=queue,
        clock=lambda: now[0],
        recovery_run_stops=bound_service,
        singleton_start_timeout_seconds=60,
    )
    assert recovery.tick()

    with sessions() as session:
        stop = session.scalar(
            select(Job).where(
                Job.kind == "recipe.stop",
                Job.payload["owner_id"].as_string() == run_id,
                Job.payload["recovery"].is_not(None),
            )
        )
        assert stop is not None
        recovery_marker = stop.payload.get("recovery")
        assert isinstance(recovery_marker, Mapping)
        deadline_text = recovery_marker.get("deadline")
        assert isinstance(deadline_text, str)
        deadline = datetime.fromisoformat(deadline_text)
    grant_now = deadline + timedelta(seconds=1)
    now[0] = grant_now

    claim, payload, _grant = _issue_exact_stop_grant(
        sessions,
        node_id=node_id,
        certificate_serial="serial-0",
        grant_now=grant_now,
    )

    with sessions() as session:
        parent = session.get(Job, stop.id)
        run = session.get(RecipeRun, run_id)
        assert parent is not None and run is not None
        assert claim.job_id == parent.id
        assert payload.run_generation + 1 == run.run_generation
        assert parent.actor == "system:singleton-recovery"
        assert parent.payload["plan_digest"] != run.plan_digest
        assert parent.authority_revision == run.plan_digest.removeprefix("sha256:")


def test_newer_workload_intent_wins_over_singleton_reboot_recovery(
    tmp_path: Path,
    recipe_observation_wire_probe: Path,
    postgres_engine,
) -> None:
    from vonk_control.distributed_recovery import DistributedRecoveryCoordinator
    from vonk_control.models import AgentNode, ResourceReservation

    (
        now,
        _app,
        sessions,
        run_id,
        _original_start_id,
        node_id,
        _grant_public_key,
        _binding,
        _service,
        queue,
        _mapping_id,
        _node_ids,
        bound_service,
        routes,
    ) = _absent_singleton(
        tmp_path,
        recipe_observation_wire_probe=recipe_observation_wire_probe,
        engine=postgres_engine,
    )
    with sessions.begin() as session:
        node = session.get(AgentNode, node_id, with_for_update=True)
        assert node is not None
        node.workload_intent_ordinal += 1
    now[0] = NOW + timedelta(seconds=4)
    recovery = DistributedRecoveryCoordinator(
        sessions,
        routes=routes,
        agent_jobs=queue,
        clock=lambda: now[0],
        recovery_run_stops=bound_service,
        singleton_start_timeout_seconds=60,
    )

    assert recovery.tick() is True

    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        node = session.get(AgentNode, node_id)
        claims = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == run_id,
                )
            )
        )
        assert run is not None and run.state == "failed"
        assert run.route_state == "withdrawn"
        assert "newer workload intent" in (run.route_error or "")
        assert run.run_generation == 2
        assert node is not None and node.workload_intent_ordinal == 3
        assert claims and all(claim.state == "active" for claim in claims)
        assert not session.scalar(
            select(Job.id).where(
                Job.kind == "recipe.stop",
                Job.payload["owner_id"].as_string() == run_id,
                Job.payload["recovery"].is_not(None),
            )
        )


def test_uncertain_singleton_recovery_stop_retains_run_claims(
    tmp_path: Path,
    recipe_observation_wire_probe: Path,
    postgres_engine,
) -> None:
    from vonk_control.distributed_recovery import DistributedRecoveryCoordinator
    from vonk_control.models import ResourceReservation

    (
        now,
        _app,
        sessions,
        run_id,
        _original_start_id,
        _node_id,
        _grant_public_key,
        _binding,
        _service,
        queue,
        _mapping_id,
        node_ids,
        bound_service,
        routes,
    ) = _absent_singleton(
        tmp_path,
        recipe_observation_wire_probe=recipe_observation_wire_probe,
        engine=postgres_engine,
    )
    now[0] = NOW + timedelta(seconds=4)
    recovery = DistributedRecoveryCoordinator(
        sessions,
        routes=routes,
        agent_jobs=queue,
        clock=lambda: now[0],
        recovery_run_stops=bound_service,
        singleton_start_timeout_seconds=60,
    )
    assert recovery.tick() is True
    with sessions() as session:
        stop = session.scalar(
            select(Job).where(
                Job.kind == "recipe.stop",
                Job.payload["owner_id"].as_string() == run_id,
                Job.payload["recovery"].is_not(None),
            )
        )
        assert stop is not None
    bound_service.record_node_result(
        stop.id,
        node_ids[0],
        succeeded=False,
        evidence={
            "reason": "host Stop outcome is uncertain after reboot",
            "failure_kind": "uncertain-effect",
            "uncertain": True,
        },
    )
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        claims = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == run_id,
                )
            )
        )
        assert run is not None and run.state == "failed"
        assert run.route_state == "withdrawn"
        assert claims and all(claim.state == "active" for claim in claims)


def test_singleton_recovery_enters_cooldown_then_resumes_automatically(
    tmp_path: Path,
    recipe_observation_wire_probe: Path,
    postgres_engine,
) -> None:
    (
        now,
        _app,
        sessions,
        run_id,
        _original_start_id,
        _node_id,
        _grant_public_key,
        _binding,
        _service,
        queue,
        _mapping_id,
        _node_ids,
        bound_service,
        routes,
    ) = _absent_singleton(
        tmp_path,
        recipe_observation_wire_probe=recipe_observation_wire_probe,
        engine=postgres_engine,
    )
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id, with_for_update=True)
        assert run is not None
        run.recovery_attempts = 5
        run.route_next_attempt_at = None
    recovery = DistributedRecoveryCoordinator(
        sessions,
        routes=routes,
        agent_jobs=queue,
        clock=lambda: now[0],
        recovery_run_stops=bound_service,
        singleton_start_timeout_seconds=60,
    )

    assert recovery.tick() is True
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        assert run.recovery_attempts == 6
        assert run.route_next_attempt_at == now[0] + timedelta(minutes=5)
        assert "degraded" in (run.route_error or "")
    now[0] += timedelta(minutes=5)

    assert recovery.tick() is True
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.recovery_attempts == 0
        assert run.route_next_attempt_at == now[0] + timedelta(seconds=5)
        assert "fresh exact absence" in (run.route_error or "")
        assert not session.scalar(
            select(Job.id).where(
                Job.kind == "recipe.start",
                Job.payload["owner_id"].as_string() == run_id,
                Job.payload["recovery"].is_not(None),
            )
        )


def test_stale_singleton_absence_can_be_refreshed_read_only_and_recovered(
    tmp_path: Path,
    recipe_observation_wire_probe: Path,
    postgres_engine,
) -> None:
    from vonk_control.distributed_recovery import DistributedRecoveryCoordinator

    (
        now,
        app,
        sessions,
        run_id,
        _original_start_id,
        node_id,
        _grant_public_key,
        binding,
        _service,
        queue,
        _mapping_id,
        _node_ids,
        bound_service,
        routes,
    ) = _absent_singleton(
        tmp_path,
        recipe_observation_wire_probe=recipe_observation_wire_probe,
        engine=postgres_engine,
    )
    identity = {"node_id": node_id, **binding}
    recovery = DistributedRecoveryCoordinator(
        sessions,
        routes=routes,
        agent_jobs=queue,
        clock=lambda: now[0],
        recovery_run_stops=bound_service,
        singleton_start_timeout_seconds=60,
    )

    # The first exact proof aged beyond the route evidence window. The run
    # stays current, but the coordinator must wait for a new inspection.
    now[0] = NOW + timedelta(seconds=124)
    assert recovery.tick() is True
    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        node = session.query(RunNode).filter_by(run_id=run_id).one()
        assert run is not None and run.state == "running"
        assert run.route_state == "withdrawn"
        assert "fresh exact absence" in (run.route_error or "")
        assert run.route_next_attempt_at == now[0] + timedelta(seconds=5)
        assert node.state == "failed"

    now[0] = NOW + timedelta(seconds=125)
    _submit_observation(
        app,
        sessions,
        identity=identity,
        recipe_observation_wire_probe=recipe_observation_wire_probe,
        observed_at=now[0],
        process_running=False,
        endpoint_ready=False,
    )
    with sessions.begin() as session:
        presence = session.get(AgentPresence, node_id)
        assert presence is not None
        presence.observed_at = now[0]
    now[0] = NOW + timedelta(seconds=126)

    assert recovery.tick() is True

    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        stop = session.scalar(
            select(Job).where(
                Job.kind == "recipe.stop",
                Job.payload["owner_id"].as_string() == run_id,
                Job.payload["recovery"].is_not(None),
            )
        )
        assert stop is not None and stop.state == "running"
        assert run is not None and run.state == "stopping"
        assert run.run_generation == 2


def test_stale_presence_waits_then_recovery_resumes_without_new_run(
    tmp_path: Path,
    recipe_observation_wire_probe: Path,
    postgres_engine,
) -> None:
    from vonk_control.distributed_recovery import DistributedRecoveryCoordinator

    (
        now,
        _app,
        sessions,
        run_id,
        _original_start_id,
        node_id,
        _grant_public_key,
        _binding,
        _service,
        queue,
        _mapping_id,
        _node_ids,
        bound_service,
        routes,
    ) = _absent_singleton(
        tmp_path,
        recipe_observation_wire_probe=recipe_observation_wire_probe,
        engine=postgres_engine,
    )
    with sessions.begin() as session:
        presence = session.get(AgentPresence, node_id)
        assert presence is not None
        presence.observed_at = NOW - timedelta(minutes=10)
    now[0] = NOW + timedelta(seconds=4)
    recovery = DistributedRecoveryCoordinator(
        sessions,
        routes=routes,
        agent_jobs=queue,
        clock=lambda: now[0],
        recovery_run_stops=bound_service,
        singleton_start_timeout_seconds=60,
    )

    assert recovery.tick() is True

    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None and run.state == "running"
        assert run.route_state == "withdrawn"
        assert "fresh Controller-observed Spark presence" in (run.route_error or "")
        assert run.route_next_attempt_at == now[0] + timedelta(seconds=5)
        assert not session.scalar(
            select(Job.id).where(
                Job.kind == "recipe.stop",
                Job.payload["owner_id"].as_string() == run_id,
                Job.payload["recovery"].is_not(None),
            )
        )

    now[0] = NOW + timedelta(seconds=10)
    with sessions.begin() as session:
        presence = session.get(AgentPresence, node_id)
        assert presence is not None
        presence.observed_at = now[0]

    assert recovery.tick() is True

    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        stop = session.scalar(
            select(Job).where(
                Job.kind == "recipe.stop",
                Job.payload["owner_id"].as_string() == run_id,
                Job.payload["recovery"].is_not(None),
            )
        )
        assert run is not None and run.state == "stopping"
        assert stop is not None and stop.state == "running"


def test_stale_singleton_wait_does_not_starve_later_recovery_or_hot_loop(
    tmp_path: Path,
    recipe_observation_wire_probe: Path,
    postgres_engine,
) -> None:
    from vonk_control.distributed_recovery import DistributedRecoveryCoordinator

    (
        now,
        _app,
        sessions,
        run_id,
        _original_start_id,
        node_id,
        _grant_public_key,
        _binding,
        _service,
        queue,
        _mapping_id,
        _node_ids,
        bound_service,
        routes,
    ) = _absent_singleton(
        tmp_path,
        recipe_observation_wire_probe=recipe_observation_wire_probe,
        engine=postgres_engine,
    )
    wait_reason = "singleton recovery waits for a fresh exact absence observation"
    stale_id = str(uuid.uuid4())
    with sessions.begin() as session:
        current = session.get(RecipeRun, run_id)
        current_node = session.query(RunNode).filter_by(run_id=run_id).one()
        assert current is not None
        stale_plan = json.loads(canonical_message(current.plan))
        stale_plan["alias"] = "older-unobserved-singleton"
        session.add(
            RecipeRun(
                id=stale_id,
                installation_id=current.installation_id,
                mapping_id=current.mapping_id,
                mapping_generation=current.mapping_generation,
                run_generation=1,
                observation_deadline_at=None,
                alias="older-unobserved-singleton",
                plan_digest=current.plan_digest,
                plan=stale_plan,
                state="running",
                route_state="withdrawn",
                route_error=wait_reason,
                route_attempts=0,
                route_next_attempt_at=None,
                actor="test",
                created_at=NOW - timedelta(seconds=1),
                updated_at=NOW - timedelta(seconds=1),
            )
        )
        session.add(
            RunNode(
                run_id=stale_id,
                node_id=node_id,
                rank=current_node.rank,
                role=current_node.role,
                state="failed",
                port=current_node.port,
                reserved_memory_bytes=current_node.reserved_memory_bytes,
                observed_memory_bytes=None,
                endpoint=None,
                observed_run_generation=None,
                observation_process_running=None,
                observation_observed_at=None,
                observation_endpoint_ready=None,
                updated_at=NOW - timedelta(seconds=1),
            )
        )
    now[0] = NOW + timedelta(seconds=4)
    recovery = DistributedRecoveryCoordinator(
        sessions,
        routes=routes,
        agent_jobs=queue,
        clock=lambda: now[0],
        recovery_run_stops=bound_service,
        singleton_start_timeout_seconds=60,
    )

    assert recovery.tick() is True

    with sessions() as session:
        stale = session.get(RecipeRun, stale_id)
        later = session.get(RecipeRun, run_id)
        stop = session.scalar(
            select(Job).where(
                Job.kind == "recipe.stop",
                Job.payload["owner_id"].as_string() == run_id,
                Job.payload["recovery"].is_not(None),
            )
        )
        assert stale is not None
        assert stale.route_error == wait_reason
        assert stale.route_next_attempt_at == now[0] + timedelta(seconds=5)
        assert later is not None and later.state == "stopping"
        assert stop is not None and stop.state == "running"
        first_wait_updated_at = stale.updated_at
        first_wait_due_at = stale.route_next_attempt_at

    assert recovery.tick() is False

    with sessions() as session:
        stale = session.get(RecipeRun, stale_id)
        assert stale is not None
        assert stale.updated_at == first_wait_updated_at
        assert stale.route_next_attempt_at == first_wait_due_at


@pytest.mark.parametrize("hook_phase", ["pre_start", "post_stop"])
def test_singleton_recovery_fails_closed_before_effects_for_lifecycle_hooks(
    tmp_path: Path,
    recipe_observation_wire_probe: Path,
    postgres_engine,
    hook_phase: Literal["pre_start", "post_stop"],
) -> None:
    from vonk_control.distributed_recovery import DistributedRecoveryCoordinator
    from vonk_control.models import ResourceReservation

    (
        now,
        _app,
        sessions,
        run_id,
        _original_start_id,
        _node_id,
        _grant_public_key,
        _binding,
        _service,
        queue,
        _mapping_id,
        _node_ids,
        bound_service,
        routes,
    ) = _absent_singleton(
        tmp_path,
        recipe_observation_wire_probe=recipe_observation_wire_probe,
        engine=postgres_engine,
        hook_phase=hook_phase,
    )
    now[0] = NOW + timedelta(seconds=4)
    recovery = DistributedRecoveryCoordinator(
        sessions,
        routes=routes,
        agent_jobs=queue,
        clock=lambda: now[0],
        recovery_run_stops=bound_service,
        singleton_start_timeout_seconds=60,
    )

    assert recovery.tick() is True

    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        claims = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == run_id,
                )
            )
        )
        assert run is not None and run.state == "failed"
        assert run.route_state == "withdrawn"
        assert f"{hook_phase} hook" in (run.route_error or "")
        assert claims and all(claim.state == "active" for claim in claims)
        assert not session.scalar(
            select(Job.id).where(
                Job.kind.in_({"recipe.stop", "recipe.start"}),
                Job.payload["owner_id"].as_string() == run_id,
                Job.payload["recovery"].is_not(None),
            )
        )


@pytest.mark.parametrize("authority_copy", ["installed", "accepted-start"])
def test_singleton_recovery_checks_compiled_lifecycle_authority(
    tmp_path: Path,
    recipe_observation_wire_probe: Path,
    postgres_engine,
    authority_copy: Literal["installed", "accepted-start"],
) -> None:
    from vonk_control.distributed_recovery import DistributedRecoveryCoordinator
    from vonk_control.models import ResourceReservation

    (
        now,
        _app,
        sessions,
        run_id,
        original_start_id,
        node_id,
        _grant_public_key,
        _binding,
        _service,
        queue,
        _mapping_id,
        _node_ids,
        bound_service,
        routes,
    ) = _absent_singleton(
        tmp_path,
        recipe_observation_wire_probe=recipe_observation_wire_probe,
        engine=postgres_engine,
    )
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        if authority_copy == "installed":
            installation = session.get(RecipeInstallation, run.installation_id)
            assert installation is not None
            install_plan = json.loads(canonical_message(installation.plan))
            compiled = install_plan["compiled_execution_plans"][node_id]
            compiled["lifecycle"]["pre_start"] = [["/bin/true"]]
            installation.plan = install_plan
        else:
            start = session.get(Job, original_start_id)
            assert start is not None
            payload = json.loads(canonical_message(start.payload))
            item = payload["phases"][0][0]
            start_payload = item["payload"]
            start_payload["compiled_execution_plan"]["lifecycle"]["pre_start"] = [
                ["/bin/true"]
            ]
            child = session.get(AgentOperation, item["operation_id"])
            assert child is not None
            start.payload = payload
            start.payload_digest = hashlib.sha256(
                canonical_message(payload)
            ).hexdigest()
            child.payload = start_payload
            child.payload_digest = hashlib.sha256(
                canonical_message(start_payload)
            ).hexdigest()
    now[0] = NOW + timedelta(seconds=4)
    recovery = DistributedRecoveryCoordinator(
        sessions,
        routes=routes,
        agent_jobs=queue,
        clock=lambda: now[0],
        recovery_run_stops=bound_service,
        singleton_start_timeout_seconds=60,
    )

    assert recovery.tick() is True

    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        claims = tuple(
            session.scalars(
                select(ResourceReservation).where(
                    ResourceReservation.owner_kind == "run",
                    ResourceReservation.owner_id == run_id,
                )
            )
        )
        assert run is not None and run.state == "failed"
        assert run.route_state == "withdrawn"
        assert "lifecycle" in (run.route_error or "")
        assert claims and all(claim.state == "active" for claim in claims)
        assert not session.scalar(
            select(Job.id).where(
                Job.kind.in_({"recipe.stop", "recipe.start"}),
                Job.payload["owner_id"].as_string() == run_id,
                Job.payload["recovery"].is_not(None),
            )
        )


def test_singleton_recovery_refuses_per_node_start_for_another_plan(
    tmp_path: Path,
    recipe_observation_wire_probe: Path,
    postgres_engine,
) -> None:
    """The Job binds the run plan; the replayed per-node payload must too."""

    from vonk_control.distributed_recovery import DistributedRecoveryCoordinator

    (
        now,
        _app,
        sessions,
        run_id,
        original_start_id,
        _node_id,
        _grant_public_key,
        _binding,
        _service,
        queue,
        _mapping_id,
        _node_ids,
        bound_service,
        routes,
    ) = _absent_singleton(
        tmp_path,
        recipe_observation_wire_probe=recipe_observation_wire_probe,
        engine=postgres_engine,
    )
    with sessions.begin() as session:
        start = session.get(Job, original_start_id)
        assert start is not None
        payload = json.loads(canonical_message(start.payload))
        item = payload["phases"][0][0]
        start_payload = item["payload"]
        # Job-level binding stays exact; only the per-node payload differs.
        start_payload["plan_digest"] = "0" * 64
        child = session.get(AgentOperation, item["operation_id"])
        assert child is not None
        start.payload = payload
        start.payload_digest = hashlib.sha256(canonical_message(payload)).hexdigest()
        child.payload = start_payload
        child.payload_digest = hashlib.sha256(
            canonical_message(start_payload)
        ).hexdigest()
    now[0] = NOW + timedelta(seconds=4)
    recovery = DistributedRecoveryCoordinator(
        sessions,
        routes=routes,
        agent_jobs=queue,
        clock=lambda: now[0],
        recovery_run_stops=bound_service,
        singleton_start_timeout_seconds=60,
    )

    assert recovery.tick() is True

    with sessions() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        assert "accepted Start image authority is stale" in (run.route_error or "")
        assert not session.scalar(
            select(Job.id).where(
                Job.kind.in_({"recipe.stop", "recipe.start"}),
                Job.payload["owner_id"].as_string() == run_id,
                Job.payload["recovery"].is_not(None),
            )
        )


@pytest.mark.parametrize("schedule", [(120,), (121,), (0, 60, 121)])
def test_observation_deadline_applies_to_first_observation_not_renewal(
    tmp_path: Path,
    recipe_observation_wire_probe: Path,
    postgres_engine,
    schedule: tuple[int, ...],
) -> None:
    # Wrong implementation: every renewal overwrites updated_at, so a healthy
    # rank with timely evidence is killed at the initial grace deadline.
    now = [NOW]
    app, sessions, run_id, _job_id, node_id, _public_key, produced = (
        _production_controller_app(
            tmp_path,
            nodes=1,
            producer=recipe_observation_wire_probe,
            clock=lambda: now[0],
            engine=postgres_engine,
        )
    )
    binding = require_mapping(produced["binding"], "recipe run inspection binding")
    identity = {"node_id": node_id, **binding}
    first_late = schedule[0] > 120
    for elapsed in schedule:
        now[0] = NOW + timedelta(seconds=elapsed)
        _submit_observation(
            app,
            sessions,
            identity=identity,
            recipe_observation_wire_probe=recipe_observation_wire_probe,
            observed_at=now[0],
        )
    with sessions() as session:
        rank = session.query(RunNode).filter_by(run_id=run_id, node_id=node_id).one()
        assert rank.state == ("failed" if first_late else "running")
        assert rank.observed_run_generation == identity["run_generation"]
        assert rank.observation_observed_at is not None
    # The worker re-reads PostgreSQL after a process restart; it must preserve
    # timely, continuously renewed observations while publication is pending.
    if not first_late:

        class UnusedRoutes:
            def publish_run(self, run_id: str) -> object:
                raise AssertionError("expiry must not invoke route publication")

            def maintain(self, *, renew_before_seconds=10):
                raise AssertionError("expiry must not invoke route maintenance")

        worker = RecipeOperationWorker(sessions, UnusedRoutes(), clock=lambda: now[0])
        assert worker._expire_initial_observation_deadline() is False
        with sessions() as session:
            run = session.get(RecipeRun, run_id)
            assert run is not None and run.route_state == "pending"


@pytest.mark.parametrize("endpoint_ready", [None, True])
def test_rust_observation_json_is_consumed_by_the_controller_wire_model(
    recipe_observation_wire_probe: Path,
    endpoint_ready: bool | None,
) -> None:
    payload = {
        "run_id": str(uuid.uuid4()),
        "run_generation": 3,
        "process_running": True,
        "endpoint_ready": endpoint_ready,
    }
    completed = subprocess.run(
        [str(recipe_observation_wire_probe), "serialize"],
        input=json.dumps(
            {"observed_at": NOW.isoformat(), "runs": [payload]},
            separators=(",", ":"),
        )
        + "\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    output = [line for line in completed.stdout.splitlines() if line.strip()]
    assert len(output) == 1
    parsed = RecipeRunObservationsWire.parse(json.loads(output[0]))
    assert len(parsed.runs) == 1
    assert parsed.runs[0].endpoint_ready is endpoint_ready
    wire_observation = json.loads(output[0])["runs"][0]
    # Derive the missing-field cases from the canonical definition, including
    # the explicit null. Rust Option must not turn omission into an accepted
    # null that Python rejects.
    for field in RecipeRunObservationWire.model_json_schema()["required"]:
        incomplete = dict(wire_observation)
        del incomplete[field]
        with pytest.raises(ValidationError):
            RecipeRunObservationWire.model_validate(incomplete)
        rejected = subprocess.run(
            [str(recipe_observation_wire_probe), "parse"],
            input=json.dumps(incomplete) + "\n",
            text=True,
            capture_output=True,
            check=False,
        )
        assert rejected.returncode != 0, field
