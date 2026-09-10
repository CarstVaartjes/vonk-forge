from datetime import timedelta

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from vonk_control.deployment_provenance import (
    DeploymentProvenanceService,
    local_deployment_observations,
)
from vonk_control.deployment_provenance_api import install_deployment_provenance_routes
from vonk_control.deployment_provenance_contract import (
    DeploymentObservations,
    DeploymentProvenance,
    PhysicalAcceptanceReceipt,
    PlatformObservation,
)
from vonk_control.models import (
    AgentNode,
    ClusterMappingNode,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from vonk_control.run_admission import RunAdmissionService

from .test_run_admission import setup


def deployment(tmp_path):
    sessions, now, node, installation = setup(tmp_path)
    admission = RunAdmissionService(
        sessions, inventory_max_age=300, memory_floor_bytes=50
    )
    plan = admission.plan_run(installation, alias="qwen", now=now)
    run_id = admission.accept_run(plan, actor="admin", now=now)
    with sessions.begin() as session:
        agent = session.get(AgentNode, node)
        agent.last_seen_at = now
        agent.semantic_version = "1.2.3"
        agent.build_digest = "sha256:" + "a" * 64
        agent.binary_digest = "b" * 64
        rank = session.scalar(select(RunNode).where(RunNode.run_id == run_id))
        rank.observed_run_generation = 1
        rank.observation_receipt_sha256 = "c" * 64
        rank.state = "running"
    return sessions, now, node, run_id


def test_all_boundaries_and_stable_json(tmp_path):
    sessions, now, node, _ = deployment(tmp_path)
    service = DeploymentProvenanceService(sessions, clock=lambda: now)
    result = service.snapshot()
    assert [b.boundary for b in result.platform] == [
        "repository",
        "publication",
        "controller_deployment",
    ]
    assert all(b.state == "unknown" for b in result.platform)
    assert result.agents[0].binary_sha256 == "b" * 64
    assert result.agents[0].package_sha256 is None
    workload = result.workloads[0]
    assert workload.models[0].repository.startswith("https://")
    assert workload.source_bundle_sha256 == "c" * 64
    assert workload.build_input_sha256 == "e" * 64
    assert workload.rank_agreement == "match"
    assert workload.ranks[0].node_id == node
    assert workload.physical_acceptance.state == "not_qualified"
    assert DeploymentProvenance.model_validate_json(result.model_dump_json()) == result
    assert service.snapshot().model_dump_json() == result.model_dump_json()


def test_running_image_metadata_supplies_source_without_inventing_digest(tmp_path, monkeypatch):
    from vonk_control import deployment_provenance as provenance

    metadata = tmp_path / "controller-build.json"
    metadata.write_text('{"source_commit":"' + "a" * 40 + '"}')
    monkeypatch.delenv("VONK_DEPLOYMENT_OBSERVATIONS_FILE", raising=False)
    monkeypatch.setattr(provenance, "CONTROLLER_BUILD_METADATA", metadata)
    observation = local_deployment_observations()
    assert observation.controller.source_commit == "a" * 40
    assert observation.controller.image_digest is None
    assert observation.publication is None
    metadata.write_text('{"source_commit":12}')
    with pytest.raises(ValueError):
        local_deployment_observations()


def test_composed_app_serves_provenance_with_authentication(tmp_path):
    from vonk_control.api import create_app
    from vonk_control.audit import MemoryAuditStore
    from vonk_control.auth import Actor, TokenCodec

    from .test_api import Jobs

    sessions, now, _, _ = deployment(tmp_path)
    codec = TokenCodec(b"k" * 32)
    app = create_app(
        jobs=Jobs(), tokens=codec, audits=MemoryAuditStore(), now=lambda: 10,
        deployment_provenance=DeploymentProvenanceService(sessions, clock=lambda: now),
    )
    client = TestClient(app)
    assert client.get("/api/deployment-provenance").status_code == 401
    token = codec.issue(Actor("admin", "administrator"), ttl_seconds=100, now=0)
    response = client.get("/api/deployment-provenance", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert DeploymentProvenance.model_validate_json(response.content).agents


def test_repository_publication_and_deployment_are_independent(tmp_path):
    sessions, now, _, _ = deployment(tmp_path)
    observations = DeploymentObservations(
        repository=PlatformObservation(
            source="Repository observation", observed_at=now, source_commit="a" * 40
        ),
        publication=PlatformObservation(
            source="Signed installer",
            observed_at=now,
            source_commit="b" * 40,
            manifest_sha256="e" * 64,
        ),
        controller=PlatformObservation(
            source="Running image",
            observed_at=now,
            source_commit="c" * 40,
            image_digest="sha256:" + "d" * 64,
        ),
    )
    result = DeploymentProvenanceService(
        sessions, clock=lambda: now, observations=lambda: observations
    ).snapshot()
    assert [b.state for b in result.platform] == [
        "repository_not_published",
        "publication_not_deployed",
        "observed",
    ]
    assert result.platform[1].image_digest is None
    assert result.workloads[0].physical_acceptance.state == "not_qualified"


def test_offline_evidence_remains_visible_and_aged(tmp_path):
    sessions, now, _, _ = deployment(tmp_path)
    result = DeploymentProvenanceService(
        sessions, clock=lambda: now + timedelta(hours=2)
    ).snapshot()
    assert result.agents[0].connectivity == "offline"
    assert result.agents[0].evidence.age_seconds == 7200
    assert result.workloads[0].ranks[0].runtime_evidence.freshness == "stale"
    assert result.workloads[0].ranks[0].observation_receipt_sha256 == "c" * 64


def test_multi_rank_missing_observation_and_generation_mismatch(tmp_path):
    sessions, now, _, run_id = deployment(tmp_path)
    with sessions.begin() as session:
        installation = session.scalar(select(RecipeInstallation))
        session.add(
            AgentNode(node_id="spk_" + "2" * 32, state="active", capabilities=[])
        )
        session.flush()
        session.add(
            ClusterMappingNode(
                mapping_id=installation.mapping_id,
                node_id="spk_" + "2" * 32,
                rank=1,
                role="worker",
                endpoint_owner=False,
                created_at=now,
            )
        )
    service = DeploymentProvenanceService(sessions, clock=lambda: now)
    result = service.snapshot().workloads[0]
    assert result.rank_agreement == "unknown"
    assert [r.identity_agreement for r in result.ranks] == ["match", "unknown"]
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        run.run_generation = 2
    assert service.snapshot().workloads[0].rank_agreement == "mismatch"


def test_physical_acceptance_requires_exact_execution_receipt(tmp_path):
    sessions, now, node, run_id = deployment(tmp_path)
    base = (
        DeploymentProvenanceService(sessions, clock=lambda: now).snapshot().workloads[0]
    )
    receipt = PhysicalAcceptanceReceipt(
        source="Physical Spark campaign",
        observed_at=now,
        evidence_sha256="f" * 64,
        lane="physical-spark",
        run_id=run_id,
        run_generation=1,
        installation_id=base.installation_id,
        recipe_sha256=base.recipe_content_sha256,
        image_digest=base.image_digest,
        node_ids=[node],
        passed=True,
    )
    observations = DeploymentObservations(physical_acceptance=[receipt])
    service = DeploymentProvenanceService(
        sessions, clock=lambda: now, observations=lambda: observations
    )
    assert service.snapshot().workloads[0].physical_acceptance.state == "accepted"
    receipt.run_generation = 2
    assert (
        service.snapshot().workloads[0].physical_acceptance.state == "identity_mismatch"
    )
    receipt.run_generation = 1
    receipt.passed = False
    assert service.snapshot().workloads[0].physical_acceptance.state == "failed"


def test_configured_malformed_evidence_is_not_silently_unknown(tmp_path, monkeypatch):
    monkeypatch.delenv("VONK_DEPLOYMENT_OBSERVATIONS_FILE", raising=False)
    assert local_deployment_observations().controller is None
    path = tmp_path / "observations.json"
    path.write_text('{"schema_version": 1}')
    monkeypatch.setenv("VONK_DEPLOYMENT_OBSERVATIONS_FILE", str(path))
    with pytest.raises(ValueError):
        local_deployment_observations()


def test_authenticated_endpoint_serves_typed_projection_and_bounds_errors(tmp_path):
    sessions, now, _, _ = deployment(tmp_path)
    service = DeploymentProvenanceService(sessions, clock=lambda: now)
    app = FastAPI()

    def unauthorized():
        raise HTTPException(401)

    install_deployment_provenance_routes(
        app, actor_dependency=Depends(unauthorized), provenance=service
    )
    client = TestClient(app)
    assert client.get("/api/deployment-provenance").status_code == 401
    app.dependency_overrides[unauthorized] = lambda: object()
    response = client.get("/api/deployment-provenance")
    assert response.status_code == 200
    assert DeploymentProvenance.model_validate_json(response.text) == service.snapshot()
    service._observations = lambda: (_ for _ in ()).throw(ValueError("secret details"))
    response = client.get("/api/deployment-provenance")
    assert response.status_code == 503
    assert "secret" not in response.text


def test_package_receipt_must_match_the_current_authenticated_binary(tmp_path):
    import uuid

    from vonk_control.models import AgentOperation, AgentOperationAttempt

    from .package_upgrade_fixtures import activation_receipt, source_transport

    sessions, now, node, _ = deployment(tmp_path)
    with sessions.begin() as session:
        operation = AgentOperation(
            parent_job_id=str(uuid.uuid4()),
            node_id=node,
            kind="agent.upgrade.v1",
            payload_digest="a" * 64,
            payload={},
            authority_revision="a" * 64,
            state="succeeded",
            created_at=now,
            updated_at=now,
        )
        session.add(operation)
        session.flush()
        session.add(
            AgentOperationAttempt(
                operation_id=operation.id,
                attempt=1,
                fence=str(uuid.uuid4()),
                lease_deadline=now,
                agent_certificate_serial="test",
                state="succeeded",
                result={
                    "architecture": "linux-arm64",
                    "binary_digest": "b" * 64,
                    "build_digest": "sha256:" + "a" * 64,
                    "package_sha256": "d" * 64,
                    "package_version": "1.2.3",
                    "self_test_passed": True,
                    "status": "upgraded",
                    "activation_receipt": activation_receipt({
                        **source_transport(), "package_sha256": "d" * 64,
                        "package_version": "1.2.3", "target_binary_digest": "b" * 64,
                    }, node, now=int(now.timestamp())),
                },
            )
        )
    service = DeploymentProvenanceService(sessions, clock=lambda: now)
    assert service.snapshot().agents[0].package_sha256 == "d" * 64
    with sessions.begin() as session:
        session.get(AgentNode, node).binary_digest = "e" * 64
    assert service.snapshot().agents[0].package_sha256 is None


def test_cli_human_view_preserves_evidence_boundaries(tmp_path):
    from cluster_profiles.deployment_provenance_cli import render_deployment_provenance

    sessions, now, _, _ = deployment(tmp_path)
    result = DeploymentProvenanceService(sessions, clock=lambda: now).snapshot()
    text = render_deployment_provenance(result.model_dump(mode="json"))
    assert "repository: unknown" in text
    assert "controller_deployment: unknown" in text
    assert "physical acceptance: not_qualified" in text
    assert result.workloads[0].recipe_content_sha256 in text
    assert "rank 0" in text


def test_connected_recipe_start_receipts_expose_rank_artifacts(tmp_path):
    import uuid

    from vonk_control.models import AgentOperation, AgentOperationAttempt

    from .test_recipe_operations import (
        NOW,
        installed_recipe,
        setup_services,
        start_evidence,
        started_recipe,
    )

    sessions, operations, _, mapping_id, build_id, nodes = setup_services(
        tmp_path, nodes=2
    )
    installation = installed_recipe(
        operations, mapping_id, build_id, nodes, request_id="provenance-install"
    )
    run = started_recipe(
        sessions,
        operations,
        installation.owner_id,
        nodes,
        request_id="provenance-start",
    )
    with sessions.begin() as session:
        children = session.scalars(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == run.id,
                AgentOperation.kind == "recipe.start",
            )
        ).all()
        assert len(children) == 2
        for child in children:
            evidence = start_evidence(child.payload)
            session.add(
                AgentOperationAttempt(
                    operation_id=child.id,
                    attempt=1,
                    fence=str(uuid.uuid4()),
                    lease_deadline=NOW,
                    agent_certificate_serial="serial-0",
                    state="succeeded",
                    result={
                        "evidence": evidence,
                        "evidence_digest": evidence["evidence_digest"],
                    },
                )
            )
    service = DeploymentProvenanceService(sessions, clock=lambda: NOW)
    result = service.snapshot().workloads[0]
    assert result.rank_agreement == "match"
    assert all(
        rank.observed_image_digest == result.image_digest for rank in result.ranks
    )
    assert all(
        rank.observed_recipe_sha256 == result.recipe_content_sha256
        for rank in result.ranks
    )
    with sessions.begin() as session:
        attempt = session.scalar(
            select(AgentOperationAttempt)
            .join(AgentOperation, AgentOperation.id == AgentOperationAttempt.operation_id)
            .where(AgentOperation.kind == "recipe.start")
            .order_by(AgentOperationAttempt.id)
        )
        attempt.result = {
            **attempt.result,
            "evidence": {
                **attempt.result["evidence"],
                "image_digest": "sha256:" + "f" * 64,
            },
        }
    result = service.snapshot().workloads[0]
    assert result.rank_agreement == "mismatch"
    assert sum(rank.identity_agreement == "mismatch" for rank in result.ranks) == 1


@pytest.mark.parametrize("coerced", [True, "7200", 7200.0])
def test_nested_provenance_scalar_coercions_are_rejected(tmp_path, coerced):
    import json

    from pydantic import ValidationError

    sessions, now, _, _ = deployment(tmp_path)
    snapshot = DeploymentProvenanceService(sessions, clock=lambda: now).snapshot()
    document = snapshot.model_dump()
    document["agents"][0]["evidence"]["age_seconds"] = coerced
    with pytest.raises(ValidationError) as error:
        DeploymentProvenance.model_validate(document)
    assert error.value.errors()[0]["loc"] == ("agents", 0, "evidence", "age_seconds")
    document = snapshot.model_dump(mode="json")
    document["agents"][0]["evidence"]["age_seconds"] = coerced
    with pytest.raises(ValidationError) as error:
        DeploymentProvenance.model_validate_json(json.dumps(document))
    assert error.value.errors()[0]["loc"] == ("agents", 0, "evidence", "age_seconds")
