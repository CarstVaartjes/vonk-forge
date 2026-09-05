from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import Base, CatalogDocument, CatalogDocumentRevision
from vonk_control.recipe_builds import (
    RecipeBuildError,
    _canonical_build,
    derive_build_input_identity,
)

NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _sessions():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _model_document(
    *, path: str, file_digest: str, roles: list[str]
) -> dict[str, object]:
    return {
        "kind": "model",
        "source": {"repository": "owner/model", "revision": "a" * 40},
        "files": [
            {
                "id": "weights",
                "path": path,
                "sha256": file_digest,
                "size_bytes": 12,
                "roles": roles,
            }
        ],
    }


def _recipe_document(model_digest: str) -> dict[str, object]:
    return {
        "kind": "recipe",
        "models": [
            {
                "id": "primary",
                "model": {
                    "kind": "model",
                    "publisher": "owner",
                    "slug": "model",
                    "content_sha256": model_digest,
                },
                "files": [
                    {
                        "id": "weights-selector",
                        "file_id": "weights",
                        "roles": ["model"],
                    }
                ],
            }
        ],
    }


def _add_active(
    session,
    *,
    root_id: str,
    revision_id: str,
    kind: str,
    publisher: str,
    slug: str,
    document: dict[str, object],
    revision_number: int = 1,
) -> CatalogDocumentRevision:
    root = CatalogDocument(
        id=root_id,
        kind=kind,
        publisher=publisher,
        slug=slug,
        title=slug,
        created_by="test",
        created_at=NOW,
        updated_at=NOW,
    )
    revision = CatalogDocumentRevision(
        id=revision_id,
        document_id=root_id,
        kind=kind,
        publisher=publisher,
        slug=slug,
        revision_number=revision_number,
        schema_version=2,
        state="active",
        document=document,
        content_digest=_digest(document),
        artifact_key=("a" * 64 if kind == "model" else None),
        projected={},
        created_by="test",
        created_at=NOW,
    )
    if revision_number == 1:
        session.add(root)
    session.add(revision)
    return revision


def test_canonical_recipe_resolution_uses_selected_file_identity_and_provenance(
    tmp_path: Path,
) -> None:
    sessions = _sessions()
    file_digest = hashlib.sha256(b"model bytes!").hexdigest()
    model_document = _model_document(
        path="weights/model.safetensors", file_digest=file_digest, roles=["weights"]
    )
    model_digest = _digest(model_document)
    recipe_document = _recipe_document(model_digest)
    with sessions.begin() as session:
        _add_active(
            session,
            root_id="00000000-0000-4000-8000-000000000001",
            revision_id="00000000-0000-4000-8000-000000000002",
            kind="model",
            publisher="owner",
            slug="model",
            document=model_document,
        )
        recipe = _add_active(
            session,
            root_id="00000000-0000-4000-8000-000000000003",
            revision_id="00000000-0000-4000-8000-000000000004",
            kind="recipe",
            publisher="owner",
            slug="recipe",
            document=recipe_document,
        )

    service = ModelCacheService(sessions, tmp_path / "cache", reserve_bytes=0)
    first = service.resolve_artifact_set(recipe_revision_id=recipe.id)
    assert first.model_version_sha256 == model_digest
    assert first.recipe_revision_sha256 == _digest(recipe_document)
    assert first.artifacts[0].path == "weights/model.safetensors"
    assert first.artifacts[0].expected_bytes == 12

    editorial = {**recipe_document, "editorial_note": "release note"}
    with sessions.begin() as session:
        edited = _add_active(
            session,
            root_id="00000000-0000-4000-8000-000000000003",
            revision_id="00000000-0000-4000-8000-000000000005",
            kind="recipe",
            publisher="owner",
            slug="recipe",
            document=editorial,
            revision_number=2,
        )
    second = service.resolve_artifact_set(recipe_revision_id=edited.id)
    assert second.recipe_revision_sha256 != first.recipe_revision_sha256
    assert second.digest == first.digest

    changed_document = _model_document(
        path="weights/other.safetensors", file_digest=file_digest, roles=["weights"]
    )
    changed_digest = _digest(changed_document)
    changed_recipe = _recipe_document(changed_digest)
    with sessions.begin() as session:
        _add_active(
            session,
            root_id="00000000-0000-4000-8000-000000000006",
            revision_id="00000000-0000-4000-8000-000000000007",
            kind="model",
            publisher="owner",
            slug="changed-model",
            document=changed_document,
        )
        changed_recipe_revision = _add_active(
            session,
            root_id="00000000-0000-4000-8000-000000000008",
            revision_id="00000000-0000-4000-8000-000000000009",
            kind="recipe",
            publisher="owner",
            slug="changed-recipe",
            document=changed_recipe,
        )
    changed = service.resolve_artifact_set(
        recipe_revision_id=changed_recipe_revision.id
    )
    assert changed.digest != first.digest


def test_canonical_build_identity_excludes_editorial_runtime_selectors_and_notes() -> (
    None
):
    build = {
        "base_image": {
            "repository": "runtime/base",
            "digest": "b" * 64,
            "platform": "linux/arm64",
        },
        "context": {"path": "source"},
        "dockerfile": "Dockerfile",
        "patches": [],
        "target": "runtime",
        "arguments": [{"name": "flavor", "value": "release"}],
        "network": {"mode": "none", "hosts": []},
    }
    settings = {
        "kind": "generation",
        "context_tokens": {"value": 4096, "change_effect": "restart"},
        "concurrency": {"value": 1, "change_effect": "restart"},
        "knobs": {"compiler": {"value": "clang", "change_effect": "rebuild"}},
    }
    artifacts = [
        {
            "path": "weights/model.safetensors",
            "sha256": "c" * 64,
            "size_bytes": 12,
            "roles": ["weights"],
            "mount": {"target": "/models", "read_only": True},
        }
    ]
    first = derive_build_input_identity(
        build,
        source_bundle_sha256="d" * 64,
        builder_binary_digest="e" * 64,
        effective_settings=settings,
        model_artifacts=artifacts,
    )
    editorial = {**build, "notes": "release note"}
    runtime_only = [
        {
            **artifacts[0],
            "roles": ["auxiliary"],
            "mount": {"target": "/other", "read_only": True},
        }
    ]
    assert (
        derive_build_input_identity(
            editorial,
            source_bundle_sha256="d" * 64,
            builder_binary_digest="e" * 64,
            effective_settings=settings,
            model_artifacts=runtime_only,
        )
        == first
    )
    changed_settings = {
        **settings,
        "knobs": {"compiler": {"value": "gcc", "change_effect": "rebuild"}},
    }
    assert (
        derive_build_input_identity(
            build,
            source_bundle_sha256="d" * 64,
            builder_binary_digest="e" * 64,
            effective_settings=changed_settings,
            model_artifacts=artifacts,
        )
        != first
    )
    changed_file = [{**artifacts[0], "path": "weights/other.safetensors"}]
    assert (
        derive_build_input_identity(
            build,
            source_bundle_sha256="d" * 64,
            builder_binary_digest="e" * 64,
            effective_settings=settings,
            model_artifacts=changed_file,
        )
        != first
    )


def test_prebuilt_recipe_does_not_enter_source_build_path() -> None:
    image_recipe = {
        "execution": {
            "mode": "image",
            "image": {
                "repository": "runtime/image",
                "digest": "a" * 64,
                "platform": "linux/arm64",
            },
        }
    }
    try:
        _canonical_build(image_recipe)
    except RecipeBuildError as error:
        assert error.code == "build.not_required"
    else:
        raise AssertionError("prebuilt recipe unexpectedly selected source build")
