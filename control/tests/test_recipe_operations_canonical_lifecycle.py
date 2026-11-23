"""Recipe operations: canonical lifecycle."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from vonk_forge_contracts import (
    ModelDefinition,
    RecipeDefinition,
    document_sha256,
)

from vonk_agent_protocol.recipe_operations import RecipeInstallPayload
from vonk_control.model_primitives import Base
from vonk_control.models.catalog import (
    CatalogDocumentRevision,
)
from vonk_control.models.execution import (
    InstallationNode,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from vonk_control.models.jobs import Job
from vonk_control.models.operations import AgentOperation
from vonk_control.recipe_operations.progress import _recipe_model_identities

from .recipe_operations_support import (
    NOW,
    installed_recipe,
    setup_services,
    started_recipe,
)


def test_recipe_model_identities_include_canonical_companion_dependencies() -> None:
    primary_document = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text()
    )
    companion_document = json.loads(json.dumps(primary_document))
    companion_document["identity"]["slug"] = "synthetic-companion"
    companion_document["identity"]["model"]["slug"] = "synthetic-companion"
    companion = ModelDefinition.model_validate(companion_document)
    companion_digest = document_sha256(companion.model_dump(mode="json"))
    primary_document["dependencies"] = [
        {
            "kind": "model",
            "publisher": companion.identity.publisher,
            "slug": companion.identity.slug,
            "content_sha256": companion_digest,
        }
    ]
    primary = ModelDefinition.model_validate(primary_document)
    primary_digest = document_sha256(primary.model_dump(mode="json"))
    recipe_document = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "recipe-source-build.json")
        .read_text()
    )
    recipe_document["models"][0]["model"] = {
        "kind": "model",
        "publisher": primary.identity.publisher,
        "slug": primary.identity.slug,
        "content_sha256": primary_digest,
    }
    recipe = RecipeDefinition.model_validate(recipe_document)
    revisions = iter(
        (
            SimpleNamespace(document=primary.model_dump(mode="json")),
            SimpleNamespace(document=companion.model_dump(mode="json")),
        )
    )

    class RevisionSession(Session):
        def scalar(self, _statement: object) -> SimpleNamespace:
            return next(revisions)

    identities = _recipe_model_identities(
        RevisionSession(), recipe.model_dump(mode="json")
    )

    assert identities == (
        (primary_digest, f"{primary.identity.publisher}/{primary.identity.slug}"),
        (companion_digest, f"{companion.identity.publisher}/{companion.identity.slug}"),
    )


@pytest.mark.parametrize("retry_state", ["succeeded", "observing"])
def test_restart_interrupted_install_result_keeps_lifecycle_pending_until_retry(
    tmp_path: Path,
    retry_state: str,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    plan = service.preview_install(mapping_id, build_id)
    installation_operation = service.install(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id=str(uuid.uuid4())
    )
    with sessions.begin() as session:
        child = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == installation_operation.id
            )
        )
        assert child is not None
        child.state = "observing"
        child.next_action_at = NOW + timedelta(seconds=2)
        child.status_reason = "exact lifecycle retry scheduled"
        service.consume_agent_result(
            session,
            child,
            object(),
            SimpleNamespace(
                state="observing",
                result={
                    "reason": "agent restarted during install",
                    "error_code": "agent_restart_interrupted",
                    "failure_kind": "uncertain-effect",
                    "uncertain": True,
                },
            ),
        )
    with sessions() as session:
        job = session.get(Job, installation_operation.id)
        installation = session.get(RecipeInstallation, installation_operation.owner_id)
        node = session.scalar(
            select(InstallationNode).where(
                InstallationNode.installation_id == installation_operation.owner_id,
                InstallationNode.node_id == nodes[0],
            )
        )
        assert job is not None
        assert job.state == "running"
        assert installation is not None
        assert installation.state == "installing"
        assert node is not None
        assert node.state != "failed"
    view = service.get(installation_operation.id)
    assert view.retry_due_at == NOW + timedelta(seconds=2)

    with sessions.begin() as session:
        child = session.scalar(
            select(AgentOperation).where(
                AgentOperation.parent_job_id == installation_operation.id
            )
        )
        assert child is not None
        child.state = retry_state
        service.consume_agent_result(
            session,
            child,
            object(),
            SimpleNamespace(
                state=retry_state,
                result=(
                    {"installed_bytes": 120}
                    if retry_state == "succeeded"
                    else {
                        "error_code": "exact_install_effect_unproven",
                        "failure_kind": "uncertain-effect",
                        "uncertain": True,
                    }
                ),
            ),
        )
    with sessions() as session:
        installation = session.get(RecipeInstallation, installation_operation.owner_id)
        node = session.scalar(
            select(InstallationNode).where(
                InstallationNode.installation_id == installation_operation.owner_id,
                InstallationNode.node_id == nodes[0],
            )
        )
        assert installation is not None
        assert node is not None
        if retry_state == "succeeded":
            assert installation.state == "installed"
            assert node.state == "installed"
        else:
            assert installation.state == "installing"
            assert node.state != "failed"


def test_retry_scheduled_start_observation_failure_keeps_the_run_pending(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        service, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    plan = service.preview_run(installation.owner_id, "restart-recovery")
    started = service.start(
        plan, plan_digest=plan.plan_digest, actor="admin", request_id=str(uuid.uuid4())
    )
    with sessions.begin() as session:
        child = session.scalar(
            select(AgentOperation).where(AgentOperation.parent_job_id == started.id)
        )
        assert child is not None
        child.state = "backoff"
        child.next_action_at = NOW + timedelta(seconds=2)
        service.consume_agent_result(
            session,
            child,
            object(),
            SimpleNamespace(
                state="failed",
                result={
                    "error_code": "runtime_observation_unavailable",
                    "failure_kind": "temporary-dependency",
                },
            ),
        )
    with sessions() as session:
        run = session.get(RecipeRun, started.owner_id)
        node = session.scalar(select(RunNode).where(RunNode.run_id == started.owner_id))
        assert run is not None
        assert run.state == "starting"
        assert node is not None
        assert node.state != "failed"
    assert service.get(started.id).retry_due_at == NOW + timedelta(seconds=2)


def test_canonical_recipe_revision_drives_install_and_payload(
    tmp_path: Path,
) -> None:
    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)

    plan = service.preview_install(mapping_id, build_id)
    with sessions() as session:
        revision = session.get(CatalogDocumentRevision, plan.recipe_revision_id)
        assert revision is not None
        assert revision.kind == "recipe"
        assert revision.state == "active"
        assert plan.recipe_content_sha256 == revision.content_digest

    operation = service.install(
        plan,
        plan_digest=plan.plan_digest,
        actor="admin",
        request_id="canonical-schema2-install",
    )
    with sessions() as session:
        installation = session.get(RecipeInstallation, operation.owner_id)
        assert installation is not None
        compiled = installation.plan.get("compiled_execution_plans")
        assert isinstance(compiled, dict)
        payload = compiled[nodes[0]]
        assert payload["identity"]["recipe_revision_sha256"] == revision.content_digest
        RecipeInstallPayload.model_validate(
            {
                "installation_id": operation.owner_id,
                "plan_digest": plan.plan_digest,
                "expected_bytes": 120,
                "compiled_execution_plan": payload,
            }
        )


def test_fresh_alembic_head_postgres_runs_canonical_recipe_lifecycle(
    tmp_path: Path, postgres_engine
) -> None:
    """Prove migrations alone support the canonical operational graph."""

    control_root = Path(__file__).resolve().parents[1]
    config = Config(control_root / "alembic.ini")
    config.set_main_option("script_location", str(control_root / "migrations"))
    config.set_main_option(
        "sqlalchemy.url",
        postgres_engine.url.render_as_string(hide_password=False),
    )
    command.upgrade(config, "head")

    with postgres_engine.connect() as connection:
        metadata_differences = compare_metadata(
            MigrationContext.configure(connection), Base.metadata
        )
    assert [
        difference
        for difference in metadata_differences
        if difference[0] in {"add_fk", "remove_fk"}
    ] == []

    expected_foreign_keys = {
        ("cluster_mappings", ("recipe_revision_id",)): (
            "catalog_document_revisions",
            ("id",),
        ),
        ("recipe_builds", ("recipe_revision_id",)): (
            "catalog_document_revisions",
            ("id",),
        ),
        ("recipe_installations", ("recipe_revision_id",)): (
            "catalog_document_revisions",
            ("id",),
        ),
        ("recipe_installations", ("mapping_id",)): ("cluster_mappings", ("id",)),
        ("recipe_installations", ("recipe_build_id",)): ("recipe_builds", ("id",)),
        ("installation_nodes", ("installation_id",)): (
            "recipe_installations",
            ("id",),
        ),
        ("recipe_runs", ("installation_id",)): ("recipe_installations", ("id",)),
        ("recipe_runs", ("mapping_id",)): ("cluster_mappings", ("id",)),
        ("run_nodes", ("run_id",)): ("recipe_runs", ("id",)),
        ("artifact_jobs", ("run_id",)): ("recipe_runs", ("id",)),
    }
    inspector = inspect(postgres_engine)
    assert "local_recipe_revisions" not in inspector.get_table_names()
    for (table, columns), (
        target_table,
        target_columns,
    ) in expected_foreign_keys.items():
        matching = [
            foreign_key
            for foreign_key in inspector.get_foreign_keys(table)
            if tuple(foreign_key["constrained_columns"]) == columns
        ]
        assert len(matching) == 1
        assert matching[0]["referred_table"] == target_table
        assert tuple(matching[0]["referred_columns"]) == target_columns

    sessions, service, _queue, mapping_id, build_id, nodes = setup_services(
        tmp_path,
        engine=postgres_engine,
        create_schema=False,
    )
    installation = installed_recipe(
        service,
        mapping_id,
        build_id,
        nodes,
        request_id="fresh-alembic-canonical-install",
    )
    run = started_recipe(
        sessions,
        service,
        installation.owner_id,
        nodes,
        request_id="fresh-alembic-canonical-run",
    )

    with sessions() as session:
        installation_row = session.get(RecipeInstallation, installation.owner_id)
        run_row = session.get(RecipeRun, run.owner_id)
        revisions = tuple(
            session.scalars(
                select(CatalogDocumentRevision).order_by(CatalogDocumentRevision.kind)
            )
        )
        assert installation_row is not None
        assert run_row is not None
        assert [revision.kind for revision in revisions] == ["model", "recipe"]
        assert installation_row.state == "installed"
        assert run_row.state == "running"
        assert run_row.installation_id == installation_row.id
        assert run_row.mapping_id == installation_row.mapping_id == mapping_id
        canonical_revision = session.get(
            CatalogDocumentRevision, installation_row.recipe_revision_id
        )
        assert canonical_revision is not None
        assert canonical_revision.kind == "recipe"
        assert canonical_revision.state == "active"

    with pytest.raises(IntegrityError), sessions.begin() as session:
        installation_row = session.get(RecipeInstallation, installation.owner_id)
        assert installation_row is not None
        installation_row.recipe_revision_id = "missing-canonical-revision"
        session.flush()
