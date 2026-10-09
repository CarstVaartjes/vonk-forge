"""The artifact-job apply path must reject a build drifted from the installed plan.

``compile_job_invocation`` re-derives the package handle from the live
``RecipeBuild`` row.  A build row can acquire a different image digest after
repair (see ``installation_matches_runtime_image``), so the apply path must
compare its re-derived compiled runtime image against the exact runtime image
the installed plan binds instead of silently rebinding the job.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import CompiledExecutionPlan
from vonk_control.catalog_entities import CatalogEntityService
from vonk_control.execution_plan_service import (
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
    return sessions, revision, installed, installed


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


def _invoke(
    sessions,
    revision,
    installed,
    build,
    *,
    memory_floor_bytes=None,
    reserved_memory_bytes=None,
):
    with sessions() as session:
        return compile_job_invocation(
            session,
            revision=revision,
            installed=installed,
            build=build,
            parameters={},
            timeout_seconds=installed.job.timeout_seconds,
            memory_floor_bytes=(
                installed.runtime.placement.memory_floor_bytes
                if memory_floor_bytes is None
                else memory_floor_bytes
            ),
            reserved_memory_bytes=(
                installed.runtime.placement.reserved_memory_bytes
                if reserved_memory_bytes is None
                else reserved_memory_bytes
            ),
        )


def test_job_apply_compiles_against_the_installed_build(installation):
    sessions, revision, installed, plan = installation
    assert revision.content_digest == installed.identity.recipe_revision_sha256
    compiled = _invoke(
        sessions,
        revision,
        installed,
        _build(installed, plan.runtime_image.image_digest),
    )
    assert compiled.identity.recipe_revision_sha256 == (
        plan.identity.recipe_revision_sha256
    )


def test_job_apply_rejects_a_build_that_drifted_from_the_installed_plan(installation):
    sessions, revision, installed, _plan = installation
    repaired = "sha256:" + "c" * 64
    assert repaired != installed.runtime_image.image_digest
    with pytest.raises(Exception):  # noqa: B017 -- no invocation for drifted bytes
        _invoke(sessions, revision, installed, _build(installed, repaired))
    accepted = _invoke(
        sessions,
        revision,
        installed,
        _build(installed, installed.runtime_image.image_digest),
    )
    assert accepted is not None


def test_installed_fixture_matches_its_recipe_document():
    """The committed payload must be the compile of the committed recipe."""

    recipes, _models = load_inputs()
    document = recipes[RECIPE_SLUG]
    installed = CompiledExecutionPlan.model_validate_json(
        INSTALLED_PLAN.read_text(encoding="utf-8")
    )
    assert installed.identity.recipe_revision_sha256 == document_sha256(document)


def test_job_apply_preserves_the_reviewed_system_memory_reserve(installation):
    # Break caught: an invocation can overwrite the recipe's system reserve
    # with zero, admitting work into memory reserved for the host.
    sessions, revision, installed, plan = installation
    assert plan.runtime.placement.memory_floor_bytes > 0
    with pytest.raises(Exception):  # noqa: B017 -- observable effects and recovery establish the rejection
        _invoke(
            sessions,
            revision,
            installed,
            _build(installed, plan.runtime_image.image_digest),
            memory_floor_bytes=0,
        )


def test_job_invocation_binds_the_accepted_reservation_instead_of_recipe_peak(
    installation,
):
    # Break caught: a job copies the installation's peak estimate into its
    # reservation field instead of the exact capacity accepted for its run.
    sessions, revision, installed, plan = installation
    reservation = plan.runtime.placement.reserved_memory_bytes // 2
    assert reservation > 0
    compiled = _invoke(
        sessions,
        revision,
        installed,
        _build(installed, plan.runtime_image.image_digest),
        reserved_memory_bytes=reservation,
    )
    assert compiled.runtime.placement.reserved_memory_bytes == reservation


def test_unconfirmed_model_cache_evidence_keeps_its_type(installation):
    """A model receipt that cannot be read now is an unknown outcome the
    admitting owner observes again, not a compilation verdict."""

    from vonk_control.execution_plan_service import ControllerExecutionPlanService
    from vonk_control.model_cache import ModelCacheStorageUnknown

    sessions, revision, _installed, _plan = installation

    class Cache:
        def resolve_artifact_set(self, **_unused):
            raise ModelCacheStorageUnknown(
                "model_cache.source_unavailable", "the cache is unavailable"
            )

    with sessions() as session, pytest.raises(ModelCacheStorageUnknown):
        ControllerExecutionPlanService(Cache()).compile_installation(
            session,
            revision=revision,
            build=None,
            mapping_nodes=[],
            parameters={},
        )
