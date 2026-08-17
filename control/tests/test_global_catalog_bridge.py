from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from test_catalog_service import _seed_recipe_dependencies
from vonk_control.auth import TokenCodec
from vonk_control.catalog_service import CatalogService
from vonk_control.global_catalog import (
    GlobalCatalogClient,
    GlobalCatalogError,
    GlobalRecipeRevision,
)
from vonk_control.models import (
    Base,
    LocalRecipe,
    LocalRecipeRevision,
    RecipeGlobalLink,
    RecipeTestReport,
)
from vonk_control.recipe_contract import recipe_content_sha256
from vonk_control.source_bundles import generate_source_bundle

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures/global/recipe-v1-minimal.json"


@pytest.fixture
def recipe() -> dict[str, object]:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["identity"] = {"publisher": "vonk", "slug": "qwen3-vllm"}
    return document


@pytest.fixture
def catalog(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    return (
        CatalogService(
            sessions,
            clock=lambda: NOW,
            cursors=TokenCodec(b"c" * 32).cursor_codec(),
        ),
        sessions,
    )


def _uri(document: dict[str, object]) -> str:
    identity = document["identity"]
    assert isinstance(identity, dict)
    return (
        f"vonk://catalog/{identity['publisher']}/{identity['slug']}"
        f"@sha256:{recipe_content_sha256(document)}"
    )


def test_client_fetches_only_the_exact_immutable_revision(recipe) -> None:
    digest = recipe_content_sha256(recipe)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200,
            headers={"ETag": f'"sha256:{digest}"'},
            json={
                "publisher": "vonk",
                "slug": "qwen3-vllm",
                "recipe_id": "00000000-0000-4000-8000-000000000004",
                "revision_number": 4,
                "revision_id": "10000000-0000-4000-8000-000000000004",
                "content_sha256": digest,
                "schema_version": 1,
                "published_at": "2026-08-07T10:00:00+00:00",
                "document": recipe,
            },
        )

    client = GlobalCatalogClient(
        "https://vonkforge.ai",
        transport=httpx.MockTransport(handler),
    )
    revision = client.fetch(_uri(recipe))

    assert revision.content_sha256 == digest
    assert revision.revision_number == 4
    assert seen == [f"/v1/recipes/vonk/qwen3-vllm/revisions/sha256/{digest}"]


def test_client_downloads_a_bounded_exact_source_bundle() -> None:
    bundle = generate_source_bundle({"Dockerfile": b"FROM scratch\nUSER 65532:65532\n"})

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/source-bundles/{bundle.sha256}"
        return httpx.Response(
            200,
            headers={"content-type": "application/vnd.vonk-forge.source-bundle.v1+tar"},
            content=bundle.archive,
        )

    client = GlobalCatalogClient(
        "https://vonkforge.ai", transport=httpx.MockTransport(handler)
    )

    assert client.fetch_source_bundle(bundle.sha256) == bundle.archive


@pytest.mark.parametrize(
    ("base_url", "revision_hash", "status", "code"),
    [
        ("http://catalog.example", "a" * 64, 200, "global.url_insecure"),
        ("https://vonkforge.ai", "a" * 64, 200, "global.revision_changed"),
        ("https://vonkforge.ai", "a" * 64, 302, "global.redirect_forbidden"),
    ],
)
def test_client_fails_closed_on_insecure_changed_or_redirected_content(
    recipe, base_url, revision_hash, status, code
) -> None:
    expected = recipe_content_sha256(recipe)

    def handler(request: httpx.Request) -> httpx.Response:
        if status == 302:
            return httpx.Response(302, headers={"Location": "https://attacker.invalid"})
        return httpx.Response(
            200,
            json={
                "publisher": "vonk",
                "slug": "qwen3-vllm",
                "revision_number": 1,
                "recipe_id": "00000000-0000-4000-8000-000000000001",
                "revision_id": "10000000-0000-4000-8000-000000000001",
                "content_sha256": revision_hash,
                "schema_version": 1,
                "published_at": "2026-08-07T10:00:00+00:00",
                "document": recipe,
            },
        )

    with pytest.raises(GlobalCatalogError) as failure:
        GlobalCatalogClient(base_url, transport=httpx.MockTransport(handler)).fetch(
            f"vonk://catalog/vonk/qwen3-vllm@sha256:{expected}"
        )
    assert failure.value.code == code


def test_global_import_is_idempotent_and_remains_local_after_remote_disappears(
    catalog, recipe
) -> None:
    service, sessions = catalog
    _seed_recipe_dependencies(service, recipe)
    digest = recipe_content_sha256(recipe)
    remote = GlobalRecipeRevision(
        publisher="vonk",
        slug="qwen3-vllm",
        recipe_id="00000000-0000-4000-8000-000000000004",
        revision_number=4,
        revision_id="10000000-0000-4000-8000-000000000004",
        content_sha256=digest,
        published_at="2026-08-07T10:00:00+00:00",
        document=recipe,
    )

    first = service.import_global("admin", remote)
    replay = service.import_global("admin", remote)

    assert replay.id == first.id
    assert first.lifecycle == "resolved"
    assert first.source_kind == "global"
    assert first.content_sha256 == digest
    assert service.get_recipe(first.recipe_id).document == recipe
    with sessions() as database:
        link = database.get(RecipeGlobalLink, first.recipe_id)
        assert link is not None
        assert link.global_revision == 4
        assert database.scalar(
            select(LocalRecipe).where(LocalRecipe.id == first.recipe_id)
        )


def test_new_remote_revision_becomes_a_new_immutable_local_revision(
    catalog, recipe
) -> None:
    service, sessions = catalog
    _seed_recipe_dependencies(service, recipe)
    first_document = copy.deepcopy(recipe)
    first = GlobalRecipeRevision(
        "vonk",
        "qwen3-vllm",
        "00000000-0000-4000-8000-000000000004",
        1,
        "10000000-0000-4000-8000-000000000001",
        recipe_content_sha256(first_document),
        "2026-08-07T10:00:00+00:00",
        first_document,
    )
    service.import_global("admin", first)
    second_document = copy.deepcopy(recipe)
    second_document["metadata"]["title"] = "Qwen3 revised"
    second = GlobalRecipeRevision(
        "vonk",
        "qwen3-vllm",
        "00000000-0000-4000-8000-000000000004",
        2,
        "10000000-0000-4000-8000-000000000002",
        recipe_content_sha256(second_document),
        "2026-08-07T11:00:00+00:00",
        second_document,
    )

    imported = service.import_global("admin", second)

    assert imported.revision_number == 2
    assert imported.content_sha256 == second.content_sha256
    with sessions() as database:
        assert len(list(database.scalars(select(LocalRecipeRevision)))) == 2


def _test_report(recipe_hash: str, image_digest: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "recipe_sha256": recipe_hash,
        "source_bundle_sha256": "c" * 64,
        "build_input_sha256": "b" * 64,
        "image_digest": image_digest,
        "topology_name": "solo",
        "node_count": 1,
        "runtime": {
            "agent_version": "1.0.0",
            "container_runtime": "podman",
            "architecture": "linux/arm64",
        },
        "checks": [
            {"name": "container.started", "passed": True},
            {"name": "endpoint.healthy", "passed": True},
            {"name": "inference.completed", "passed": True},
        ],
        "started_at": "2026-08-07T10:00:00+00:00",
        "finished_at": "2026-08-07T10:05:00+00:00",
    }


def test_publication_export_requires_bound_evidence_and_target_namespace(
    catalog, recipe
) -> None:
    service, sessions = catalog
    _seed_recipe_dependencies(service, recipe)
    draft = service.create_recipe(
        "admin",
        type(
            "Draft",
            (),
            {"slug": "qwen3-vllm", "document": recipe, "source_kind": "local"},
        )(),
    )
    resolved = service.resolve(draft.recipe_id, draft.revision_number, "admin")
    image_digest = "sha256:" + "c" * 64

    with pytest.raises(Exception, match="test report"):
        service.publication_export(resolved.recipe_id, "ada-lab")

    service.attach_test_report(
        resolved.recipe_id,
        _test_report(resolved.content_sha256 or "", image_digest),
        "admin",
    )
    envelope = service.publication_export(resolved.recipe_id, "ada-lab")

    exported = envelope["recipe"]
    assert exported["identity"]["publisher"] == "ada-lab"
    exported_hash = recipe_content_sha256(exported)
    assert envelope["test_report"]["recipe_sha256"] == exported_hash
    assert envelope["test_report"]["image_digest"] == image_digest
    with sessions() as database:
        assert database.scalar(select(RecipeTestReport)) is not None


def test_test_report_rejects_failed_or_mismatched_claims(catalog, recipe) -> None:
    service, _sessions = catalog
    _seed_recipe_dependencies(service, recipe)
    draft = service.create_recipe(
        "admin",
        type(
            "Draft",
            (),
            {"slug": "qwen3-vllm", "document": recipe, "source_kind": "local"},
        )(),
    )
    resolved = service.resolve(draft.recipe_id, 1, "admin")
    report = _test_report(resolved.content_sha256 or "", "sha256:" + "c" * 64)
    report["checks"][2]["passed"] = False

    with pytest.raises(Exception, match="required lifecycle"):
        service.attach_test_report(resolved.recipe_id, report, "admin")

    report["checks"][2]["passed"] = True
    report["finished_at"] = "2026-08-07T09:00:00+00:00"
    with pytest.raises(Exception, match="timestamps"):
        service.attach_test_report(resolved.recipe_id, report, "admin")
