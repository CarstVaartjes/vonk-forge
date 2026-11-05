"""The two stored reference facts a removal review asks by target.

Which installations use a model set is ``recipe_installations.
model_artifact_set_sha256``; which saved profiles name a recipe is
``fleet_profile_recipes``. Both are written with the row they describe, and rows
from before they existed are filled at startup (and, for installations, by the
model-cache pass) from their own stored documents.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol.inventory import ModelStoreObject
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import (
    CatalogDocumentRevision,
    FleetProfile,
    FleetProfileRecipe,
    InstallationNode,
    RecipeInstallation,
)
from vonk_control.recipe_execution_contract import parse_stored_installation_plan
from vonk_control.reference_facts import fill_reference_facts, profiles_naming

from .test_fleet_model_removal_postgres import inventory
from .test_recipe_operations import NOW, installed_recipe, setup_services
from .test_removal_review_budget import (
    _cached_sets,
    _hex,
    _noise_models,
    _noise_recipes,
    plan_using,
)

TARGET_SET = _hex("set/target")


@dataclass
class Small:
    sessions: sessionmaker[Session]
    cache: ModelCacheService
    target: str
    target_installation: str
    target_recipe: tuple[str, str]
    other_recipe: tuple[str, str, str]
    nodes: tuple[str, ...]


def _small(tmp_path: Path) -> Small:
    """One installed target with its cached set, and one unrelated recipe."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'facts.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    sessions, lifecycle, _, mapping_id, build_id, nodes = setup_services(
        tmp_path, engine=engine, nodes=2, mapping_node_count=1
    )
    installed = installed_recipe(
        lifecycle, mapping_id, build_id, nodes[:1], request_id=str(uuid.uuid4())
    )
    with sessions.begin() as session:
        row = session.get(RecipeInstallation, installed.owner_id)
        assert row is not None and row.model_content_sha256 is not None
        revision = session.get(CatalogDocumentRevision, row.recipe_revision_id)
        assert revision is not None
        target, target_recipe = (
            row.model_content_sha256,
            (
                revision.publisher,
                revision.slug,
            ),
        )
        models = _noise_models(session, NOW, count=60)
        _cached_sets(session, [], target, NOW, count=0)
        recipes = _noise_recipes(session, models, NOW)
        other = session.get(CatalogDocumentRevision, recipes[0])
        assert other is not None
        other_recipe = (other.id, other.publisher, other.slug)
    now = NOW + timedelta(minutes=1)
    objects = [ModelStoreObject(sha256=_hex(f"spark/{n}"), bytes=3) for n in range(3)]
    for node in nodes:
        inventory(sessions, node, now, objects)
    cache = ModelCacheService(
        sessions, tmp_path / "nas", free_fraction=0, clock=lambda: now
    )
    cache._fleet_lifecycle = lifecycle
    return Small(
        sessions,
        cache,
        target,
        installed.owner_id,
        target_recipe,
        other_recipe,
        nodes,
    )


def _reused_installation(world: Small) -> str:
    """An installation on the other Spark that runs the target's set while its
    recipe and model columns name another model (identical files reused)."""
    with world.sessions.begin() as session:
        real = session.get(RecipeInstallation, world.target_installation)
        assert real is not None
        installation_id = str(uuid.uuid4())
        session.add(
            RecipeInstallation(
                id=installation_id,
                recipe_revision_id=world.other_recipe[0],
                model_content_sha256=_hex("another-model"),
                model_artifact_set_sha256=TARGET_SET,
                mapping_id=real.mapping_id,
                mapping_generation=real.mapping_generation,
                recipe_build_id=real.recipe_build_id,
                image_digest=real.image_digest,
                plan_digest=_hex("reused-plan"),
                plan=plan_using(real.plan, TARGET_SET),
                state="installed",
                actor="test",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.flush()
        session.add(
            InstallationNode(
                installation_id=installation_id,
                node_id=world.nodes[1],
                rank=0,
                role="entrypoint",
                state="installed",
                required_bytes=120,
                installed_bytes=120,
                updated_at=NOW,
            )
        )
    return installation_id


def _profile(world: Small, *recipes: tuple[str, str]) -> str:
    with world.sessions.begin() as session:
        profile = FleetProfile(
            number=1,
            name="Pins",
            installation_policy="keep-cached",
            assignments=[
                {
                    "recipe_selector": f"{publisher}/{slug}",
                    "spark_ids": [world.nodes[0]],
                    "desired_state": "running",
                    "option_choices": {},
                }
                for publisher, slug in recipes
            ],
            created_by="test",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(profile)
        session.flush()
        return profile.id


@pytest.mark.slow(30)
def test_both_reviews_find_a_reused_set_and_a_profile_naming_the_model(
    tmp_path: Path,
) -> None:
    """Catches answering by the model columns only, or reading profiles by JSON.

    The installation names the target only through its recorded model set; the
    profile names the target's recipe. A profile naming only another recipe
    keeps nothing.
    """
    world = _small(tmp_path)
    try:
        reused = _reused_installation(world)
        pins = _profile(world, world.target_recipe)
        elsewhere = _profile_number(world, 2, world.other_recipe[1:])
        review = world.cache.review_model_removal(world.target)
        assert reused in {item.owner_id for item in review.active_work}
        assert {item.owner_id for item in review.references} >= {pins}
        assert elsewhere not in {item.owner_id for item in review.references}
        fleet = world.cache.review_fleet_model_removal(world.target)
        assert fleet.fleet is not None
        assert reused in {
            effect.installation_id
            for target in fleet.fleet.targets
            for effect in target.installations
        }
    finally:
        world.cache.close()


def _profile_number(world: Small, number: int, recipe: tuple[str, str]) -> str:
    with world.sessions.begin() as session:
        profile = FleetProfile(
            number=number,
            name=f"Other {number}",
            installation_policy="keep-cached",
            assignments=[
                {
                    "recipe_selector": f"{recipe[0]}/{recipe[1]}",
                    "spark_ids": [world.nodes[0]],
                    "desired_state": "running",
                    "option_choices": {},
                }
            ],
            created_by="test",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(profile)
        session.flush()
        return profile.id


@pytest.mark.slow(30)
def test_a_profile_save_rewrites_its_recipes_in_the_same_flush(
    tmp_path: Path,
) -> None:
    """Catches a projection that misses an edit, or a save that stores a bad selector."""
    world = _small(tmp_path)
    try:
        profile_id = _profile(world, world.target_recipe)
        with world.sessions() as session:
            assert profiles_naming(session, [world.target_recipe]) == {
                profile_id: frozenset({world.target_recipe})
            }
        with world.sessions.begin() as session:
            profile = session.get(FleetProfile, profile_id)
            assert profile is not None
            profile.assignments = [
                {
                    "recipe_selector": "/".join(world.other_recipe[1:]),
                    "spark_ids": [world.nodes[0]],
                    "desired_state": "running",
                    "option_choices": {},
                }
            ]
        with world.sessions() as session:
            assert profiles_naming(session, [world.target_recipe]) == {}
            assert profiles_naming(session, profile_id=profile_id) == {
                profile_id: frozenset({world.other_recipe[1:]})
            }
        with pytest.raises((TypeError, ValueError)), world.sessions.begin() as session:
            profile = session.get(FleetProfile, profile_id)
            assert profile is not None
            profile.assignments = [{"recipe_selector": "Not A Selector"}]
    finally:
        world.cache.close()


@pytest.mark.slow(30)
def test_rows_from_before_the_facts_are_filled_and_an_upgrade_install_heals(
    tmp_path: Path,
) -> None:
    """Fault test: an upgrade adopts rows written without the facts.

    Admission records the set. Rows in the old shape (no set, no projection)
    are filled once at startup; a damaged plan stays unfilled without stopping
    startup; a projection row its profile no longer names is removed.
    """
    world = _small(tmp_path)
    try:
        with world.sessions() as session:
            row = session.get(RecipeInstallation, world.target_installation)
            assert row is not None
            assert row.model_artifact_set_sha256 is not None
            assert (
                row.model_artifact_set_sha256
                == parse_stored_installation_plan(row.plan).model_artifact_set_sha256
            )
            admitted = row.model_artifact_set_sha256
        reused = _reused_installation(world)
        pins = _profile(world, world.target_recipe)
        damaged = _reused_installation(world)
        with world.sessions.begin() as session:
            # The old shape: neither fact is recorded.
            session.execute(
                text("UPDATE recipe_installations SET model_artifact_set_sha256 = NULL")
            )
            session.execute(text("DELETE FROM fleet_profile_recipes"))
            session.execute(
                text("UPDATE recipe_installations SET plan = :plan WHERE id = :id"),
                {"plan": '{"damaged": true}', "id": damaged},
            )
        with world.sessions.begin() as session:
            filled = fill_reference_facts(session.connection())
        assert filled == 3  # two plans and one profile recipe
        with world.sessions.begin() as session:
            assert fill_reference_facts(session.connection()) == 0
            sets = dict(
                session.execute(
                    select(
                        RecipeInstallation.id,
                        RecipeInstallation.model_artifact_set_sha256,
                    )
                ).all()
            )
            assert sets[world.target_installation] == admitted
            reused_row = session.get(RecipeInstallation, reused)
            assert reused_row is not None
            assert (
                sets[reused]
                == parse_stored_installation_plan(
                    reused_row.plan
                ).model_artifact_set_sha256
            )
            assert sets[damaged] is None
            assert session.scalars(select(FleetProfileRecipe.profile_id)).all() == [
                pins
            ]
            # A row the profile no longer names is removed; its own are kept.
            session.add(
                FleetProfileRecipe(profile_id=pins, publisher="stale", slug="gone")
            )
            session.flush()
            assert fill_reference_facts(session.connection()) == 1
            assert session.scalars(select(FleetProfileRecipe.profile_id)).all() == [
                pins
            ]
            assert session.scalars(select(FleetProfileRecipe.publisher)).all() != [
                "stale"
            ]
    finally:
        world.cache.close()
