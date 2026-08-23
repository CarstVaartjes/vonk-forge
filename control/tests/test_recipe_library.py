from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from vonk_control.catalog_contract import catalog_content_sha256
from vonk_control.recipe_contract import recipe_content_sha256
from vonk_control.recipe_library import RecipeLibraryClient, RecipeLibraryError
from vonk_control.source_bundles import generate_source_bundle

FIXTURE = Path(__file__).parent / "fixtures/global/recipe-v1-minimal.json"
DEVELOPMENT_FIXTURE = Path(__file__).parent / "fixtures/recipes/dev-http-smoke"


def test_recipe_library_lists_current_recipe_documents_and_exact_uris() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    digest = recipe_content_sha256(document)
    commit = "a" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": commit})
        if request.url.path.endswith(f"/git/trees/{commit}"):
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {"path": "recipes/synthetic-tiny-openai.json", "type": "blob"},
                        {"path": "README.md", "type": "blob"},
                    ]
                },
            )
        if request.url.path.endswith("/contents/catalog-index.json"):
            return httpx.Response(404)
        if request.url.path.endswith("/contents/recipes/synthetic-tiny-openai.json"):
            encoded = base64.b64encode(json.dumps(document).encode()).decode()
            return httpx.Response(
                200,
                json={"encoding": "base64", "content": encoded},
            )
        raise AssertionError(request.url)

    recipes = RecipeLibraryClient(transport=httpx.MockTransport(handler)).list()

    assert recipes.commit == commit
    assert [item.slug for item in recipes.items] == ["synthetic-tiny-openai"]
    assert recipes.items[0].title == "Synthetic Tiny OpenAI"
    assert recipes.items[0].content_sha256 == digest
    assert recipes.items[0].uri.endswith(f"@sha256:{digest}")
    assert recipes.items[0].document == document


def test_recipe_library_uses_one_digest_bound_index_and_caches_it() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    digest = recipe_content_sha256(document)
    commit = "c" * 40
    calls: list[str] = []
    index = {
        "schema_version": 1,
        "repository": "CarstVaartjes/vonk-forge-recipes",
        "recipes": [
            {
                "source_path": "recipes/synthetic-tiny-openai.json",
                "content_sha256": digest,
                "document": document,
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": commit})
        if request.url.path.endswith("/contents/catalog-index.json"):
            encoded = base64.b64encode(json.dumps(index).encode()).decode()
            return httpx.Response(200, json={"encoding": "base64", "content": encoded})
        raise AssertionError(request.url)

    client = RecipeLibraryClient(transport=httpx.MockTransport(handler))
    first = client.list()
    second = client.list()
    fetched = client.fetch(first.items[0].uri)

    assert first is second
    assert fetched.document == document
    assert calls == [
        "/repos/CarstVaartjes/vonk-forge-recipes/commits/main",
        "/repos/CarstVaartjes/vonk-forge-recipes/contents/catalog-index.json",
    ]


def test_recipe_library_rejects_an_index_with_a_changed_digest() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    commit = "d" * 40
    index = {
        "schema_version": 1,
        "repository": "CarstVaartjes/vonk-forge-recipes",
        "recipes": [
            {
                "source_path": "recipes/synthetic-tiny-openai.json",
                "content_sha256": "0" * 64,
                "document": document,
            }
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": commit})
        encoded = base64.b64encode(json.dumps(index).encode()).decode()
        return httpx.Response(200, json={"encoding": "base64", "content": encoded})

    with pytest.raises(RecipeLibraryError, match="digest"):
        RecipeLibraryClient(transport=httpx.MockTransport(handler)).list()


def test_recipe_library_v2_index_resolves_dependencies_and_exact_source_bundle() -> None:
    document = copy.deepcopy(
        json.loads((DEVELOPMENT_FIXTURE / "recipe.json").read_text(encoding="utf-8"))
    )
    document["identity"]["publisher"] = "vonk-forge"
    entity_directories = {
        "model-group": "model-groups",
        "model": "models",
        "model-version": "model-versions",
        "runtime-distribution": "runtime-distributions",
    }
    entity_entries = []
    for path in sorted((DEVELOPMENT_FIXTURE / "entities").glob("*.json")):
        entity = json.loads(path.read_text(encoding="utf-8"))
        directory = entity_directories.get(entity["kind"])
        if directory is None:
            continue
        slug = entity["identity"]["slug"]
        entity_entries.append(
            {
                "source_path": f"{directory}/{slug}.json",
                "content_sha256": catalog_content_sha256(entity),
                "document": entity,
            }
        )
    entity_entries.sort(key=lambda entry: entry["source_path"])
    source_files = {
        path.name: path.read_bytes()
        for path in sorted((DEVELOPMENT_FIXTURE / "context").iterdir())
    }
    bundle = generate_source_bundle(source_files)
    blobs = {
        hashlib.sha1(
            f"blob {len(content)}\0".encode() + content,
            usedforsecurity=False,
        ).hexdigest(): content
        for content in source_files.values()
    }
    index = {
        "schema_version": 2,
        "repository": "CarstVaartjes/vonk-forge-recipes",
        "catalog_entities": entity_entries,
        "source_contexts": [
            {
                "context_path": "adapters/dev-http-smoke",
                "content_sha256": bundle.sha256,
                "expected_bytes": len(bundle.archive),
                "files": sorted(
                    [
                        {
                            "path": path,
                            "blob_sha": blob_sha,
                            "size": len(content),
                        }
                        for blob_sha, content in blobs.items()
                        for path, candidate in source_files.items()
                        if candidate == content
                    ],
                    key=lambda entry: entry["path"],
                ),
            }
        ],
        "recipes": [
            {
                "source_path": "recipes/dev-http-smoke.json",
                "content_sha256": recipe_content_sha256(document),
                "document": document,
            }
        ],
    }
    document["build"]["context"]["path"] = "adapters/dev-http-smoke"
    index["recipes"][0]["content_sha256"] = recipe_content_sha256(document)
    commit = "e" * 40
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": commit})
        if request.url.path.endswith("/contents/catalog-index.json"):
            return httpx.Response(
                200,
                json={
                    "encoding": "base64",
                    "content": base64.b64encode(json.dumps(index).encode()).decode(),
                },
            )
        blob_sha = request.url.path.rsplit("/", 1)[-1]
        if blob_sha in blobs:
            return httpx.Response(
                200,
                json={
                    "encoding": "base64",
                    "content": base64.b64encode(blobs[blob_sha]).decode(),
                },
            )
        raise AssertionError(request.url)

    client = RecipeLibraryClient(transport=httpx.MockTransport(handler))
    listed = client.list().items[0]
    fetched = client.fetch(listed.uri)
    repeated = client.fetch(listed.uri)

    assert listed.source_bundle is None
    assert len(listed.dependencies) == 4
    assert fetched.source_bundle == bundle.archive
    assert repeated is fetched
    assert len(calls) == 2 + len(source_files)


def test_recipe_library_rejects_a_changed_document_digest() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    commit = "b" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": commit})
        if request.url.path.endswith(f"/git/trees/{commit}"):
            return httpx.Response(
                200,
                json={"tree": [{"path": "recipes/synthetic-tiny-openai.json", "type": "blob"}]},
            )
        if request.url.path.endswith("/contents/catalog-index.json"):
            return httpx.Response(404)
        encoded = base64.b64encode(json.dumps(document).encode()).decode()
        return httpx.Response(
            200,
            json={"encoding": "base64", "content": encoded},
        )

    with pytest.raises(RecipeLibraryError, match="digest"):
        RecipeLibraryClient(transport=httpx.MockTransport(handler)).fetch(
            "vonk://catalog/vonk-forge/synthetic-tiny-openai@sha256:" + "0" * 64
        )
