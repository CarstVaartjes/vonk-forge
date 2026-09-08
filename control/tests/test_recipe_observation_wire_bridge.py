from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from pydantic import ValidationError
from sqlalchemy import select
from vonk_agent_protocol import (
    RecipeRunObservationsWire,
    RecipeRunObservationWire,
    SignedHostHelperGrant,
    canonical_message,
)
from vonk_control.agent_api import AgentApiServices
from vonk_control.agent_jobs import AgentJobService
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import TokenCodec
from vonk_control.host_helper_authority import (
    HostHelperGrantIssuer,
    HostRuntimeAuthorityService,
)
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    Job,
    RecipeInstallation,
    RecipeRun,
    RecipeRunObservationGrant,
    RunNode,
)
from vonk_control.presence import AgentPresenceService, ManagementAddressPolicy
from vonk_control.source_bundles import SourceBundleStore

from .test_agent_api import Jobs
from .test_recipe_operations import (
    NOW,
    installed_recipe,
    setup_services,
)


@pytest.fixture(scope="session")
def recipe_observation_wire_probe() -> Path:
    configured = os.environ.get("VONK_RECIPE_OBSERVATION_WIRE_PROBE")
    repository = Path(__file__).resolve().parents[2]
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = repository / path
        path = path.resolve()
    else:
        subprocess.run(
            [
                "cargo",
                "build",
                "--locked",
                "--package",
                "vonk-agent",
                "--example",
                "recipe_observation_wire_probe",
            ],
            cwd=repository,
            check=True,
        )
        target_root = Path(os.environ.get("CARGO_TARGET_DIR", repository / "target"))
        if not target_root.is_absolute():
            target_root = repository / target_root
        path = target_root / "debug" / "examples" / "recipe_observation_wire_probe"
    if not path.is_file() or not os.access(path, os.X_OK):
        raise AssertionError(f"observation wire probe is not executable: {path}")
    return path


@pytest.fixture(scope="session")
def host_helper_wire_probe() -> Path:
    configured = os.environ.get("VONK_HOST_HELPER_WIRE_PROBE")
    repository = Path(__file__).resolve().parents[2]
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = repository / path
        path = path.resolve()
    else:
        subprocess.run(
            [
                "cargo",
                "build",
                "--locked",
                "--package",
                "vonk-agent-helper",
                "--example",
                "host_helper_wire_probe",
            ],
            cwd=repository,
            check=True,
        )
        target_root = Path(os.environ.get("CARGO_TARGET_DIR", repository / "target"))
        if not target_root.is_absolute():
            target_root = repository / target_root
        path = target_root / "debug" / "examples" / "host_helper_wire_probe"
    if not path.is_file() or not os.access(path, os.X_OK):
        raise AssertionError(f"host helper wire probe is not executable: {path}")
    return path


def _production_controller_app(tmp_path: Path, *, nodes: int, producer: Path):
    sessions, service, _queue, mapping_id, build_id, node_ids = setup_services(
        tmp_path, nodes=nodes, distributed_lifecycle=nodes > 1
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
    captured: dict[str, dict[str, object]] = {}
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
            with sessions() as session:
                run = session.get(RecipeRun, started.owner_id)
                assert run is not None
                installation_row = session.get(RecipeInstallation, run.installation_id)
                assert installation_row is not None
                compiled = installation_row.plan["compiled_execution_plans"][
                    child.node_id
                ]
            persisted = subprocess.run(
                [str(producer), "persist-binding"],
                input=json.dumps(
                    {
                        "request": child.payload,
                        "artifact_set_digest": compiled["identity"][
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
            produced = json.loads(persisted.stdout)
            evidence = produced["evidence"]["evidence"]
            previous = captured.get(child.node_id)
            if previous is not None:
                assert previous["binding"] == produced["binding"]
            else:
                captured[child.node_id] = produced
            service.record_node_result(
                started.id, child.node_id, succeeded=True, evidence=evidence
            )
            completed.add(child.id)
    receipt_seed = ed25519.Ed25519PrivateKey.from_private_bytes(bytes([23]) * 32)
    with sessions.begin() as session:
        node = session.get(AgentNode, node_ids[0])
        assert node is not None
        node.capabilities = list(node.capabilities or ()) + [
            "recipe.run.inspect.exact.v1"
        ]
        node.observation_receipt_public_key = (
            receipt_seed.public_key().public_bytes_raw().hex()
        )
    grant_seed = ed25519.Ed25519PrivateKey.from_private_bytes(bytes([29]) * 32)
    authority = HostRuntimeAuthorityService(
        sessions,
        HostHelperGrantIssuer(grant_seed, clock=lambda: NOW),
        clock=lambda: NOW,
    )
    presence = AgentPresenceService(
        sessions, ManagementAddressPolicy.parse("10.0.0.0/24"), clock=lambda: NOW
    )
    operations = AgentJobService(sessions, clock=lambda: NOW)
    roots = {
        name: tmp_path / name
        for name in ("artifacts", "source-bundles", "tuf-metadata", "tuf-targets")
    }
    for root in roots.values():
        root.mkdir()
    services = AgentApiServices(
        enrollment=None,
        operations=operations,
        sessions=sessions,
        clock=lambda: NOW,
        presence=presence,
        artifact_root=roots["artifacts"],
        source_bundles=SourceBundleStore(roots["source-bundles"]),
        workload_tuf_metadata_root=roots["tuf-metadata"],
        workload_tuf_target_root=roots["tuf-targets"],
        fabric_policy=ManagementAddressPolicy.parse("192.168.100.0/24"),
        host_runtime_authority=authority,
    )
    app = create_app(
        jobs=Jobs(),
        tokens=TokenCodec(b"k" * 32),
        audits=MemoryAuditStore(),
        now=lambda: 0,
        agent=services,
        trusted_agent_proxy_auth=b"p" * 32,
    )
    assert set(captured) == set(node_ids)
    with sessions() as session:
        run = session.get(RecipeRun, started.owner_id)
        assert run is not None and isinstance(run.plan, dict)
        plan_nodes = {
            item["node_id"]: item
            for item in run.plan["nodes"]
            if isinstance(item, dict)
        }
    assert set(plan_nodes) == set(node_ids)
    if nodes == 1:
        binding = captured[node_ids[0]]["binding"]
        assert plan_nodes[node_ids[0]]["endpoint_owner"] is True
        assert binding["local_address"] is None
        assert binding["master_address"] is None
        assert binding["master_port"] is None
    else:
        owner = next(item for item in plan_nodes.values() if item["endpoint_owner"])
        assert sum(item["endpoint_owner"] for item in plan_nodes.values()) == 1
        for node_id, produced in captured.items():
            binding = produced["binding"]
            assert binding["local_address"] == plan_nodes[node_id]["fabric_address"]
            assert binding["master_address"] == owner["fabric_address"]
            assert binding["master_port"] == owner["rendezvous_port"]
    return (
        app,
        sessions,
        started.owner_id,
        started.id,
        node_ids[0],
        grant_seed.public_key().public_bytes_raw(),
        captured[node_ids[0]],
    )


@pytest.mark.parametrize("nodes", [1, 2])
@pytest.mark.parametrize("same_second", [False, True])
def test_production_start_grant_helper_receipt_rust_and_controller_consume(
    tmp_path: Path,
    recipe_observation_wire_probe: Path,
    host_helper_wire_probe: Path,
    nodes: int,
    same_second: bool,
) -> None:
    """Connect the Controller start evidence, typed helper, Rust wire, and consume route."""
    from fastapi.testclient import TestClient

    app, sessions, run_id, start_job_id, node_id, grant_public_key, produced = (
        _production_controller_app(
            tmp_path, nodes=nodes, producer=recipe_observation_wire_probe
        )
    )
    binding = produced["binding"]
    identity = {"schema_version": 1, "node_id": node_id, **binding}
    with sessions() as session:
        job = session.get(Job, start_job_id)
        assert job is not None and isinstance(job.result, dict)
        launch = job.result["launch_evidence"][node_id]
        assert launch == produced["evidence"]["evidence"]
        run = session.get(RecipeRun, run_id)
        installation = session.get(RecipeInstallation, binding["installation_id"])
        run_node = (
            session.query(RunNode).filter_by(run_id=run_id, node_id=node_id).one()
        )
        assert run is not None and installation is not None
        assert binding["run_id"] == run.id
        assert binding["installation_id"] == installation.id
        assert binding["recipe_revision_id"] == installation.recipe_revision_id
        assert binding["mapping_id"] == run.mapping_id
        assert binding["mapping_generation"] == run.mapping_generation
        assert binding["run_generation"] == run.run_generation
        assert binding["rank"] == run_node.rank
        assert binding["role"] == run_node.role
        assert binding["port"] == run_node.port
        assert binding["world_size"] == launch["world_size"]
        assert binding["recipe_content_sha256"] == launch["recipe_content_sha256"]
        assert f"sha256:{binding['image_digest']}" == launch["image_digest"]
        assert binding["artifact_set_digest"] == launch["artifact_set_digest"]
        assert binding["runtime_arguments_sha256"] == launch["runtime_arguments_sha256"]
        assert binding["local_address"] == launch.get("local_address")
        assert binding["master_address"] == launch.get("master_address")
        assert binding["master_port"] == launch.get("master_port")
    if same_second:
        with sessions.begin() as session:
            node = session.query(RunNode).filter_by(run_id=run_id, node_id=node_id).one()
            node.updated_at = NOW + timedelta(microseconds=500_000)
    operation_id = str(uuid.uuid4())
    fence = str(uuid.uuid4())
    request = {
        **identity,
        "job_id": run_id,
        "operation_id": operation_id,
        "attempt": identity["run_generation"],
        "fence": fence,
        "request_sha256": "d" * 64,
        "expires_in_seconds": 10,
    }
    headers = {
        "x-vonk-agent-node": node_id,
        "x-vonk-agent-serial": "serial-0",
        "x-vonk-agent-fingerprint": "fingerprint-0",
        "x-vonk-agent-verified": "1",
        "x-vonk-agent-proxy-auth": "p" * 32,
        "x-vonk-agent-source": "10.0.0.42",
    }
    with TestClient(app) as client:
        grant_response = client.post(
            "/agent/v1/recipe-runs/observation-grants", headers=headers, json=request
        )
        assert grant_response.status_code == 200, grant_response.text
        grant = SignedHostHelperGrant.parse(grant_response.json()["grant"])
        helper = subprocess.run(
            [str(host_helper_wire_probe)],
            input=canonical_message(grant.to_mapping()).decode() + "\n",
            text=True,
            capture_output=True,
            env={
                **os.environ,
                "VONK_HOST_HELPER_GRANT_PUBLIC_KEY": grant_public_key.hex(),
                "VONK_HOST_HELPER_WIRE_NOW": str(int(NOW.timestamp()) - int(same_second)),
            },
            check=False,
        )
        assert helper.returncode == 0, helper.stderr
        receipt = json.loads(helper.stdout)
        observed_at = NOW + timedelta(seconds=0 if same_second else 1)
        payload = {
            **identity,
            "observed_at": observed_at.isoformat(),
            "endpoint_ready": True if identity["role"] == "entrypoint" else None,
            "observation_identity_sha256": grant_response.json()[
                "observation_identity_sha256"
            ],
            "grant": grant.to_mapping(),
            "helper_receipt": receipt,
            "observation_receipt_public_key": bytes([0]) * 0,
        }
        with sessions() as session:
            node = session.get(AgentNode, node_id)
            assert node is not None
            payload["observation_receipt_public_key"] = (
                node.observation_receipt_public_key
            )
        rust = subprocess.run(
            [str(recipe_observation_wire_probe), "serialize"],
            input=json.dumps(
                {
                    "node_id": node_id,
                    "observed_at": observed_at.isoformat(),
                    "runs": [payload],
                },
                separators=(",", ":"),
            )
            + "\n",
            text=True,
            capture_output=True,
            check=False,
        )
        assert rust.returncode == 0, rust.stderr
        envelope = json.loads(rust.stdout)
        parsed = RecipeRunObservationsWire.parse(envelope)
        assert parsed.runs[0].world_size == nodes
        consumed = client.post(
            "/agent/v1/recipe-runs/observations", headers=headers, json=envelope
        )
        assert consumed.status_code == 204, consumed.text
    with sessions() as session:
        node = session.query(RunNode).filter_by(run_id=run_id, node_id=node_id).one()
        assert node.observed_run_generation == identity["run_generation"]
        assert node.state == "running"
        assert node.updated_at == max(
            observed_at, NOW + timedelta(microseconds=500_000) if same_second else NOW
        )
        pending = session.get(RecipeRunObservationGrant, node.id)
        assert pending is not None and pending.consumed is True
    with TestClient(app) as client:
        replay = client.post(
            "/agent/v1/recipe-runs/observations", headers=headers, json=envelope
        )
        assert replay.status_code == 204
    with sessions() as session:
        node = session.query(RunNode).filter_by(run_id=run_id, node_id=node_id).one()
        assert node.state == "failed"


@pytest.mark.parametrize("singleton", [False, True])
def test_rust_observation_json_is_consumed_by_the_controller_wire_model(
    recipe_observation_wire_probe: Path,
    singleton: bool,
) -> None:
    fixture = (
        Path(__file__).parents[2]
        / "agent_protocol"
        / "fixtures"
        / "recipe-run-observation.json"
    )
    payload = json.loads(fixture.read_text())
    if singleton:
        payload.update(
            {
                "rank": 0,
                "role": "entrypoint",
                "world_size": 1,
                "local_address": None,
                "master_address": None,
                "master_port": None,
                "endpoint_ready": True,
            }
        )
        identity = {
            key: value
            for key, value in payload.items()
            if key
            not in {
                "observed_at",
                "endpoint_ready",
                "observation_identity_sha256",
                "grant",
                "helper_receipt",
                "observation_receipt_public_key",
            }
        }
        identity_sha256 = hashlib.sha256(canonical_message(identity)).hexdigest()
        payload["observation_identity_sha256"] = identity_sha256
        payload["grant"]["claims"]["operation"]["observation_identity_sha256"] = (
            identity_sha256  # type: ignore[index]
        )
        payload["helper_receipt"]["claims"]["observation_identity_sha256"] = (
            identity_sha256  # type: ignore[index]
        )
    completed = subprocess.run(
        [str(recipe_observation_wire_probe), "serialize"],
        input=json.dumps(
            {
                "node_id": payload["node_id"],
                "observed_at": payload["observed_at"],
                "runs": [payload],
            },
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
    assert parsed.schema_version == 2
    assert len(parsed.runs) == 1
    assert parsed.runs[0].helper_receipt.signature.algorithm == "ed25519"
    assert parsed.runs[0].world_size == (1 if singleton else 2)
    wire_observation = json.loads(output[0])["runs"][0]
    # Derive the missing-field cases from the canonical definition, including
    # the singleton's explicit nulls. Rust Option must not turn omission into
    # an accepted null that Python rejects.
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
