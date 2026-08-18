from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
from vonk_control.recipe_contract import recipe_content_sha256
from vonk_control.recipe_library import RecipeLibraryClient, RecipeLibraryError

FIXTURE = Path(__file__).parent / "fixtures/global/recipe-v1-minimal.json"


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
        encoded = base64.b64encode(json.dumps(document).encode()).decode()
        return httpx.Response(
            200,
            json={"encoding": "base64", "content": encoded},
        )

    with pytest.raises(RecipeLibraryError, match="digest"):
        RecipeLibraryClient(transport=httpx.MockTransport(handler)).fetch(
            "vonk://catalog/vonk-forge/synthetic-tiny-openai@sha256:" + "0" * 64
        )
