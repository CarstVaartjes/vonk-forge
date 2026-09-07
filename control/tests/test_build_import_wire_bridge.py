from __future__ import annotations

import json
import hashlib
import os
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_agent_protocol import (
    AgentClaim,
    AgentOperation as ProtocolOperation,
    RecipeBuildEvidence,
    RecipeBuildRequest,
    RecipeImageImportEvidence,
    RecipeImageImportRequest,
    canonical_message,
)
from vonk_control.models import (
    AgentOperation,
    ClusterMapping,
    ClusterMappingNode,
    NodeArtifact,
    RecipeBuild,
)
from vonk_control.recipe_builds import RecipeBuildService
from vonk_control.recipe_operations import (
    RecipeOperationService,
    _record_build_evidence,
    _record_image_import_evidence,
)

from .test_recipe_builds import RecordingQueue, setup


@pytest.fixture(scope="session")
def build_import_wire_probe() -> Path:
    configured = os.environ.get("VONK_BUILD_IMPORT_WIRE_PROBE")
    if configured:
        path = Path(configured).expanduser().resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise AssertionError(f"configured wire probe is not executable: {path}")
        return path
    repository = Path(__file__).resolve().parents[2]
    subprocess.run(
        [
            "cargo",
            "build",
            "--locked",
            "--package",
            "vonk-agent-protocol",
            "--example",
            "build_import_wire_probe",
        ],
        cwd=repository,
        check=True,
    )
    target = repository / "target" / "debug" / "examples" / "build_import_wire_probe"
    if not target.is_file() or not os.access(target, os.X_OK):
        raise AssertionError(
            f"cargo did not produce an executable wire probe: {target}"
        )
    return target


def test_queued_build_and_import_cross_rust_parser_and_typed_evidence(
    tmp_path: Path, build_import_wire_probe: Path
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)

    builds = RecipeBuildService(sessions, bundles=bundles)
    plan = builds.plan(revision.id, node_id, now=now)
    operations = RecipeOperationService(
        sessions,
        install_admission=object(),
        run_admission=object(),
        agent_jobs=RecordingQueue(),
        clock=lambda: now,
        builds=builds,
    )
    queued = operations.build(
        plan,
        build_input_sha256=plan.build_input_sha256,
        actor="test",
        request_id=str(uuid.uuid4()),
    )
    assert queued.kind == "recipe.build.v1" and queued.state == "running"

    def probe(operation: str, payload: dict[str, object]) -> dict[str, object]:
        completed = subprocess.run(
            [str(build_import_wire_probe)],
            input=json.dumps({"operation": operation, "payload": payload}) + "\n",
            text=True,
            capture_output=True,
            check=True,
        )
        return json.loads(completed.stdout)

    def probe_claim(claim: AgentClaim) -> dict[str, object]:
        completed = subprocess.run(
            [str(build_import_wire_probe)],
            input=json.dumps({"claim": json.loads(canonical_message(claim))}) + "\n",
            text=True,
            capture_output=True,
            check=True,
        )
        return json.loads(completed.stdout)

    build_wire = probe("recipe.build.v1", plan.agent_payload)
    build_request = RecipeBuildRequest.model_validate(build_wire["payload"])
    build_evidence = RecipeBuildEvidence.model_validate(build_wire["evidence"])
    assert build_request.build_input_sha256 == plan.build_input_sha256
    assert build_evidence.build_input_sha256 == plan.build_input_sha256

    with sessions.begin() as session:
        build = session.get(RecipeBuild, plan.build_id)
        assert build is not None
        _record_build_evidence(
            session,
            build,
            build_evidence.model_dump(mode="json"),
            now=now,
        )

    with sessions.begin() as session:
        mapping = ClusterMapping(
            recipe_revision_id=revision.id,
            topology_name="wire-bridge",
            generation=1,
            node_count=1,
            state="ready",
            parameters={},
            placement_digest="e" * 64,
            endpoint_owner_node_id=node_id,
            created_by="test",
            created_at=now,
            updated_at=now,
        )
        session.add(mapping)
        session.flush()
        session.add(
            ClusterMappingNode(
                mapping_id=mapping.id,
                node_id=node_id,
                rank=0,
                role="entrypoint",
                endpoint_owner=True,
                created_at=now,
            )
        )
        mapping_id = mapping.id
    preview = operations.preview_image_distribution(
        plan.build_id, mapping_id, mapping_generation=1
    )
    import_operation = operations.distribute_image(
        plan.build_id,
        mapping_id,
        mapping_generation=1,
        plan_digest=preview.plan_digest,
        actor="test",
        request_id=str(uuid.uuid4()),
    )
    with sessions() as session:
        operation = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == import_operation.id
            )
        )
        assert operation is not None
        import_payload = dict(operation.payload)
        import_claim = AgentClaim(
            schema_version=1,
            job_id=operation.parent_job_id,
            operation_id=operation.id,
            attempt=1,
            fence=str(uuid.uuid4()),
            node_id=operation.node_id,
            operation=ProtocolOperation.RECIPE_IMAGE_IMPORT,
            authority_revision=operation.authority_revision,
            payload_digest=hashlib.sha256(
                canonical_message(operation.payload)
            ).hexdigest(),
            payload=operation.payload,
            deadline=now,
        )
    import_wire = probe_claim(import_claim)
    import_request = RecipeImageImportRequest.model_validate(import_wire["payload"])
    import_evidence = RecipeImageImportEvidence.model_validate(import_wire["evidence"])
    assert import_request.build_id == plan.build_id
    assert import_evidence.build_id == plan.build_id

    with sessions.begin() as session:
        operation = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == import_operation.id
            )
        )
        assert operation is not None
        _record_image_import_evidence(
            session,
            operation,
            import_evidence.model_dump(mode="json"),
            True,
            now,
        )
        artifact = session.scalar(
            select(NodeArtifact).where(
                NodeArtifact.node_id == node_id,
                NodeArtifact.digest == build_evidence.image_digest[7:],
            )
        )
        assert artifact is not None and artifact.state == "verified"


def test_build_wire_rejects_scalar_coercion(tmp_path: Path) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)

    payload = (
        RecipeBuildService(sessions, bundles=bundles)
        .plan(revision.id, node_id, now=now)
        .agent_payload
    )
    for field, value in (("schema_version", True), ("source_bundle_bytes", 1.0)):
        malformed = dict(payload)
        malformed[field] = value
        with pytest.raises(ValueError):
            RecipeBuildRequest.model_validate(malformed)
    malformed = dict(payload)
    malformed["arguments"] = [{"name": "build_arg", "value": 1.0}]
    with pytest.raises(ValueError):
        RecipeBuildRequest.model_validate(malformed)


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("capabilities",), ["SYS_ADMIN"]),
        (("capabilities",), ["DAC_OVERRIDE", "DAC_OVERRIDE"]),
        (("network", "mode"), "public"),
        (("limits", "gpu"), 1),
        (("options", "layer_compression"), "zstd"),
    ),
)
def test_build_wire_preserves_rust_security_invariants(
    tmp_path: Path, path: tuple[str, ...], value: object
) -> None:
    sessions, bundles, now, node_id, revision = setup(tmp_path)
    payload = (
        RecipeBuildService(sessions, bundles=bundles)
        .plan(revision.id, node_id, now=now)
        .agent_payload
    )
    malformed = json.loads(json.dumps(payload))
    target = malformed
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        RecipeBuildRequest.model_validate(malformed)
