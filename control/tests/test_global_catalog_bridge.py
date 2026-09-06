from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from vonk_control.global_catalog import GlobalCatalogClient, GlobalCatalogError
from vonk_control.recipe_contract import recipe_content_sha256
from vonk_control.source_bundles import generate_source_bundle

FIXTURE = Path(__file__).parent / "fixtures/global/recipe-v1-minimal.json"


@pytest.fixture
def recipe() -> dict[str, object]:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["identity"] = {"publisher": "vonk", "slug": "qwen3-vllm"}
    return document




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
