from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from vonk_control.models import Base, LocalRecipe, LocalRecipeRevision


def config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[1]
    value = Config(root / "alembic.ini")
    value.set_main_option("script_location", str(root / "migrations"))
    value.set_main_option("sqlalchemy.url", database_url)
    return value


def test_recipe_catalog_is_the_linear_head() -> None:
    script = ScriptDirectory.from_config(config("sqlite://"))
    assert script.get_heads() == ["0021_browser_authentication"]
    assert script.get_revision("0015_recipe_catalog").down_revision == (
        "0014_package_action_plans"
    )


def test_recipe_catalog_tables_upgrade_and_downgrade(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'catalog.sqlite'}"
    command.upgrade(config(url), "head")
    engine = create_engine(url)
    catalog_tables = {
        "package_families",
        "recipe_source_bundles",
        "local_recipes",
        "local_recipe_revisions",
        "recipe_imports",
        "recipe_import_items",
        "recipe_global_links",
        "recipe_test_reports",
        "recipe_builds",
        "cluster_mappings",
        "cluster_mapping_nodes",
    }
    assert catalog_tables <= set(inspect(engine).get_table_names())

    command.downgrade(config(url), "0014_package_action_plans")
    names = set(inspect(engine).get_table_names())
    assert not catalog_tables & names
    assert "package_action_plans" in names


def test_recipe_revision_constraints_are_enforced(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'constraints.sqlite'}"
    command.upgrade(config(url), "head")
    engine = create_engine(url)
    now = "2026-08-07 12:00:00"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO local_recipes "
                "(id,slug,title,description,source_kind,created_by,created_at,updated_at) "
                "VALUES ('00000000-0000-4000-8000-000000000001','demo','Demo','Demo',"
                "'local','admin',:now,:now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO local_recipe_revisions "
                "(id,recipe_id,revision_number,lifecycle,schema_version,document,"
                "content_sha256,created_by,created_at) VALUES "
                "('00000000-0000-4000-8000-000000000002',"
                "'00000000-0000-4000-8000-000000000001',1,'resolved',1,'{}',"
                ":digest,'admin',:now)"
            ),
            {"digest": "a" * 64, "now": now},
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO local_recipe_revisions "
                    "(id,recipe_id,revision_number,lifecycle,schema_version,document,"
                    "content_sha256,created_by,created_at) VALUES "
                    "('00000000-0000-4000-8000-000000000003',"
                    "'00000000-0000-4000-8000-000000000001',2,'resolved',1,'{}',"
                    ":digest,'admin',:now)"
                ),
                {"digest": "a" * 64, "now": now},
            )


def test_resolved_revision_requires_digest_and_known_lifecycle(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'lifecycle.sqlite'}"
    command.upgrade(config(url), "head")
    engine = create_engine(url)
    now = "2026-08-07 12:00:00"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO local_recipes "
                "(id,slug,title,description,source_kind,created_by,created_at,updated_at) "
                "VALUES ('00000000-0000-4000-8000-000000000010','demo','Demo','Demo',"
                "'local','admin',:now,:now)"
            ),
            {"now": now},
        )
        for lifecycle, digest in (("unknown", None), ("resolved", None)):
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO local_recipe_revisions "
                        "(id,recipe_id,revision_number,lifecycle,schema_version,document,"
                        "content_sha256,created_by,created_at) VALUES "
                        "(:id,'00000000-0000-4000-8000-000000000010',1,:lifecycle,"
                        "1,'{}',:digest,'admin',:now)"
                    ),
                    {
                        "id": "00000000-0000-4000-8000-000000000011",
                        "lifecycle": lifecycle,
                        "digest": digest,
                        "now": now,
                    },
                )


def test_sqlalchemy_metadata_matches_catalog_and_resolved_rows_are_immutable() -> None:
    assert {
        "package_families",
        "recipe_source_bundles",
        "local_recipes",
        "local_recipe_revisions",
        "recipe_imports",
        "recipe_import_items",
        "recipe_global_links",
        "recipe_test_reports",
        "recipe_builds",
        "cluster_mappings",
        "cluster_mapping_nodes",
    } <= set(Base.metadata.tables)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        recipe = LocalRecipe(
            slug="demo",
            title="Demo",
            description="Demo",
            source_kind="local",
            created_by="admin",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(recipe)
        session.flush()
        revision = LocalRecipeRevision(
            recipe_id=recipe.id,
            revision_number=1,
            lifecycle="resolved",
            schema_version=1,
            document={"schema_version": 1},
            content_sha256="c" * 64,
            created_by="admin",
            created_at=datetime.now(UTC),
        )
        session.add(revision)
        session.commit()
        revision.document = {"schema_version": 2}
        with pytest.raises(ValueError, match="immutable"):
            session.flush()
