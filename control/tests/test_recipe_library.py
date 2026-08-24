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


def _contents_document_payload(document: dict[str, object]) -> dict[str, object]:
    content = json.dumps(document).encode()
    blob_sha = hashlib.sha1(
        f"blob {len(content)}\0".encode() + content,
        usedforsecurity=False,
    ).hexdigest()
    return {
        "type": "file",
        "sha": blob_sha,
        "size": len(content),
        "encoding": "base64",
        "content": base64.b64encode(content).decode(),
    }


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
            return httpx.Response(200, json=_contents_document_payload(index))
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


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("wrong-type", "recipe_library.response_invalid"),
        ("missing-sha", "recipe_library.response_invalid"),
        ("wrong-size", "recipe_library.digest_mismatch"),
        ("tampered-content", "recipe_library.digest_mismatch"),
    ],
)
def test_recipe_library_rejects_invalid_inline_index_identity(
    mutation: str,
    expected_code: str,
) -> None:
    index = {
        "schema_version": 1,
        "repository": "CarstVaartjes/vonk-forge-recipes",
        "recipes": [],
    }
    payload = _contents_document_payload(index)
    if mutation == "wrong-type":
        payload["type"] = "dir"
    elif mutation == "missing-sha":
        del payload["sha"]
    elif mutation == "wrong-size":
        payload["size"] = int(payload["size"]) + 1
    else:
        content = bytearray(base64.b64decode(str(payload["content"])))
        content[-2] ^= 1
        payload["content"] = base64.b64encode(content).decode()
    commit = "f" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": commit})
        return httpx.Response(200, json=payload)

    with pytest.raises(RecipeLibraryError) as exc_info:
        RecipeLibraryClient(transport=httpx.MockTransport(handler)).list()

    assert exc_info.value.code == expected_code


def test_recipe_library_fetches_large_index_by_immutable_git_blob() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    index = {
        "schema_version": 1,
        "repository": "CarstVaartjes/vonk-forge-recipes",
        "padding": "x" * (1024 * 1024),
        "recipes": [
            {
                "source_path": "recipes/synthetic-tiny-openai.json",
                "content_sha256": recipe_content_sha256(document),
                "document": document,
            }
        ],
    }
    index_bytes = json.dumps(index).encode()
    blob_sha = hashlib.sha1(
        f"blob {len(index_bytes)}\0".encode() + index_bytes,
        usedforsecurity=False,
    ).hexdigest()
    commit = "f" * 40
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": commit})
        if request.url.path.endswith("/contents/catalog-index.json"):
            assert request.headers["accept"] == "application/vnd.github.object+json"
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "encoding": "none",
                    "content": "",
                    "sha": blob_sha,
                    "size": len(index_bytes),
                },
            )
        if request.url.path.endswith(f"/git/blobs/{blob_sha}"):
            return httpx.Response(
                200,
                json={
                    "sha": blob_sha,
                    "size": len(index_bytes),
                    "encoding": "base64",
                    "content": base64.b64encode(index_bytes).decode(),
                },
            )
        raise AssertionError(request.url)

    snapshot = RecipeLibraryClient(transport=httpx.MockTransport(handler)).list()

    assert [item.slug for item in snapshot.items] == ["synthetic-tiny-openai"]
    assert calls == [
        "/repos/CarstVaartjes/vonk-forge-recipes/commits/main",
        "/repos/CarstVaartjes/vonk-forge-recipes/contents/catalog-index.json",
        f"/repos/CarstVaartjes/vonk-forge-recipes/git/blobs/{blob_sha}",
    ]


@pytest.mark.parametrize("contents_encoding", ["", None, "utf-8"])
def test_recipe_library_rejects_undocumented_empty_index_encoding(
    contents_encoding: str | None,
) -> None:
    commit = "f" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": commit})
        return httpx.Response(
            200,
            json={
                "type": "file",
                "encoding": contents_encoding,
                "content": "",
                "sha": "a" * 40,
                "size": 1024 * 1024 + 1,
            },
        )

    with pytest.raises(RecipeLibraryError) as exc_info:
        RecipeLibraryClient(transport=httpx.MockTransport(handler)).list()

    assert exc_info.value.code == "recipe_library.response_invalid"


@pytest.mark.parametrize(
    ("blob_sha_override", "blob_size_offset", "blob_content_suffix"),
    [
        ("0" * 40, 0, b""),
        (None, 1, b""),
        (None, 0, b"changed"),
    ],
)
def test_recipe_library_rejects_large_index_blob_identity_mismatch(
    blob_sha_override: str | None,
    blob_size_offset: int,
    blob_content_suffix: bytes,
) -> None:
    index = {
        "schema_version": 1,
        "repository": "CarstVaartjes/vonk-forge-recipes",
        "recipes": [],
    }
    index_bytes = json.dumps(index).encode()
    blob_sha = hashlib.sha1(
        f"blob {len(index_bytes)}\0".encode() + index_bytes,
        usedforsecurity=False,
    ).hexdigest()
    commit = "f" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": commit})
        if request.url.path.endswith("/contents/catalog-index.json"):
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "encoding": "none",
                    "content": "",
                    "sha": blob_sha,
                    "size": len(index_bytes),
                },
            )
        if request.url.path.endswith(f"/git/blobs/{blob_sha}"):
            blob_content = index_bytes + blob_content_suffix
            return httpx.Response(
                200,
                json={
                    "sha": blob_sha_override or blob_sha,
                    "size": len(index_bytes) + blob_size_offset,
                    "encoding": "base64",
                    "content": base64.b64encode(blob_content).decode(),
                },
            )
        raise AssertionError(request.url)

    with pytest.raises(RecipeLibraryError) as exc_info:
        RecipeLibraryClient(transport=httpx.MockTransport(handler)).list()

    assert exc_info.value.code == "recipe_library.digest_mismatch"


def test_recipe_library_rejects_oversize_index_before_fetching_blob() -> None:
    commit = "f" * 40
    blob_sha = "a" * 40
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": commit})
        if request.url.path.endswith("/contents/catalog-index.json"):
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "encoding": "none",
                    "content": "",
                    "sha": blob_sha,
                    "size": 12 * 1024 * 1024 + 1,
                },
            )
        raise AssertionError(request.url)

    with pytest.raises(RecipeLibraryError) as exc_info:
        RecipeLibraryClient(transport=httpx.MockTransport(handler)).list()

    assert exc_info.value.code == "recipe_library.response_too_large"
    assert all("/git/blobs/" not in path for path in calls)


def test_recipe_library_parses_bounded_release_history_without_changing_recipe_digest() -> (
    None
):
    current = "a" * 64
    previous = "b" * 64
    entry = {
        "release": {
            "version": "2.0.0",
            "released_at": "2026-08-23",
            "history": [
                {
                    "version": "2.0.0",
                    "released_at": "2026-08-23",
                    "recipe_content_sha256": current,
                    "upgrade_effect": "rebuild",
                    "changes": [
                        {
                            "kind": "performance",
                            "summary": "Adopt current validated graph defaults.",
                            "details": "The executable recipe remains digest-bound.",
                            "references": ["https://github.com/MiaAI-Lab/example"],
                        }
                    ],
                },
                {
                    "version": "1.0.0",
                    "released_at": "2026-08-14",
                    "recipe_content_sha256": previous,
                    "upgrade_effect": "reinstall",
                    "changes": [{"kind": "initial", "summary": "Legacy baseline."}],
                },
            ],
        }
    }

    releases = RecipeLibraryClient._release_history(entry, current)

    assert [release.version for release in releases] == ["2.0.0", "1.0.0"]
    assert releases[0].content_sha256 == current
    assert releases[0].changes[0].references == (
        "https://github.com/MiaAI-Lab/example",
    )
    assert RecipeLibraryClient._release_history({}, current) == ()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["release"].update(version="3.0.0"),
        lambda value: value["release"]["history"].reverse(),
        lambda value: value["release"]["history"][0].update(
            recipe_content_sha256="c" * 64
        ),
    ],
)
def test_recipe_library_rejects_inconsistent_release_history(mutate) -> None:
    current = "a" * 64
    entry = {
        "release": {
            "version": "2.0.0",
            "released_at": "2026-08-23",
            "history": [
                {
                    "version": "2.0.0",
                    "released_at": "2026-08-23",
                    "recipe_content_sha256": current,
                    "upgrade_effect": "rebuild",
                    "changes": [{"kind": "fix", "summary": "Current release."}],
                },
                {
                    "version": "1.0.0",
                    "released_at": "2026-08-14",
                    "recipe_content_sha256": "b" * 64,
                    "upgrade_effect": "reinstall",
                    "changes": [{"kind": "initial", "summary": "Baseline."}],
                },
            ],
        }
    }
    mutate(entry)

    with pytest.raises(RecipeLibraryError):
        RecipeLibraryClient._release_history(entry, current)


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
        return httpx.Response(200, json=_contents_document_payload(index))

    with pytest.raises(RecipeLibraryError, match="digest"):
        RecipeLibraryClient(transport=httpx.MockTransport(handler)).list()


def test_recipe_library_v2_index_resolves_dependencies_and_exact_source_bundle() -> (
    None
):
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
            return httpx.Response(200, json=_contents_document_payload(index))
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
                json={
                    "tree": [
                        {"path": "recipes/synthetic-tiny-openai.json", "type": "blob"}
                    ]
                },
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


def test_recipe_library_allows_only_the_fixed_internal_http_relay() -> None:
    observed: list[tuple[str, int | None, str]] = []

    def relay(request: httpx.Request) -> httpx.Response:
        observed.append((request.url.host, request.url.port, request.url.path))
        return httpx.Response(503)

    client = RecipeLibraryClient(
        base_url="http://caddy:8083/",
        transport=httpx.MockTransport(relay),
    )
    with pytest.raises(RecipeLibraryError) as relay_error:
        client.list()
    client.close()

    assert relay_error.value.code == "recipe_library.unavailable"
    assert observed == [
        ("caddy", 8083, "/repos/CarstVaartjes/vonk-forge-recipes/commits/main")
    ]

    with pytest.raises(RecipeLibraryError) as exc_info:
        RecipeLibraryClient(base_url="http://api.github.com")

    assert exc_info.value.code == "recipe_library.url_insecure"
