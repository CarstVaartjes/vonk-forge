"""A refreshed model keeps the set its bytes were first cached under.

A refresh with identical files reuses the first requester's set and records the
new content only as a verified binding (model_cache/revisions.py). A profile on
the refreshed recipe, or an installation that runs that set, must still hold it:
set ownership is the rule ``model_sets()`` uses, first requester or verified
binding, never the first requester alone.
"""

import copy
import hashlib
import json
import uuid
from datetime import UTC, datetime
from importlib.resources import files

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_control.artifact_reference_scan import model_set_reference_findings
from vonk_control.model_cache_contract import CacheManifest
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
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, document_sha256

from .test_removal_review_budget import _world

NOW = datetime(2026, 10, 30, tzinfo=UTC)


def _example(name):
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


@pytest.mark.parametrize("later_refreshes", [0, 1])
def test_refreshed_model_profile_keeps_first_requested_set(later_refreshes):
    """The profile's model M2 is refreshed ``later_refreshes`` more times.

    The set then records only the newest binding (M3), none of M2, yet it still
    holds M2's files.
    """
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as s:
        m = _example("model-definition.json")
        m1 = ModelDefinition.model_validate(m).model_dump(mode="json")
        m2doc = copy.deepcopy(m)
        m2doc["identity"]["version"] = "1.0.1"
        m2 = ModelDefinition.model_validate(m2doc).model_dump(mode="json")
        pub, slug = m["identity"]["publisher"], m["identity"]["slug"]
        r1 = _doc(s, "model", pub, slug, 1, m1)
        r2 = _doc(s, "model", pub, slug, 2, m2)
        newest = r2
        for number in range(3, 3 + later_refreshes):
            later = copy.deepcopy(m)
            later["identity"]["version"] = f"1.0.{number}"
            newest = _doc(
                s,
                "model",
                pub,
                slug,
                number,
                ModelDefinition.model_validate(later).model_dump(mode="json"),
            )
        assert r1.content_digest != r2.content_digest
        recipe = _example("recipe-source-build.json")
        sel = recipe["models"][0]
        sel["model"].update(publisher=pub, slug=slug, content_sha256=r2.content_digest)
        rdoc = RecipeDefinition.model_validate(recipe).model_dump(mode="json")
        rpub, rslug = recipe["identity"]["publisher"], recipe["identity"]["slug"]
        rrev = _doc(s, "recipe", rpub, rslug, 1, rdoc)
        s.add(
            CatalogRecipeModelReference(
                recipe_revision_id=rrev.id,
                recipe_kind="recipe",
                selection_id=str(sel["id"]),
                model_revision_id=r2.id,
                model_kind="model",
                model_publisher=pub,
                model_slug=slug,
                model_content_digest=r2.content_digest,
            )
        )
        s.add(
            FleetProfile(
                number=1,
                name="P",
                installation_policy="keep-cached",
                assignments=[
                    {
                        "recipe_selector": f"{rpub}/{rslug}",
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
        # The refreshed model reuses the set its bytes were first cached under:
        # first requester M1, verified binding M2 (model_cache/revisions.py).
        artifacts = [
            {
                "key": f["id"],
                "id": f["id"],
                "path": f["path"],
                "kind": "file",
                "repository": None,
                "source": "file:///source",
                "revision": None,
                "sha256": f["sha256"],
                "download_bytes": f["size_bytes"],
                "roles": ["model"],
                "model_content_sha256": r1.content_digest,
            }
            for f in m1["files"]
        ]
        manifest = CacheManifest.model_validate(
            {
                "schema_version": 2,
                "source_policy": "nas-first",
                "model_content_sha256": r1.content_digest,
                "recipe_revision_sha256": None,
                "model_definition_ref": None,
                "model_content_digests": [r1.content_digest],
                "artifacts": artifacts,
                "verified_models": [
                    {
                        "kind": "model",
                        "publisher": pub,
                        "slug": slug,
                        "content_sha256": newest.content_digest,
                    }
                ],
            }
        ).model_dump(mode="json")
        set_digest = hashlib.sha256(b"s1").hexdigest()
        s.add(
            ModelCacheSet(
                artifact_set_sha256=set_digest,
                schema_version=2,
                model_content_sha256=r1.content_digest,
                recipe_revision_sha256=None,
                manifest=manifest,
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
        s.flush()
        s.add_all(
            ModelCacheSetArtifact(
                artifact_set_sha256=set_digest,
                artifact_key=a["key"],
                artifact_sha256=a["sha256"],
                path=a["path"],
            )
            for a in artifacts
        )
    with sessions() as s:
        findings = model_set_reference_findings(s, (set_digest,))
    assert findings[set_digest], (
        "the saved profile's model bytes are reviewed as unused"
    )


def test_installation_on_refreshed_model_holds_its_set(tmp_path):
    """Catches ownership by first requester or by plan text.

    A second set holds the same bytes and no plan names it; a running
    installation of the model still keeps it, because the files are the model's.
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
        assert template is not None
        duplicate = hashlib.sha256(b"duplicate-bytes").hexdigest()
        used = used | {duplicate}
        for digest in used:
            manifest = copy.deepcopy(template.manifest)
            manifest["model_content_sha256"] = first
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
            # The set holds the bytes the installation's model declares.
            declared = {
                file.sha256
                for file in ModelDefinition.model_validate(
                    s.scalars(
                        select(CatalogDocumentRevision.document).where(
                            CatalogDocumentRevision.kind == "model",
                            CatalogDocumentRevision.content_digest == w.target,
                        )
                    ).one()
                ).files
            }
            s.add_all(
                ModelCacheSetArtifact(
                    artifact_set_sha256=digest,
                    artifact_key=f"f{n}",
                    artifact_sha256=sha,
                    path=f"model-{n}.safetensors",
                )
                for n, sha in enumerate(sorted(declared))
            )
    with w.sessions() as s:
        findings = model_set_reference_findings(s, tuple(used))
    w.cache.close()
    assert all(findings[d] for d in used), "the running installation's set is unused"
