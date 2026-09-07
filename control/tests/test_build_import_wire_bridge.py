from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from vonk_agent_protocol import (
    RecipeBuildEvidence,
    RecipeBuildRequest,
    RecipeImageImportEvidence,
    RecipeImageImportRequest,
)
from vonk_control.models import AgentOperation, Job, NodeArtifact, RecipeBuild
from vonk_control.recipe_builds import RecipeBuildService
from vonk_control.recipe_operations import (
    _record_build_evidence,
    _record_image_import_evidence,
)

from .test_recipe_builds import setup


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

    plan = RecipeBuildService(sessions, bundles=bundles).plan(
        revision.id, node_id, now=now
    )

    def probe(operation: str, payload: dict[str, object]) -> dict[str, object]:
        completed = subprocess.run(
            [str(build_import_wire_probe)],
            input=json.dumps({"operation": operation, "payload": payload}) + "\n",
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

    import_payload = {
        "schema_version": 1,
        "kind": "recipe.image.import.v1",
        "build_id": plan.build_id,
        "mapping_id": str(uuid.uuid4()),
        "mapping_generation": 1,
        "source_node_id": node_id,
        "image_digest": build_evidence.image_digest,
        "oci_layout_sha256": build_evidence.oci_layout_sha256,
        "image_bytes": build_evidence.image_bytes,
    }
    import_wire = probe("recipe.image.import.v1", import_payload)
    import_request = RecipeImageImportRequest.model_validate(import_wire["payload"])
    import_evidence = RecipeImageImportEvidence.model_validate(import_wire["evidence"])
    assert import_request.build_id == plan.build_id
    assert import_evidence.build_id == plan.build_id

    with sessions.begin() as session:
        job_id = str(uuid.uuid4())
        session.add(
            Job(
                id=job_id,
                request_id=str(uuid.uuid4()),
                kind="recipe.image.import.v1",
                state="running",
                actor="test",
                authority_revision="a" * 64,
                targets=[node_id],
                payload_digest="c" * 64,
                payload=import_payload,
                current_attempt=1,
                created_at=now,
                updated_at=now,
            )
        )
        operation = AgentOperation(
            id=str(uuid.uuid4()),
            parent_job_id=job_id,
            node_id=node_id,
            kind="recipe.image.import.v1",
            payload_digest="d" * 64,
            payload=import_payload,
            authority_revision="a" * 64,
            state="running",
            current_attempt=1,
            created_at=now,
            updated_at=now,
        )
        session.add(operation)
        session.flush()
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
