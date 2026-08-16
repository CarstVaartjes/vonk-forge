from __future__ import annotations

import copy
import inspect
import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from test_catalog_entities import (
    execution_harness,
    model,
    model_group,
    model_version,
    patch_bundle,
    runtime_distribution,
)
from vonk_control.auth import TokenCodec
from vonk_control.catalog_service import (
    CatalogConflict,
    CatalogService,
    CatalogValidationError,
    RecipeDraftInput,
)
from vonk_control.global_catalog import GlobalRecipeRevision
from vonk_control.models import Base, RecipeImport, RecipeSourceBundle
from vonk_control.recipe_contract import recipe_content_sha256
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
        cursors=TokenCodec(b"c" * 32).cursor_codec(),
        source_bundles=SourceBundleStore(tmp_path / "source-bundles"),
    )


def _resolve_entity(service: CatalogService, document: dict[str, object]):
    draft = service.entities.create_draft(document, actor="admin")
    return service.entities.resolve(draft.id, actor="admin")


def _seed_recipe_dependencies(
    service: CatalogService, recipe_document: dict[str, object]
) -> dict[str, object]:
    group = _resolve_entity(service, model_group())
    model_revision = _resolve_entity(service, model(group.content_sha256))
    version = _resolve_entity(service, model_version(model_revision.content_sha256))
    harness = _resolve_entity(service, execution_harness())
    distribution = _resolve_entity(
        service, runtime_distribution(harness.content_sha256)
    )
    recipe_document["model"] = {
        "kind": "model-version",
        "publisher": "vonk-forge",
        "slug": "synthetic-tiny-fp16",
        "content_sha256": version.content_sha256,
    }
    recipe_document["execution"]["harness"] = {
        "kind": "execution-harness",
        "publisher": "vonk-forge",
        "slug": "vllm-openai",
        "content_sha256": harness.content_sha256,
    }
    recipe_document["runtime"]["distribution"] = {
        "kind": "runtime-distribution",
        "publisher": "vonk-forge",
        "slug": "python-312-cuda",
        "content_sha256": distribution.content_sha256,
    }
    return recipe_document


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
    _seed_recipe_dependencies(service, recipe_document)
    draft = service.create_recipe(
        "admin",
        RecipeDraftInput(slug="synthetic-tiny-openai", document=recipe_document),
    )

    resolved = service.resolve(draft.recipe_id, draft.revision_number, "admin")
    repeated = service.resolve(draft.recipe_id, draft.revision_number, "admin")

    assert resolved.lifecycle == "resolved"
    assert repeated.id == resolved.id
    assert repeated.content_sha256 == resolved.content_sha256
    assert len(resolved.content_sha256 or "") == 64


def test_recipe_library_import_records_exact_commit_and_is_idempotent(
    service: CatalogService, recipe_document: dict[str, object]
) -> None:
    _seed_recipe_dependencies(service, recipe_document)
    digest = recipe_content_sha256(recipe_document)

    imported = service.import_recipe_library(
        "admin",
        library_commit="a" * 40,
        source_path="recipes/synthetic-tiny-openai.json",
        document=recipe_document,
        expected_content_sha256=digest,
    )
    repeated = service.import_recipe_library(
        "admin",
        library_commit="a" * 40,
        source_path="recipes/synthetic-tiny-openai.json",
        document=recipe_document,
        expected_content_sha256=digest,
    )

    assert imported.lifecycle == "resolved"
    assert imported.source_kind == "recipe_library"
    assert repeated.id == imported.id
    with service._sessions() as session:
        receipt = session.query(RecipeImport).one()
        assert receipt.source_kind == "recipe_library"
        assert receipt.source_reference.endswith(
            "@" + "a" * 40 + ":recipes/synthetic-tiny-openai.json"
        )
        assert receipt.redacted_source["commit"] == "a" * 40


def test_resolved_recipe_can_start_a_new_draft_without_mutating_the_resolution(
    service: CatalogService, recipe_document: dict[str, object]
) -> None:
    _seed_recipe_dependencies(service, recipe_document)
    initial = service.create_recipe(
        "admin",
        RecipeDraftInput(slug="synthetic-tiny-openai", document=recipe_document),
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
        "admin",
        RecipeDraftInput(slug="synthetic-tiny-openai", document=recipe_document),
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
            "admin",
            RecipeDraftInput(slug="synthetic-tiny-openai", document=document),
        )

    assert caught.value.code == "catalog.sensitive_field"
    assert "do-not-store" not in str(caught.value)


def test_fork_records_attribution_and_changes_identity(
    service: CatalogService, recipe_document: dict[str, object]
) -> None:
    _seed_recipe_dependencies(service, recipe_document)
    original = service.create_recipe(
        "admin",
        RecipeDraftInput(slug="synthetic-tiny-openai", document=recipe_document),
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
            "admin",
            RecipeDraftInput(slug="synthetic-tiny-openai", document=recipe_document),
        )

    assert caught.value.code == "catalog.mutable_artifact"


def test_short_opaque_artifact_revision_is_not_treated_as_immutable(
    service: CatalogService, recipe_document: dict[str, object]
) -> None:
    recipe_document["artifacts"][0]["revision"] = "deadbeefdeadbeef"

    with pytest.raises(CatalogValidationError):
        service.create_recipe(
            "admin",
            RecipeDraftInput(slug="synthetic-tiny-openai", document=recipe_document),
        )


def test_summary_uses_source_bundle_and_exact_topology(
    service: CatalogService, recipe_document: dict[str, object]
) -> None:
    service.create_recipe(
        "admin",
        RecipeDraftInput(slug="synthetic-tiny-openai", document=recipe_document),
    )

    summaries, _ = service.list_recipes()

    assert summaries[0].source_bundle_sha256 == "c" * 64
    assert summaries[0].execution_harness == "vllm-openai"
    assert summaries[0].runtime_distribution == "python-312-cuda"
    assert summaries[0].topology_name == "solo"
    assert summaries[0].topology_mode == "single"
    assert summaries[0].node_count == 1
    assert summaries[0].maximum_installed_bytes_per_node == 7_000_001_024
    assert summaries[0].maximum_runtime_memory_bytes_per_node == 88_000_000_000


def test_recipe_resolution_never_falls_back_to_latest(
    service: CatalogService, recipe_document: dict[str, object]
) -> None:
    _seed_recipe_dependencies(service, recipe_document)
    recipe_document["model"]["content_sha256"] = "f" * 64

    with pytest.raises(CatalogConflict, match="exact model-version"):
        service.resolve_recipe_revision(recipe_document, actor="admin")


def test_recipe_resolution_rejects_a_distribution_not_bound_to_the_harness(
    service: CatalogService, recipe_document: dict[str, object]
) -> None:
    _seed_recipe_dependencies(service, recipe_document)
    other_harness = _resolve_entity(service, execution_harness("other-harness"))
    other = _resolve_entity(
        service,
        runtime_distribution(
            other_harness.content_sha256,
            slug="other-distribution",
            harness_slug="other-harness",
        ),
    )
    recipe_document["runtime"]["distribution"] = {
        "kind": "runtime-distribution",
        "publisher": "vonk-forge",
        "slug": "other-distribution",
        "content_sha256": other.content_sha256,
    }

    with pytest.raises(CatalogConflict, match="does not implement the exact harness"):
        service.resolve_recipe_revision(recipe_document, actor="admin")


def test_recipe_resolution_rejects_patch_for_another_distribution(
    service: CatalogService, recipe_document: dict[str, object]
) -> None:
    _seed_recipe_dependencies(service, recipe_document)
    recipe_harness = recipe_document["execution"]["harness"]
    other = _resolve_entity(
        service,
        runtime_distribution(
            recipe_harness["content_sha256"], slug="other-distribution"
        ),
    )
    patch = _resolve_entity(
        service,
        patch_bundle("other-distribution", other.content_sha256),
    )
    recipe_document["execution"]["patch_bundle"] = {
        "kind": "patch-bundle",
        "publisher": "vonk-forge",
        "slug": "vllm-fix",
        "content_sha256": patch.content_sha256,
    }

    with pytest.raises(CatalogConflict, match="exact distribution"):
        service.resolve_recipe_revision(recipe_document, actor="admin")


def test_persisted_recipe_resolution_resolves_patch_from_the_document(
    service: CatalogService, recipe_document: dict[str, object]
) -> None:
    _seed_recipe_dependencies(service, recipe_document)
    distribution = recipe_document["runtime"]["distribution"]
    patch = _resolve_entity(
        service,
        patch_bundle(distribution["slug"], distribution["content_sha256"]),
    )
    recipe_document["execution"]["patch_bundle"] = {
        "kind": "patch-bundle",
        "publisher": "vonk-forge",
        "slug": "vllm-fix",
        "content_sha256": patch.content_sha256,
    }
    draft = service.create_recipe(
        "admin",
        RecipeDraftInput(slug="synthetic-tiny-openai", document=recipe_document),
    )

    resolved = service.resolve(draft.recipe_id, draft.revision_number, "admin")

    assert resolved.document["execution"]["patch_bundle"]["content_sha256"] == (
        patch.content_sha256
    )
    assert resolved.content_sha256 == recipe_content_sha256(resolved.document)


def test_recipe_resolution_has_no_out_of_band_patch_argument() -> None:
    assert (
        "patch_reference"
        not in inspect.signature(CatalogService.resolve_recipe_revision).parameters
    )


def test_recipe_resolution_returns_the_canonical_digest(
    service: CatalogService, recipe_document: dict[str, object]
) -> None:
    _seed_recipe_dependencies(service, recipe_document)

    digest = service.resolve_recipe_revision(recipe_document, actor="admin")

    assert digest == service.resolve_recipe_revision(recipe_document, actor="admin")
    assert len(digest) == 64


def test_global_import_rejects_an_unresolved_exact_dependency(
    service: CatalogService, recipe_document: dict[str, object]
) -> None:
    _seed_recipe_dependencies(service, recipe_document)
    recipe_document["model"]["content_sha256"] = "f" * 64
    digest = recipe_content_sha256(recipe_document)
    remote = GlobalRecipeRevision(
        publisher="vonk-forge",
        slug="synthetic-tiny-openai",
        recipe_id="00000000-0000-4000-8000-000000000001",
        revision_number=1,
        revision_id="10000000-0000-4000-8000-000000000001",
        content_sha256=digest,
        published_at="2026-08-15T10:00:00+00:00",
        document=recipe_document,
    )

    with pytest.raises(CatalogConflict, match="exact model-version"):
        service.import_global("admin", remote)


def test_global_import_resolves_patch_from_recipe_document(
    service: CatalogService, recipe_document: dict[str, object]
) -> None:
    _seed_recipe_dependencies(service, recipe_document)
    distribution = recipe_document["runtime"]["distribution"]
    patch = _resolve_entity(
        service,
        patch_bundle(distribution["slug"], distribution["content_sha256"]),
    )
    recipe_document["execution"]["patch_bundle"] = {
        "kind": "patch-bundle",
        "publisher": "vonk-forge",
        "slug": "vllm-fix",
        "content_sha256": patch.content_sha256,
    }
    digest = recipe_content_sha256(recipe_document)
    remote = GlobalRecipeRevision(
        publisher="vonk-forge",
        slug="synthetic-tiny-openai",
        recipe_id="00000000-0000-4000-8000-000000000001",
        revision_number=1,
        revision_id="10000000-0000-4000-8000-000000000001",
        content_sha256=digest,
        published_at="2026-08-15T10:00:00+00:00",
        document=recipe_document,
    )

    imported = service.import_global("admin", remote)

    assert imported.content_sha256 == digest
    assert (
        imported.document["execution"]["patch_bundle"]
        == (recipe_document["execution"]["patch_bundle"])
    )
