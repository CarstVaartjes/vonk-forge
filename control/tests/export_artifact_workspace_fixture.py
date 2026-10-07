"""Hosted native component fixture from real artifact/recipe producers."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from vonk_agent_protocol import canonical_message
from vonk_control.api import create_app
from vonk_control.artifact_job_api import ArtifactJobCreate
from vonk_control.artifact_jobs import ArtifactJobListResponse
from vonk_control.auth import Actor, TokenCodec
from vonk_control.compiled_artifact_contract import compile_artifact_contract
from vonk_control.library_contract import LibraryRunSummary
from vonk_control.models import CatalogDocumentRevision, RecipeRun, RunNode
from vonk_control.strict_json import serialize_json_value
from vonk_forge_contracts import RecipeDefinition

from tests.test_artifact_jobs import _mapping, running_artifact_service
from tests.test_platform_observation import Jobs

INITIAL = 9_007_199_254_740_993
EDITED = 9_007_199_254_740_995


def _recipe_transform(document: dict[str, object]) -> None:
    settings = _mapping(document["settings"])
    knobs = _mapping(settings["knobs"])
    _mapping(knobs["seed"])["value"] = INITIAL
    _mapping(knobs["prompt"])["value"] = "fox"
    interfaces = document["interfaces"]
    assert isinstance(interfaces, list)
    # RecipeJobInterface owns an optional input declaration. Omission is the
    # canonical no-input recipe; zero-sized/empty RecipeJobInput is invalid.
    _mapping(interfaces[0]).pop("input")


def export(output: Path) -> None:
    with tempfile.TemporaryDirectory() as directory:
        sessions, _operations, _queue, service, run_id, _node = (
            running_artifact_service(
                Path(directory), recipe_transform=_recipe_transform
            )
        )
        with sessions() as session:
            revision = session.scalar(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "recipe"
                )
            )
            run = session.get(RecipeRun, run_id)
            assert revision is not None and run is not None
            definition = RecipeDefinition.model_validate_json(
                canonical_message(revision.document), strict=True
            )
            members = session.scalars(
                select(RunNode).where(RunNode.run_id == run_id)
            ).all()
            healthy_count = sum(member.state == "running" for member in members)
            observed_run = LibraryRunSummary.model_validate(
                {
                    "run_id": run.id,
                    "installation_id": run.installation_id,
                    "recipe_revision_id": revision.id,
                    "state": run.state,
                    "route_state": run.route_state,
                    "healthy_rank_count": healthy_count,
                    "expected_rank_count": len(members),
                    "healthy": bool(members) and healthy_count == len(members),
                }
            )
        compiled = compile_artifact_contract(definition, "image-job")
        assert compiled.input.required is False
        assert compiled.input.slots == ()
        expected_request = ArtifactJobCreate.model_validate_json(
            canonical_message(
                {
                    "interface": "image-job",
                    "parameters": {"seed": EDITED, "prompt": "fox"},
                    "inputs": [],
                    "output_limits": serialize_json_value(compiled.output_limits),
                    "timeout_seconds": 3600,
                }
            ),
            strict=True,
        )
        codec = TokenCodec(b"k" * 32)
        token = codec.issue(Actor("operator", "operator"), ttl_seconds=100, now=0)
        with TestClient(
            create_app(jobs=Jobs(), tokens=codec, now=lambda: 10),
            headers={"Authorization": f"Bearer {token}"},
        ) as peer:
            refusal = peer.post(
                f"/api/recipe/runs/{run_id}/artifact-jobs",
                content=canonical_message(expected_request),
                headers={
                    "Content-Type": "application/json",
                    "X-Request-ID": "00000000-0000-4000-8000-000000000301",
                },
            )
        assert refusal.status_code == 503, refusal.text
        fixture = {
            "definition_json": canonical_message(definition).decode(),
            "run_json": canonical_message(observed_run).decode(),
            "revision_id": revision.id,
            "content_sha256": revision.content_digest,
            "capabilities_json": canonical_message(service.capabilities()).decode(),
            "jobs_json": canonical_message(ArtifactJobListResponse(jobs=[])).decode(),
            "refusal": {
                "status": refusal.status_code,
                "body": refusal.text,
                "media_type": refusal.headers["content-type"],
            },
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(fixture, ensure_ascii=False) + "\n")


def verify(request: Path) -> None:
    body = ArtifactJobCreate.model_validate_json(request.read_bytes(), strict=True)
    assert type(body.parameters["seed"]) is int
    assert body.parameters["seed"] == EDITED
    assert body.parameters["prompt"] == "fox"
    assert body.inputs == []


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--output", type=Path)
    group.add_argument("--verify-request", type=Path)
    options = parser.parse_args()
    if options.output is not None:
        export(options.output)
    else:
        verify(options.verify_request)
