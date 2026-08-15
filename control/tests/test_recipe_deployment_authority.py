from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from vonk_control.catalog_service import CatalogService, RecipeDraftInput
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.models import AgentNode, Base, ClusterMapping


def migration_config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[1]
    value = Config(root / "alembic.ini")
    value.set_main_option("script_location", str(root / "migrations"))
    value.set_main_option("sqlalchemy.url", database_url)
    return value


def test_recipe_deployment_authority_follows_recipe_catalog() -> None:
    script = ScriptDirectory.from_config(migration_config("sqlite://"))
    assert script.get_revision("0016_recipe_deployment_authority").down_revision == (
        "0015_recipe_catalog"
    )


def test_migration_backfills_legacy_authority_and_allows_recipe_authority(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'authority.sqlite'}"
    config = migration_config(url)
    command.upgrade(config, "0015_recipe_catalog")
    engine = create_engine(url)
    now = "2026-08-07 12:00:00"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO package_rollouts "
                "(id,deployment_id,deployment_digest,release_digest,base_commit,"
                "policy_digest,tuf_target_digest,fleet_digest,topology_digest,plan_digest,"
                "state,actor,current_batch,created_at,updated_at) VALUES "
                "('10000000-0000-4000-8000-000000000001','demo',:digest,:digest,:commit,"
                ":digest,:digest,:digest,:digest,:plan,'planned','admin',0,:now,:now)"
            ),
            {
                "digest": "a" * 64,
                "plan": "b" * 64,
                "commit": "c" * 40,
                "now": now,
            },
        )
    command.upgrade(config, "head")
    columns = {
        column["name"]: column
        for column in inspect(engine).get_columns("package_rollouts")
    }
    assert columns["base_commit"]["nullable"] is True
    assert columns["recipe_revision_id"]["nullable"] is True
    assert columns["authority_digest"]["nullable"] is False
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT authority_digest,recipe_revision_id FROM package_rollouts")
        ).one()
    assert len(row.authority_digest) == 64
    assert row.recipe_revision_id is None


def test_resolved_recipe_maps_without_git_remote(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    clock = lambda: datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    catalog = CatalogService(sessions, clock=clock)
    node_id = "spk_" + "1" * 32
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=node_id,
                state="active",
                architecture="linux-arm64",
                capabilities=["runtime.vonk.v1"],
            )
        )
    document = json.loads(
        (Path(__file__).parent / "fixtures/global/recipe-v1-minimal.json").read_text()
    )
    draft = catalog.create_recipe(
        "admin", RecipeDraftInput(slug="qwen3-vllm", document=document)
    )
    resolved = catalog.resolve(draft.recipe_id, draft.revision_number, "admin")
    service = ClusterMappingService(sessions)

    plan = service.plan(
        resolved.id,
        "solo",
        (node_id,),
        parameters={},
    )
    mapping_id = service.materialize(plan, actor="admin", now=clock())

    assert plan.recipe_revision_id == resolved.id
    assert len(plan.placement_digest) == 64
    with sessions() as session:
        mapping = session.get(ClusterMapping, mapping_id)
        assert mapping is not None and mapping.generation == 1
