"""The artifact-job apply path must reject a build drifted from the installed plan.

``compile_job_invocation`` re-derives the package handle from the live
``RecipeBuild`` row.  A build row can acquire a different image digest after
repair (see ``installation_matches_runtime_image``), so the apply path must
compare its re-derived compiled runtime image against the exact runtime image
the installed plan binds instead of silently rebinding the job.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import CompiledExecutionPlan
from vonk_control.catalog_entities import CatalogEntityService
from vonk_control.execution_plan_service import (
    ExecutionPlanCompilationError,
    compile_job_invocation,
)
from vonk_control.models import Base, RecipeBuild
from vonk_forge_contracts import document_sha256

from .catalog_launch_fixtures import load_inputs

RECIPE_SLUG = "trellis-2-4b-pytorch-single"
INSTALLED_PLAN = (
    Path(__file__).resolve().parents[2]
    / "agent_protocol"
    / "tests"
    / "fixtures"
    / "catalog-launch"
    / f"{RECIPE_SLUG}--default--rank0.json"
)


@pytest.fixture()
def installation(tmp_path):
    """Activate the committed fixture recipe and its exact models."""

    recipes, models = load_inputs()
    document = recipes[RECIPE_SLUG]
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 8, 7, 12, tzinfo=UTC)
    catalog = CatalogEntityService(sessions, clock=lambda: now)
    for selection in document["models"]:
        reference = selection["model"]
        model_document = models[reference["content_sha256"]]
        draft = catalog.create_draft(model_document, actor="test")
        catalog.resolve(draft.id, actor="test")
    recipe_draft = catalog.create_draft(document, actor="test")
    revision = catalog.resolve(recipe_draft.id, actor="test")
    installed = CompiledExecutionPlan.model_validate_json(
        INSTALLED_PLAN.read_text(encoding="utf-8")
    )
    return sessions, revision, installed.model_dump(mode="json"), installed


def _build(installed, image_digest: str) -> RecipeBuild:
    return RecipeBuild(
        id=str(uuid4()),
        recipe_revision_id="unused",
        builder_node_id="spk_" + "0" * 32,
        source_bundle_sha256="d" * 64,
        build_input_sha256="e" * 64,
        state="succeeded",
        policy_report={},
        plan={},
        image_digest=image_digest,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _invoke(sessions, revision, installed, build):
    with sessions() as session:
        return compile_job_invocation(
            session,
            revision=revision,
            installed=installed,
            build=build,
            parameters={},
            timeout_seconds=installed["job"]["timeout_seconds"],
            memory_floor_bytes=0,
        )


def test_job_apply_compiles_against_the_installed_build(installation):
    sessions, revision, installed, plan = installation
    assert revision.content_digest == installed["identity"]["recipe_revision_sha256"]
    compiled = _invoke(
        sessions,
        revision,
        installed,
        _build(installed, plan.runtime_image.image_digest),
    )
    compiled_identity = compiled["identity"]
    assert isinstance(compiled_identity, Mapping)
    assert compiled_identity["recipe_revision_sha256"] == (
        plan.identity.recipe_revision_sha256
    )


def test_job_apply_rejects_a_build_that_drifted_from_the_installed_plan(installation):
    sessions, revision, installed, _plan = installation
    repaired = "sha256:" + "c" * 64
    assert repaired != installed["runtime_image"]["image_digest"]
    with pytest.raises(
        ExecutionPlanCompilationError,
        match="job build differs from the installed workload",
    ):
        _invoke(sessions, revision, installed, _build(installed, repaired))


def test_installed_fixture_matches_its_recipe_document():
    """The committed payload must be the compile of the committed recipe."""

    recipes, _models = load_inputs()
    document = recipes[RECIPE_SLUG]
    installed = CompiledExecutionPlan.model_validate_json(
        INSTALLED_PLAN.read_text(encoding="utf-8")
    )
    assert installed.identity.recipe_revision_sha256 == document_sha256(document)
