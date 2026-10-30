"""A cached set is held when it is the set a protected recipe revision resolves to.

The resolution is the model cache's own (``resolve_recipe_set``, the function a
download uses), so a recipe's file selection and its models' dependencies are
accounted for exactly as they were when the set was built. Sets that merely share
a file or a dependency with a used model are not held.
"""

import copy
import hashlib
import json
import uuid
from datetime import UTC, datetime
from importlib.resources import files
from typing import cast

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_control.artifact_reference_scan import model_set_reference_findings
from vonk_control.auth import CursorCodec
from vonk_control.catalog_service import CatalogService
from vonk_control.model_cache.resolution import resolve_recipe_set
from vonk_control.models import (
    Base,
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
    CatalogRecipeModelReference,
    FleetProfile,
    ModelCacheSet,
    ModelCacheSetArtifact,
    RecipeInstallation,
)
from vonk_control.recipe_execution_contract import parse_stored_installation_plan
from vonk_forge_contracts import (
    ModelDefinition,
    RecipeDefinition,
    document_sha256,
)

from .test_removal_review_budget import _world

NOW = datetime(2026, 10, 30, tzinfo=UTC)
PUBLISHER = "vonk-forge"


def _example(name: str) -> dict:
    return json.loads(
        files("vonk_forge_contracts").joinpath("examples", name).read_text("utf-8")
    )


def _doc(session, kind, publisher, slug, number, document, *, head=True):
    root = (
        session.query(CatalogDocument)
        .filter_by(kind=kind, publisher=publisher, slug=slug)
        .one_or_none()
    )
    if root is None:
        root = CatalogDocument(
            id=str(uuid.uuid4()),
            kind=kind,
            publisher=publisher,
            slug=slug,
            title=slug,
            created_by="t",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(root)
        session.flush()
    rev = CatalogDocumentRevision(
        id=str(uuid.uuid4()),
        document_id=root.id,
        kind=kind,
        publisher=publisher,
        slug=slug,
        revision_number=number,
        schema_version=2,
        state="active",
        document=document,
        content_digest=document_sha256(document),
        projected={},
        created_by="t",
        created_at=NOW,
    )
    session.add(rev)
    session.flush()
    h = (
        session.query(CatalogDocumentHead)
        .filter_by(kind=kind, publisher=publisher, slug=slug)
        .one_or_none()
    )
    if h is None:
        session.add(
            CatalogDocumentHead(
                kind=kind, publisher=publisher, slug=slug, active_revision_id=rev.id
            )
        )
    else:
        h.active_revision_id = rev.id
    session.flush()
    return rev


@pytest.fixture
def sessions() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def model(
    session: Session,
    slug: str,
    names: list[str],
    *,
    depends_on: tuple[tuple[str, str], ...] = (),
    version: str = "1.0.0",
    number: int = 1,
    upstream: str = "0" * 40,
) -> str:
    """A catalog model with one file per name; returns its content digest."""
    document = _example("model-definition.json")
    document["identity"].update(publisher=PUBLISHER, slug=slug, version=version)
    document["identity"]["model"].update(publisher=PUBLISHER, slug=slug)
    document["source"] = {
        "repository": f"https://huggingface.co/{PUBLISHER}/{slug}",
        "revision": upstream,
    }
    document["files"] = [
        {
            "id": name,
            "path": f"{name}.bin",
            "sha256": hashlib.sha256(name.encode()).hexdigest(),
            "size_bytes": 3,
            "roles": ["weights"],
        }
        for name in names
    ]
    document["dependencies"] = [
        {"kind": "model", "publisher": PUBLISHER, "slug": dep, "content_sha256": digest}
        for dep, digest in depends_on
    ]
    clean = ModelDefinition.model_validate(document).model_dump(mode="json")
    revision = _doc(session, "model", PUBLISHER, slug, number, clean)
    return revision.content_digest


def recipe(
    session: Session, slug: str, model_slug: str, digest: str, file_ids: list[str]
) -> str:
    """A recipe selecting these files of one model; returns its revision id."""
    document = _example("recipe-source-build.json")
    document["identity"].update(publisher=PUBLISHER, slug=slug)
    selection = document["models"][0]
    selection["model"].update(
        publisher=PUBLISHER, slug=model_slug, content_sha256=digest
    )
    template = selection["files"][0]
    selection["files"] = [
        {
            **template,
            "id": f"sel-{file_id}",
            "file_id": file_id,
            "mount": {"target": f"/models/{file_id}"},
        }
        for file_id in file_ids
    ]
    clean = RecipeDefinition.model_validate(document).model_dump(mode="json")
    revision = _doc(session, "recipe", PUBLISHER, slug, 1, clean)
    model_revision = session.scalars(
        select(CatalogDocumentRevision).where(
            CatalogDocumentRevision.kind == "model",
            CatalogDocumentRevision.content_digest == digest,
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
            model_slug=model_slug,
            model_content_digest=digest,
        )
    )
    return revision.id


def profile(session: Session, recipe_slug: str) -> None:
    session.add(
        FleetProfile(
            number=1,
            name="P",
            installation_policy="keep-cached",
            assignments=[
                {
                    "recipe_selector": f"{PUBLISHER}/{recipe_slug}",
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


def cache_resolved(session: Session, revision_id: str) -> str:
    """Cache the set a recipe revision resolves to, as a download would."""
    manifest = resolve_recipe_set(session, revision_id)
    session.add(
        ModelCacheSet(
            artifact_set_sha256=manifest.digest,
            schema_version=2,
            model_content_sha256=manifest.model_content_sha256,
            recipe_revision_sha256=manifest.recipe_revision_sha256,
            manifest=manifest.document(),
            expected_bytes=1,
            verified_bytes=1,
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
            artifact_set_sha256=manifest.digest,
            artifact_key=item.key,
            artifact_sha256=item.sha256,
            path=item.path,
        )
        for item in manifest.artifacts
    )
    return manifest.digest


def test_a_shared_dependency_does_not_hold_a_sibling_models_set(sessions) -> None:
    """Catches holding every set that contains a file a used model needs."""
    with sessions.begin() as s:
        base = model(s, "base", ["base-weights"])
        used = model(s, "used", ["used-weights"], depends_on=(("base", base),))
        sibling = model(s, "sibling", ["sib-weights"], depends_on=(("base", base),))
        used_recipe = recipe(s, "recipe-used", "used", used, ["used-weights"])
        sibling_recipe = recipe(
            s, "recipe-sibling", "sibling", sibling, ["sib-weights"]
        )
        profile(s, "recipe-used")
        kept = cache_resolved(s, used_recipe)
        free = cache_resolved(s, sibling_recipe)
    assert kept != free
    with sessions() as s:
        findings = model_set_reference_findings(s, (kept, free))
    assert findings[kept]
    assert not findings[free]


def test_a_profile_holds_exactly_the_set_its_recipe_resolves_to(sessions) -> None:
    """A recipe's file selection and its models' dependencies are in that set."""
    with sessions.begin() as s:
        base = model(s, "base", ["base-weights"])
        used = model(s, "used", ["a1", "a2"], depends_on=(("base", base),))
        selected = recipe(s, "recipe-a", "used", used, ["a1"])
        whole = recipe(s, "recipe-b", "used", used, ["a1", "a2"])
        profile(s, "recipe-a")
        kept = cache_resolved(s, selected)
        other = cache_resolved(s, whole)
    with sessions() as s:
        findings = model_set_reference_findings(s, (kept, other))
    assert findings[kept]
    assert not findings[other]


@pytest.mark.parametrize("later_refreshes", [0, 1])
def test_a_refreshed_model_with_identical_files_stays_held(
    sessions, later_refreshes
) -> None:
    """The profile's recipe pins the refreshed model; the set was cached for the first."""
    with sessions.begin() as s:
        first = model(s, "tiny", ["w"], version="1.0.0")
        refreshed = model(s, "tiny", ["w"], version="1.0.1", number=2)
        for number in range(later_refreshes):
            model(s, "tiny", ["w"], version=f"1.0.{2 + number}", number=3 + number)
        assert first != refreshed
        cached = cache_resolved(s, recipe(s, "recipe-first", "tiny", first, ["w"]))
        recipe(s, "recipe-refreshed", "tiny", refreshed, ["w"])
        profile(s, "recipe-refreshed")
    with sessions() as s:
        assert model_set_reference_findings(s, (cached,))[cached]


def test_a_refresh_keeps_the_old_set_until_the_new_set_is_cached(sessions) -> None:
    """Identical bytes, new upstream revision: the new set is another digest."""
    with sessions.begin() as s:
        first = model(s, "tiny", ["w"], version="1.0.0")
        refreshed = model(
            s, "tiny", ["w"], version="1.0.1", number=2, upstream="1" * 40
        )
        old = cache_resolved(s, recipe(s, "recipe-first", "tiny", first, ["w"]))
        new_recipe = recipe(s, "recipe-refreshed", "tiny", refreshed, ["w"])
        profile(s, "recipe-refreshed")
        assert resolve_recipe_set(s, new_recipe).digest != old
    with sessions() as s:
        assert model_set_reference_findings(s, (old,))[old]
    with sessions.begin() as s:
        cache_resolved(s, new_recipe)
    with sessions() as s:
        assert not model_set_reference_findings(s, (old,))[old]


def test_a_retracted_recipe_still_holds_its_set_and_only_its_set(sessions) -> None:
    """Catches a recipe with no active head either holding everything or nothing."""
    with sessions.begin() as s:
        used = model(s, "used", ["used-weights"])
        other = model(s, "other", ["other-weights"])
        kept = cache_resolved(
            s, recipe(s, "recipe-used", "used", used, ["used-weights"])
        )
        free = cache_resolved(
            s, recipe(s, "recipe-other", "other", other, ["other-weights"])
        )
        profile(s, "recipe-used")
    CatalogService(
        sessions, clock=lambda: NOW, cursors=CursorCodec(b"c" * 32)
    ).retract_recipes_absent_from([])
    with sessions() as s:
        heads = s.scalars(
            select(CatalogDocumentHead).where(CatalogDocumentHead.kind == "recipe")
        ).all()
        assert all(head.active_revision_id is None for head in heads)
        findings = model_set_reference_findings(s, (kept, free))
    assert findings[kept]
    assert not findings[free]


def test_an_unresolvable_recipe_holds_only_the_sets_of_its_models(sessions) -> None:
    with sessions.begin() as s:
        used = model(s, "used", ["used-weights"])
        other = model(s, "other", ["other-weights"])
        kept = cache_resolved(
            s, recipe(s, "recipe-used", "used", used, ["used-weights"])
        )
        free = cache_resolved(
            s, recipe(s, "recipe-other", "other", other, ["other-weights"])
        )
        profile(s, "recipe-used")
        # The used model's document becomes unreadable: the recipe cannot resolve.
        s.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.content_digest == used)
            .values(document={})
        )
    with sessions() as s:
        findings = model_set_reference_findings(s, (kept, free))
    assert findings[kept]
    assert not findings[free]


def test_a_selector_with_no_revision_at_all_holds_every_set(sessions) -> None:
    with sessions.begin() as s:
        other = model(s, "other", ["other-weights"])
        free = cache_resolved(
            s, recipe(s, "recipe-other", "other", other, ["other-weights"])
        )
        profile(s, "missing-recipe")
    with sessions() as s:
        assert model_set_reference_findings(s, (free,))[free]


def test_installation_holds_the_set_its_plan_names(tmp_path):
    """Catches ownership by first requester: the plan names these sets.

    A second set holds the same bytes and no plan names it; it is not held.
    """
    w = _world(tmp_path, "alone", unrelated=None)
    with w.sessions.begin() as s:
        inst = s.scalars(select(RecipeInstallation)).one()
        plan = parse_stored_installation_plan(inst.plan, for_uninstall=True)
        used = {
            c.identity.model_artifact_set_sha256
            for c in plan.compiled_execution_plans.values()
        }
        template = s.scalars(
            select(ModelCacheSet).where(ModelCacheSet.model_content_sha256 == w.target)
        ).first()
        first = hashlib.sha256(b"first-requester").hexdigest()
        target_revision = s.scalars(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.content_digest == w.target
            )
        ).first()
        assert target_revision is not None
        assert template is not None
        duplicate = hashlib.sha256(b"duplicate-bytes").hexdigest()
        for digest in used | {duplicate}:
            manifest = copy.deepcopy(template.manifest)
            manifest["model_content_sha256"] = first
            # The set serves the target through a verified binding, as the
            # removal's selected sets do; its first requester is another model.
            manifest["verified_models"] = [
                {
                    "kind": "model",
                    "publisher": target_revision.publisher,
                    "slug": target_revision.slug,
                    "content_sha256": w.target,
                }
            ]
            s.add(
                ModelCacheSet(
                    artifact_set_sha256=digest,
                    schema_version=2,
                    model_content_sha256=first,
                    recipe_revision_sha256=None,
                    manifest=manifest,
                    expected_bytes=1,
                    verified_bytes=1,
                    state="cached",
                    protected=False,
                    protected_reasons=[],
                    created_at=w.now,
                    updated_at=w.now,
                    last_accessed_at=w.now,
                )
            )
            s.flush()
            s.add_all(
                ModelCacheSetArtifact(
                    artifact_set_sha256=digest,
                    artifact_key=a["key"],
                    artifact_sha256=a["sha256"],
                    path=a["path"],
                )
                for a in cast(list[dict[str, str]], manifest["artifacts"])
            )
    with w.sessions() as s:
        findings = model_set_reference_findings(s, (*used, duplicate))
    w.cache.close()
    assert all(findings[d] for d in used), "the running installation's set is unused"
    assert not findings[duplicate]
