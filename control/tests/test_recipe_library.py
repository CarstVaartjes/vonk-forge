from __future__ import annotations

import base64
import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from vonk_control.catalog_contract import catalog_content_sha256
from vonk_control.recipe_contract import recipe_content_sha256
from vonk_control.recipe_library import (
    RecipeLibraryClient,
    RecipeLibraryError,
    RecipeLibrarySnapshot,
    RecipeLibrarySourceContext,
)
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


def _recipe_index(document: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "repository": "CarstVaartjes/vonk-forge-recipes",
        "recipes": [
            {
                "source_path": "recipes/synthetic-tiny-openai.json",
                "content_sha256": recipe_content_sha256(document),
                "document": document,
            }
        ],
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


def test_recipe_library_initial_transient_failure_remains_unavailable() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503)

    client = RecipeLibraryClient(transport=httpx.MockTransport(handler))

    with pytest.raises(RecipeLibraryError) as exc_info:
        client.list()

    assert exc_info.value.code == "recipe_library.unavailable"
    assert calls == 1


def test_recipe_library_uses_validated_stale_snapshot_during_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    commit = "a" * 40
    clock = [0.0]
    unavailable = False
    calls: list[str] = []
    monkeypatch.setattr("vonk_control.recipe_library.time.monotonic", lambda: clock[0])

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if unavailable:
            return httpx.Response(503)
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": commit})
        return httpx.Response(
            200, json=_contents_document_payload(_recipe_index(document))
        )

    client = RecipeLibraryClient(
        transport=httpx.MockTransport(handler),
        cache_ttl_seconds=5.0,
        stale_retry_interval_seconds=10.0,
    )
    current = client.list()
    unavailable = True
    clock[0] = 6.0

    stale = client.list()

    assert stale is current
    assert stale.commit == commit
    assert stale.items[0].library_commit == commit
    assert stale.items[0].content_sha256 == recipe_content_sha256(document)
    assert len(calls) == 3


def test_recipe_library_suppresses_retries_until_stale_retry_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    clock = [0.0]
    unavailable = False
    calls = 0
    monkeypatch.setattr("vonk_control.recipe_library.time.monotonic", lambda: clock[0])

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if unavailable:
            return httpx.Response(503)
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": "a" * 40})
        return httpx.Response(
            200, json=_contents_document_payload(_recipe_index(document))
        )

    client = RecipeLibraryClient(
        transport=httpx.MockTransport(handler),
        cache_ttl_seconds=5.0,
        stale_retry_interval_seconds=10.0,
    )
    current = client.list()
    unavailable = True
    clock[0] = 6.0
    assert client.list() is current
    assert calls == 3

    clock[0] = 15.999
    assert client.list() is current
    assert calls == 3

    clock[0] = 16.0
    assert client.list() is current
    assert calls == 4


def test_recipe_library_recovers_to_new_commit_and_invalidates_hydration_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    new_document = copy.deepcopy(old_document)
    new_document["metadata"]["title"] = "Synthetic Tiny OpenAI refreshed"
    clock = [0.0]
    state = "old"
    monkeypatch.setattr("vonk_control.recipe_library.time.monotonic", lambda: clock[0])

    def handler(request: httpx.Request) -> httpx.Response:
        if state == "unavailable":
            return httpx.Response(503)
        commit = "a" * 40 if state == "old" else "b" * 40
        document = old_document if state == "old" else new_document
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": commit})
        return httpx.Response(
            200, json=_contents_document_payload(_recipe_index(document))
        )

    client = RecipeLibraryClient(
        transport=httpx.MockTransport(handler),
        cache_ttl_seconds=5.0,
        stale_retry_interval_seconds=10.0,
    )
    old = client.list()
    client._hydrated_items[(old.commit, old.items[0].uri)] = old.items[0]
    client._hydrated_bundles["a" * 64] = b"old bundle"

    state = "unavailable"
    clock[0] = 6.0
    assert client.list() is old

    state = "new"
    clock[0] = 16.0
    recovered = client.list()

    assert recovered is not old
    assert recovered.commit == "b" * 40
    assert recovered.items[0].library_commit == "b" * 40
    assert recovered.items[0].content_sha256 == recipe_content_sha256(new_document)
    assert client._hydrated_items == {}
    assert client._hydrated_bundles == {}


@pytest.mark.parametrize(
    "headers",
    [
        {"X-RateLimit-Remaining": "0"},
        {"Retry-After": "60"},
    ],
)
def test_recipe_library_uses_stale_snapshot_for_header_proven_rate_limit_403(
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
) -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    clock = [0.0]
    rate_limited = False
    monkeypatch.setattr("vonk_control.recipe_library.time.monotonic", lambda: clock[0])

    def handler(request: httpx.Request) -> httpx.Response:
        if rate_limited:
            return httpx.Response(403, headers=headers)
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": "a" * 40})
        return httpx.Response(
            200, json=_contents_document_payload(_recipe_index(document))
        )

    client = RecipeLibraryClient(
        transport=httpx.MockTransport(handler), cache_ttl_seconds=5.0
    )
    current = client.list()
    rate_limited = True
    clock[0] = 6.0

    assert client.list() is current


def test_recipe_library_fails_closed_when_validated_snapshot_exceeds_max_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    clock = [0.0]
    unavailable = False
    calls = 0
    monkeypatch.setattr("vonk_control.recipe_library.time.monotonic", lambda: clock[0])

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if unavailable:
            return httpx.Response(503)
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": "a" * 40})
        return httpx.Response(
            200, json=_contents_document_payload(_recipe_index(document))
        )

    client = RecipeLibraryClient(
        transport=httpx.MockTransport(handler),
        cache_ttl_seconds=5.0,
        stale_retry_interval_seconds=300.0,
        max_stale_seconds=20.0,
    )
    current = client.list()
    unavailable = True
    clock[0] = 6.0
    assert client.list() is current
    clock[0] = 19.999
    assert client.list() is current
    assert calls == 3

    clock[0] = 20.0
    with pytest.raises(RecipeLibraryError) as exc_info:
        client.list()

    assert exc_info.value.code == "recipe_library.unavailable"
    assert calls == 4


def test_recipe_library_clamps_max_stale_window() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(503))

    default = RecipeLibraryClient(transport=transport)
    below_ttl = RecipeLibraryClient(
        transport=transport, cache_ttl_seconds=60.0, max_stale_seconds=-1.0
    )
    above_limit = RecipeLibraryClient(
        transport=transport, max_stale_seconds=30 * 24 * 60 * 60
    )

    assert default._max_stale_seconds == 24 * 60 * 60
    assert below_ttl._max_stale_seconds == 60.0
    assert above_limit._max_stale_seconds == 7 * 24 * 60 * 60


def test_recipe_library_clamps_oversize_ttl_and_expires_at_hard_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    clock = [0.0]
    unavailable = False
    calls = 0
    hard_limit = 7 * 24 * 60 * 60
    monkeypatch.setattr("vonk_control.recipe_library.time.monotonic", lambda: clock[0])

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if unavailable:
            return httpx.Response(503)
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": "a" * 40})
        return httpx.Response(
            200, json=_contents_document_payload(_recipe_index(document))
        )

    client = RecipeLibraryClient(
        transport=httpx.MockTransport(handler),
        cache_ttl_seconds=30 * 24 * 60 * 60,
        max_stale_seconds=30 * 24 * 60 * 60,
    )
    current = client.list()
    unavailable = True

    assert client._cache_ttl_seconds == hard_limit
    assert client._max_stale_seconds == hard_limit
    clock[0] = hard_limit - 0.001
    assert client.list() is current
    assert calls == 2

    clock[0] = hard_limit
    with pytest.raises(RecipeLibraryError) as exc_info:
        client.list()

    assert exc_info.value.code == "recipe_library.unavailable"
    assert calls == 3


def test_recipe_library_late_old_hydration_cannot_poison_new_same_uri_item() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    commit = "a" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": commit})
        return httpx.Response(
            200, json=_contents_document_payload(_recipe_index(document))
        )

    client = RecipeLibraryClient(transport=httpx.MockTransport(handler))
    listed = client.list().items[0]
    bundle = b"validated source bundle"
    bundle_digest = hashlib.sha256(bundle).hexdigest()
    context = RecipeLibrarySourceContext("context", bundle_digest, len(bundle), ())
    old_item = replace(listed, source_context=context)
    new_item = replace(old_item, library_commit="b" * 40)
    client._cached_snapshot = RecipeLibrarySnapshot("b" * 40, (new_item,))
    client._hydrated_bundles[bundle_digest] = bundle

    # The old fetch reached hydration only after the current snapshot advanced.
    late_old = client._hydrate_source_bundle(old_item)
    current = client._hydrate_source_bundle(new_item)

    assert late_old.library_commit == "a" * 40
    assert current.library_commit == "b" * 40
    assert current.source_bundle == bundle
    assert current is not late_old


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("rejected", "recipe_library.request_rejected"),
        ("forbidden", "recipe_library.request_rejected"),
        ("redirect", "recipe_library.redirect_forbidden"),
        ("invalid", "recipe_library.response_invalid"),
        ("digest", "recipe_library.digest_mismatch"),
    ],
)
def test_recipe_library_does_not_mask_non_transient_refresh_failures(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    expected_code: str,
) -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    clock = [0.0]
    refreshing = False
    monkeypatch.setattr("vonk_control.recipe_library.time.monotonic", lambda: clock[0])

    def handler(request: httpx.Request) -> httpx.Response:
        if not refreshing:
            if request.url.path.endswith("/commits/main"):
                return httpx.Response(200, json={"sha": "a" * 40})
            return httpx.Response(
                200, json=_contents_document_payload(_recipe_index(document))
            )
        if failure in {"rejected", "forbidden"}:
            return httpx.Response(401 if failure == "rejected" else 403)
        if failure == "redirect":
            return httpx.Response(302)
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": "b" * 40})
        if failure == "invalid":
            return httpx.Response(200, content=b"not JSON")
        index = _recipe_index(document)
        index["recipes"][0]["content_sha256"] = "0" * 64
        return httpx.Response(200, json=_contents_document_payload(index))

    client = RecipeLibraryClient(
        transport=httpx.MockTransport(handler), cache_ttl_seconds=5.0
    )
    client.list()
    refreshing = True
    clock[0] = 6.0

    with pytest.raises(RecipeLibraryError) as exc_info:
        client.list()

    assert exc_info.value.code == expected_code


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
