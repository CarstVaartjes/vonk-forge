"""A cached set is held when it serves a model a profile or workload uses.

Serving means containing every file the model needs: the files its recipe
selects, plus those of its dependencies. Sharing one small file with a used
model does not hold a set; the object stays protected by the sets that are kept.
"""

import hashlib
import json
from datetime import UTC, datetime
from importlib.resources import files

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_control.artifact_lifecycle import ArtifactReferenceUnverified
from vonk_control.artifact_reference_scan import model_set_reference_findings
from vonk_control.auth import CursorCodec
from vonk_control.catalog_service import CatalogService
from vonk_control.model_cache_contract import CacheManifest
from vonk_control.models import (
    Base,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    CatalogRecipeModelReference,
    FleetProfile,
    ModelCacheSet,
    ModelCacheSetArtifact,
)
from vonk_forge_contracts import ModelDefinition, RecipeDefinition

from .test_removal_refreshed_model import _doc

NOW = datetime(2026, 10, 30, tzinfo=UTC)
PUBLISHER = "vonk-forge"


def _hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _example(name: str) -> dict:
    return json.loads(
        files("vonk_forge_contracts").joinpath("examples", name).read_text("utf-8")
    )


@pytest.fixture
def sessions() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _model(
    session: Session,
    slug: str,
    names: list[str],
    depends_on: tuple[tuple[str, str], ...] = (),
) -> tuple[str, dict[str, str]]:
    """A catalog model with one file per name; returns (content digest, name -> sha)."""
    document = _example("model-definition.json")
    document["identity"].update(publisher=PUBLISHER, slug=slug)
    document["identity"]["model"].update(publisher=PUBLISHER, slug=slug)
    shas = {name: _hex(name) for name in names}
    document["files"] = [
        {
            "id": name,
            "path": f"{name}.bin",
            "sha256": sha,
            "size_bytes": 3,
            "roles": ["weights"],
        }
        for name, sha in shas.items()
    ]
    document["dependencies"] = [
        {"kind": "model", "publisher": PUBLISHER, "slug": dep, "content_sha256": digest}
        for dep, digest in depends_on
    ]
    clean = ModelDefinition.model_validate(document).model_dump(mode="json")
    return _doc(session, "model", PUBLISHER, slug, 1, clean).content_digest, shas


def _recipe(session: Session, slug: str, model: tuple[str, str], file_id: str) -> None:
    """A recipe that selects one file of one model; ``model`` is (slug, digest)."""
    document = _example("recipe-source-build.json")
    document["identity"].update(publisher=PUBLISHER, slug=slug)
    selection = document["models"][0]
    selection["model"].update(
        publisher=PUBLISHER, slug=model[0], content_sha256=model[1]
    )
    selection["files"][0]["file_id"] = file_id
    clean = RecipeDefinition.model_validate(document).model_dump(mode="json")
    revision = _doc(session, "recipe", PUBLISHER, slug, 1, clean)
    model_revision = session.scalars(
        select(CatalogDocumentRevision).where(
            CatalogDocumentRevision.kind == "model",
            CatalogDocumentRevision.content_digest == model[1],
        )
    ).one()
    session.add(
        CatalogRecipeModelReference(
            recipe_revision_id=revision.id,
            recipe_kind="recipe",
            selection_id=str(selection["id"]),
            model_revision_id=model_revision.id,
            model_kind="model",
            model_publisher=PUBLISHER,
            model_slug=model[0],
            model_content_digest=model[1],
        )
    )


def _profile(session: Session, recipe: str) -> None:
    session.add(
        FleetProfile(
            number=1,
            name="P",
            installation_policy="keep-cached",
            assignments=[
                {
                    "recipe_selector": f"{PUBLISHER}/{recipe}",
                    "spark_ids": ["spk_" + "1" * 32],
                    "desired_state": "running",
                    "option_choices": {},
                }
            ],
            created_by="t",
            created_at=NOW,
            updated_at=NOW,
        )
    )


def _cached(session: Session, name: str, shas: list[str]) -> str:
    """A cached set holding exactly these objects."""
    digest = _hex(f"set/{name}")
    artifacts = [
        {
            "key": f"f{n}",
            "id": f"f{n}",
            "path": f"f{n}.bin",
            "kind": "file",
            "repository": None,
            "source": "file:///source",
            "revision": None,
            "sha256": sha,
            "download_bytes": 3,
            "roles": ["model"],
            "model_content_sha256": _hex(name),
        }
        for n, sha in enumerate(shas)
    ]
    manifest = CacheManifest.model_validate(
        {
            "schema_version": 2,
            "source_policy": "nas-first",
            "model_content_sha256": _hex(name),
            "recipe_revision_sha256": None,
            "model_definition_ref": None,
            "model_content_digests": [_hex(name)],
            "artifacts": artifacts,
        }
    ).model_dump(mode="json")
    session.add(
        ModelCacheSet(
            artifact_set_sha256=digest,
            schema_version=2,
            model_content_sha256=_hex(name),
            recipe_revision_sha256=None,
            manifest=manifest,
            expected_bytes=3,
            verified_bytes=3,
            state="cached",
            protected=False,
            protected_reasons=[],
            created_at=NOW,
            updated_at=NOW,
            last_accessed_at=NOW,
        )
    )
    session.flush()
    session.add_all(
        ModelCacheSetArtifact(
            artifact_set_sha256=digest,
            artifact_key=item["key"],
            artifact_sha256=item["sha256"],
            path=item["path"],
        )
        for item in artifacts
    )
    return digest


def test_a_shared_small_file_does_not_hold_an_unrelated_set(sessions) -> None:
    """Catches holding every set that merely contains a file a used model declares."""
    with sessions.begin() as s:
        used, used_files = _model(s, "used", ["weights-a", "tokenizer"])
        # The other model ships the same tokenizer file.
        _, other_files = _model(s, "other", ["weights-b", "tokenizer"])
        _recipe(s, "recipe-used", ("used", used), "weights-a")
        _profile(s, "recipe-used")
        kept = _cached(s, "used", [used_files["weights-a"], used_files["tokenizer"]])
        free = _cached(s, "other", [other_files["weights-b"], other_files["tokenizer"]])
    with sessions() as s:
        findings = model_set_reference_findings(s, (kept, free))
    assert findings[kept]
    assert not findings[free]


def test_a_retracted_recipe_still_holds_its_model_and_only_its_model(
    sessions,
) -> None:
    """Catches a recipe with no active head either holding everything or nothing."""
    with sessions.begin() as s:
        used, used_files = _model(s, "used", ["weights-a"])
        _, other_files = _model(s, "other", ["weights-b"])
        _recipe(s, "recipe-used", ("used", used), "weights-a")
        _profile(s, "recipe-used")
        kept = _cached(s, "used", [used_files["weights-a"]])
        free = _cached(s, "other", [other_files["weights-b"]])
    CatalogService(
        sessions, clock=lambda: NOW, cursors=CursorCodec(b"c" * 32)
    ).retract_recipes_absent_from([])
    with sessions() as s:
        head = s.scalars(
            select(CatalogDocumentHead).where(CatalogDocumentHead.kind == "recipe")
        ).one()
        assert head.active_revision_id is None
        findings = model_set_reference_findings(s, (kept, free))
    assert findings[kept]
    assert not findings[free]


def test_a_selector_with_no_revision_at_all_holds_every_set(sessions) -> None:
    with sessions.begin() as s:
        _, other_files = _model(s, "other", ["weights-b"])
        _profile(s, "missing-recipe")
        free = _cached(s, "other", [other_files["weights-b"]])
    with sessions() as s:
        findings = model_set_reference_findings(s, (free,))
    assert findings[free]


def test_a_used_model_keeps_the_sets_of_its_dependencies(sessions) -> None:
    """Catches requiring or holding only the used model's own files."""
    with sessions.begin() as s:
        base, base_files = _model(s, "base", ["base-weights"])
        used, used_files = _model(s, "used", ["adapter"], depends_on=(("base", base),))
        _, other_files = _model(s, "other", ["weights-b"])
        _recipe(s, "recipe-used", ("used", used), "adapter")
        _profile(s, "recipe-used")
        # The set the recipe is built into holds the model and its dependency.
        whole = _cached(s, "whole", [used_files["adapter"], base_files["base-weights"]])
        # The dependency cached on its own is a used model's set as well.
        alone = _cached(s, "alone", [base_files["base-weights"]])
        # Only the used model's own file: not the set a recipe run is built from.
        partial = _cached(s, "partial", [used_files["adapter"]])
        free = _cached(s, "other", [other_files["weights-b"]])
    with sessions() as s:
        findings = model_set_reference_findings(s, (whole, alone, partial, free))
    assert findings[whole]
    assert findings[alone]
    assert not findings[partial]
    assert not findings[free]


def test_a_set_without_members_is_unverified(sessions) -> None:
    with sessions.begin() as s:
        empty = _cached(s, "empty", [])
    with sessions() as s, pytest.raises(ArtifactReferenceUnverified):
        model_set_reference_findings(s, (empty,))
