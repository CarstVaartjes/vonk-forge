from __future__ import annotations

import copy
import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.catalog_service import (
    CatalogConflict,
    CatalogService,
    CatalogValidationError,
    RecipeDraftInput,
)
from vonk_control.models import Base, RecipeSourceBundle
from vonk_control.source_bundles import SourceBundleStore, generate_source_bundle


@pytest.fixture
def recipe_document() -> dict[str, object]:
    path = Path(__file__).parent / "fixtures/global/recipe-v1-minimal.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def service(tmp_path: Path) -> CatalogService:
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    return CatalogService(
        sessions,
        clock=lambda: datetime(2026, 8, 7, tzinfo=UTC),
        source_bundles=SourceBundleStore(tmp_path / "source-bundles"),
    )


def test_uploaded_source_bundle_is_verified_and_recorded(
    service: CatalogService,
) -> None:
    bundle = generate_source_bundle(
        {
            "Dockerfile": b"FROM scratch\nUSER 65532:65532\n",
            "compose.yaml": b"services: {}\n",
        }
    )

    stored = service.store_source_bundle(
        bundle.sha256, io.BytesIO(bundle.archive), "administrator"
    )

    assert stored.sha256 == bundle.sha256
    assert stored.file_count == 2
    with service._sessions() as session:
        row = session.get(RecipeSourceBundle, bundle.sha256)
        assert row is not None
        assert row.manifest["sha256"] == bundle.sha256


def test_resolve_creates_immutable_revision_and_repeated_resolve_is_idempotent(
    service: CatalogService, recipe_document: dict[str, object]
) -> None:
    draft = service.create_recipe(
        "admin", RecipeDraftInput(slug="qwen3-vllm", document=recipe_document)
    )

    resolved = service.resolve(draft.recipe_id, draft.revision_number, "admin")
    repeated = service.resolve(draft.recipe_id, draft.revision_number, "admin")

    assert resolved.lifecycle == "resolved"
    assert repeated.id == resolved.id
    assert repeated.content_sha256 == resolved.content_sha256
    assert len(resolved.content_sha256 or "") == 64


def test_resolved_recipe_can_start_a_new_draft_without_mutating_the_resolution(
    service: CatalogService, recipe_document: dict[str, object]
) -> None:
    initial = service.create_recipe(
        "admin", RecipeDraftInput(slug="qwen3-vllm", document=recipe_document)
    )
    resolved = service.resolve(initial.recipe_id, initial.revision_number, "admin")
    changed = copy.deepcopy(recipe_document)
    changed["metadata"]["title"] = "Changed title"

    draft = service.update_draft(
        resolved.recipe_id, resolved.revision_number, changed, "admin"
    )

    assert draft.lifecycle == "draft"
    assert draft.revision_number == resolved.revision_number + 1
    assert draft.document["metadata"]["title"] == "Changed title"
    with service._sessions() as session:
        preserved = service._repository.revision(
            session, resolved.recipe_id, resolved.revision_number
        )
    assert preserved is not None
    assert preserved.lifecycle == "resolved"
    assert preserved.document == recipe_document
    assert preserved.content_sha256 == resolved.content_sha256


def test_stale_draft_update_has_stable_conflict_code(
    service: CatalogService, recipe_document: dict[str, object]
) -> None:
    draft = service.create_recipe(
        "admin", RecipeDraftInput(slug="qwen3-vllm", document=recipe_document)
    )
    changed = copy.deepcopy(recipe_document)
    changed["metadata"]["title"] = "Changed title"
    service.update_draft(draft.recipe_id, draft.revision_number, changed, "admin")

    with pytest.raises(CatalogConflict) as caught:
        service.update_draft(draft.recipe_id, draft.revision_number, changed, "admin")

    assert caught.value.code == "catalog.stale_revision"


@pytest.mark.parametrize(
    "sensitive_key",
    [
        "authorization",
        "credential",
        "password",
        "secret",
        "token",
        "private_key",
        "certificate",
    ],
)
def test_sensitive_keys_are_rejected_at_any_depth(
    service: CatalogService,
    recipe_document: dict[str, object],
    sensitive_key: str,
) -> None:
    document = copy.deepcopy(recipe_document)
    document["metadata"][sensitive_key] = "do-not-store"

    with pytest.raises(CatalogValidationError) as caught:
        service.create_recipe(
            "admin", RecipeDraftInput(slug="qwen3-vllm", document=document)
        )

    assert caught.value.code == "catalog.sensitive_field"
    assert "do-not-store" not in str(caught.value)


def test_fork_records_attribution_and_changes_identity(
    service: CatalogService, recipe_document: dict[str, object]
) -> None:
    original = service.create_recipe(
        "admin", RecipeDraftInput(slug="qwen3-vllm", document=recipe_document)
    )
    resolved = service.resolve(original.recipe_id, original.revision_number, "admin")

    forked = service.fork(
        original.recipe_id, resolved.revision_number, "my-qwen", "alice"
    )

    assert forked.document["identity"]["slug"] == "my-qwen"
    assert forked.document["provenance"]["source_kind"] == "fork"
    assert resolved.content_sha256 in forked.document["provenance"]["attribution"][0]


def test_mutable_external_revision_is_rejected(
    service: CatalogService, recipe_document: dict[str, object]
) -> None:
    recipe_document["artifacts"][0]["revision"] = "main-latest"

    with pytest.raises(CatalogValidationError) as caught:
        service.create_recipe(
            "admin", RecipeDraftInput(slug="qwen3-vllm", document=recipe_document)
        )

    assert caught.value.code == "catalog.mutable_artifact"


def test_short_opaque_artifact_revision_is_not_treated_as_immutable(
    service: CatalogService, recipe_document: dict[str, object]
) -> None:
    recipe_document["artifacts"][0]["revision"] = "deadbeefdeadbeef"

    with pytest.raises(CatalogValidationError):
        service.create_recipe(
            "admin", RecipeDraftInput(slug="qwen3-vllm", document=recipe_document)
        )


def test_summary_uses_source_bundle_and_exact_profile_counts(
    service: CatalogService, recipe_document: dict[str, object]
) -> None:
    service.create_recipe(
        "admin", RecipeDraftInput(slug="qwen3-vllm", document=recipe_document)
    )

    summaries, _ = service.list_recipes()

    assert summaries[0].source_bundle_sha256 == "a" * 64
    assert summaries[0].profile_node_counts == (1,)
    assert summaries[0].maximum_installed_bytes_per_node == 68_000_000_000
    assert summaries[0].maximum_runtime_memory_bytes_per_node == 88_000_000_000
