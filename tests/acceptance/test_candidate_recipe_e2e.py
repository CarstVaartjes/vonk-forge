"""Connected acceptance for one producer-published schema-2 Recipe package.

The test consumes the producer's immutable catalog index and package archive,
asks a real Controller to synchronize its configured publication, and compares
canonical Library API and vonkctl projections.  No catalog rows are authored by
the test.  Without the published package fixture and connected Controller,
the connected checks skip with an actionable reason.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from cluster_profiles.generated_control.models.library_recipe_detail import (
    LibraryRecipeDetail,
)
from cluster_profiles.generated_control.models.library_recipe_list import (
    LibraryRecipeList,
)
from cluster_profiles.generated_control.models.library_snapshot import LibrarySnapshot
from cluster_profiles.generated_control.models.model_definition import ModelDefinition
from cluster_profiles.generated_control.models.recipe_definition import RecipeDefinition

PACKAGE_MEDIA_TYPE = "application/vnd.vonk-forge.recipe-package.v2+tar+gzip"
DEFAULT_SLUG = "qwen3-8-flash-next-nvfp4-sglang-dual"
ROOT = Path(__file__).resolve().parents[2]


def _path(name: str, default: str | None = None) -> Path | None:
    value = os.environ.get(name, default)
    return Path(value).expanduser().resolve() if value else None


def _package_inputs() -> tuple[dict[str, Any], dict[str, Any], Path]:
    index_path = _path("VONK_ACCEPTANCE_PACKAGE_INDEX")
    package_root = _path("VONK_ACCEPTANCE_PACKAGE_ROOT")
    if index_path is None or package_root is None:
        pytest.skip(
            "set VONK_ACCEPTANCE_PACKAGE_ROOT and VONK_ACCEPTANCE_PACKAGE_INDEX "
            "to the exact frozen producer package fixture"
        )
    if not index_path.is_file() or not package_root.is_dir():
        pytest.skip("producer package index/archive fixture is unavailable")
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        pytest.fail(f"producer package index cannot be read: {error}")
    if not isinstance(index, dict):
        pytest.fail("producer package index must be an object")
    entries = index.get("recipes")
    if not isinstance(entries, list) or not entries:
        pytest.fail("producer package index has no recipes")
    slug = os.environ.get("VONK_ACCEPTANCE_RECIPE_SLUG", DEFAULT_SLUG)
    entry = next(
        (
            value
            for value in entries
            if isinstance(value, dict)
            and isinstance(value.get("document"), dict)
            and value["document"].get("identity", {}).get("slug") == slug
        ),
        None,
    )
    if entry is None:
        pytest.fail(f"candidate Recipe {slug!r} is absent from producer index")
    return index, entry, package_root


def _candidate_package(
    index: dict[str, Any], entry: dict[str, Any], package_root: Path
) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    package = entry.get("package")
    if not isinstance(package, dict):
        pytest.fail("catalog entry has no package descriptor")
    path = package.get("path")
    digest = package.get("sha256")
    expected_bytes = package.get("expected_bytes")
    if not isinstance(path, str) or Path(path).is_absolute():
        pytest.fail("package path is not a safe relative producer output")
    if not isinstance(digest, str) or len(digest) != 64:
        pytest.fail("package digest is not a sha256 value")
    if not isinstance(expected_bytes, int) or expected_bytes < 1:
        pytest.fail("package size is not a positive integer")
    archive_path = package_root / path
    if not archive_path.is_file():
        pytest.fail(f"published package is missing: {archive_path}")
    archive = archive_path.read_bytes()
    assert len(archive) == expected_bytes
    assert hashlib.sha256(archive).hexdigest() == digest
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            members = {member.name: member for member in tar.getmembers()}
            manifest = json.loads(tar.extractfile(members["manifest.json"]).read())
            recipe_bytes = tar.extractfile(members["recipe.json"]).read()
            recipe = json.loads(recipe_bytes)
    except (
        KeyError,
        OSError,
        tarfile.TarError,
        TypeError,
        AttributeError,
        json.JSONDecodeError,
    ) as error:
        pytest.fail(f"published package cannot be inspected: {error}")
    if not isinstance(manifest, dict) or not isinstance(recipe, dict):
        pytest.fail("published package has invalid JSON members")
    assert package.get("media_type") == PACKAGE_MEDIA_TYPE
    assert manifest["schema_version"] == 2
    assert manifest["kind"] == "recipe-package"
    assert manifest["package_type"] == "recipe"
    assert manifest["recipe_content_sha256"] == entry["content_sha256"]
    manifest_files = manifest.get("files")
    if not isinstance(manifest_files, list):
        pytest.fail("published package manifest has no files list")
    recipe_file = next(
        (
            value
            for value in manifest_files
            if isinstance(value, dict) and value.get("path") == "recipe.json"
        ),
        None,
    )
    if not isinstance(recipe_file, dict):
        pytest.fail("published package manifest has no recipe.json file entry")
    assert recipe_file["size"] == len(recipe_bytes)
    assert recipe_file["sha256"] == hashlib.sha256(recipe_bytes).hexdigest()
    assert recipe == entry["document"]
    return package, archive, recipe


def _identity_key(value: dict[str, Any], digest: str) -> tuple[str, str, str]:
    identity = value.get("identity")
    if not isinstance(identity, dict):
        pytest.fail("catalog document has no identity")
    publisher = identity.get("publisher")
    slug = identity.get("slug")
    if not isinstance(publisher, str) or not isinstance(slug, str):
        pytest.fail("catalog identity is incomplete")
    return publisher, slug, digest


def _recipe_summaries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for model in payload.get("models", []):
        if isinstance(model, dict):
            summaries.extend(
                recipe for recipe in model.get("recipes", []) if isinstance(recipe, dict)
            )
    summaries.extend(
        recipe
        for recipe in payload.get("unlinked_recipes", [])
        if isinstance(recipe, dict)
    )
    return summaries


def _model_reference_keys(index: dict[str, Any]) -> set[tuple[str, str, str]]:
    keys: set[tuple[str, str, str]] = set()
    for row in index["recipes"]:
        document = row.get("document")
        if not isinstance(document, dict):
            pytest.fail("Recipe index row has no document")
        models = document.get("models")
        if not isinstance(models, list):
            pytest.fail("canonical Recipe document has no models list")
        for selection in models:
            if not isinstance(selection, dict) or not isinstance(selection.get("model"), dict):
                pytest.fail("Recipe model selection is malformed")
            model = selection["model"]
            digest = model.get("content_sha256")
            if not isinstance(digest, str):
                pytest.fail("Recipe model selection has no content digest")
            keys.add(_identity_key(model, digest))
    return keys


def _fetch_publication_index(
    package_index_url: str,
    timeout: float,
    *,
    transport: httpx.BaseTransport | None = None,
) -> httpx.Response:
    client_options: dict[str, Any] = {"timeout": timeout, "trust_env": False}
    if transport is not None:
        client_options["transport"] = transport
    with httpx.Client(**client_options) as client:
        return client.get(package_index_url)


def test_candidate_package_is_exact_self_contained_and_producer_bound() -> None:
    index, entry, package_root = _package_inputs()
    assert index["schema_version"] == 2
    assert index["kind"] == "recipe-library-index"
    package_contract = index.get("package_contract")
    assert isinstance(package_contract, dict)
    assert package_contract["schema_version"] == 2
    assert package_contract["media_type"] == PACKAGE_MEDIA_TYPE
    package, _archive, recipe = _candidate_package(index, entry, package_root)
    assert package["path"].startswith(package_contract["path_prefix"])
    RecipeDefinition.from_dict(recipe)
    model_rows = [
        row
        for row in index.get("catalog_entities", [])
        if isinstance(row, dict)
        and isinstance(row.get("document"), dict)
        and row["document"].get("kind") == "model"
    ]
    if not model_rows:
        pytest.fail("published fixture has no canonical Model documents")
    for row in model_rows:
        ModelDefinition.from_dict(row["document"])
    library_root = _path("VONK_ACCEPTANCE_LIBRARY_ROOT", "/opt/vonk-forge-recipes")
    if library_root is not None:
        source = library_root / str(entry["source_path"])
        if source.is_file():
            assert json.loads(source.read_text(encoding="utf-8")) == recipe


def test_publication_fetch_does_not_forward_controller_authorization() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("catalog-index.json"):
            return httpx.Response(200, json={"schema_version": 2}, request=request)
        return httpx.Response(204, request=request)

    transport = httpx.MockTransport(handle)
    publication = _fetch_publication_index(
        "https://raw.example.invalid/immutable/catalog-index.json",
        timeout=1,
        transport=transport,
    )
    with httpx.Client(
        base_url="https://controller.example.invalid",
        headers={"Authorization": "Bearer controller-token"},
        timeout=1,
        trust_env=False,
        transport=transport,
    ) as controller:
        controller_response = controller.get("/api/v1/library")

    assert publication.status_code == 200
    assert controller_response.status_code == 204
    assert requests[0].headers.get("authorization") is None
    assert requests[1].headers["authorization"] == "Bearer controller-token"


def test_recipe_producer_output_is_fresh_before_controller_sync() -> None:
    _index, _entry, _package_root = _package_inputs()
    library_root = _path("VONK_ACCEPTANCE_LIBRARY_ROOT", "/opt/vonk-forge-recipes")
    producer = library_root / "tools/build-catalog-index" if library_root else None
    if producer is None or not producer.is_file():
        pytest.skip("recipe producer checkout is unavailable")
    result = subprocess.run(
        [sys.executable, os.fspath(producer), "--check"],
        check=False,
        capture_output=True,
        text=True,
        cwd=library_root,
        timeout=120,
    )
    assert result.returncode == 0, (result.stdout[-1024:], result.stderr[-1024:])


def test_controller_sync_exposes_canonical_library_documents_to_api_and_cli() -> None:
    index, entry, package_root = _package_inputs()
    _candidate_package(index, entry, package_root)
    base_url = os.environ.get("VONK_ACCEPTANCE_CONTROL_URL")
    package_index_url = os.environ.get("VONK_ACCEPTANCE_PACKAGE_INDEX_URL")
    token_path = _path("VONK_ACCEPTANCE_TOKEN_FILE")
    if not base_url or not package_index_url or token_path is None or not token_path.is_file():
        pytest.skip(
            "set VONK_ACCEPTANCE_CONTROL_URL, VONK_ACCEPTANCE_PACKAGE_INDEX_URL "
            "and VONK_ACCEPTANCE_TOKEN_FILE for connected Controller checks"
        )
    token = token_path.read_text(encoding="ascii").strip()
    if not token or any(character.isspace() for character in token):
        pytest.fail("acceptance token file is empty or contains whitespace")
    headers = {"Authorization": f"Bearer {token}"}
    timeout = float(os.environ.get("VONK_ACCEPTANCE_TIMEOUT_SECONDS", "30"))
    request_key = str(uuid.uuid4())
    expected_recipe_rows = index["recipes"]
    expected_recipe_keys = {
        _identity_key(row["document"], row["content_sha256"])
        for row in expected_recipe_rows
    }
    expected_models = {
        _identity_key(row["document"], row["content_sha256"]): row["document"]
        for row in index["catalog_entities"]
        if isinstance(row, dict)
        and isinstance(row.get("document"), dict)
        and row["document"].get("kind") == "model"
    }
    expected_model_references = _model_reference_keys(index)

    with httpx.Client(
        base_url=base_url.rstrip("/"),
        headers=headers,
        timeout=timeout,
        trust_env=False,
    ) as client:
        publication = _fetch_publication_index(package_index_url, timeout)
        assert publication.status_code == 200, publication.text[:1024]
        remote_index = publication.json()
        assert remote_index["schema_version"] == index["schema_version"]
        assert remote_index["source_commit"] == index["source_commit"]
        remote_entry = next(
            row
            for row in remote_index["recipes"]
            if row["content_sha256"] == entry["content_sha256"]
        )
        assert remote_entry["package"] == entry["package"]

        sync = client.post(
            "/api/v1/catalog/managed-recipes/sync",
            json={"request_key": request_key, "expected_commit": index["source_commit"]},
        )
        assert sync.status_code == 200, sync.text[:1024]
        sync_payload = sync.json()
        assert sync_payload["state"] == "current"
        assert sync_payload["commit"] == index["source_commit"]
        assert sync_payload["expected_commit"] == index["source_commit"]
        assert sync_payload["total_count"] == len(expected_recipe_rows)
        assert sync_payload["processed_count"] == len(expected_recipe_rows)
        assert sync_payload["imported_count"] + sync_payload["unchanged_count"] == len(
            expected_recipe_rows
        )
        assert sync_payload["problems"] == []

        library_response = client.get("/api/v1/library", params={"limit": 512})
        assert library_response.status_code == 200, library_response.text[:1024]
        library_payload = library_response.json()
        library = LibrarySnapshot.from_dict(library_payload)
        assert library.schema_version == 2
        actual_models = {
            _identity_key(model["model"], model["model"]["content_sha256"]): model
            for model in library_payload["models"]
        }
        assert set(actual_models) == set(expected_models)
        actual_recipe_keys = {
            _identity_key(recipe, recipe["content_sha256"])
            for recipe in _recipe_summaries(library_payload)
        }
        assert actual_recipe_keys == expected_recipe_keys
        actual_unlinked_models = {
            key for key, model in actual_models.items() if model["recipes"] == []
        }
        assert actual_unlinked_models == set(expected_models) - expected_model_references
        for key, model in actual_models.items():
            assert model["model_document"] == expected_models[key]
            assert model["model_document"]["files"] == expected_models[key]["files"]
            assert model["model_document"]["capabilities"] == expected_models[key]["capabilities"]
        LibrarySnapshot.from_dict(library_payload)

        recipe_list_response = client.get("/api/v1/library/recipes", params={"limit": 512})
        assert recipe_list_response.status_code == 200, recipe_list_response.text[:1024]
        recipe_list_payload = recipe_list_response.json()
        recipe_list = LibraryRecipeList.from_dict(recipe_list_payload)
        assert recipe_list.schema_version == 2
        assert {
            _identity_key(recipe, recipe["content_sha256"])
            for recipe in recipe_list_payload["recipes"]
        } == expected_recipe_keys
        recipe_by_key = {
            _identity_key(recipe, recipe["content_sha256"]): recipe
            for recipe in recipe_list_payload["recipes"]
        }
        candidate_key = _identity_key(entry["document"], entry["content_sha256"])
        candidate_summary = recipe_by_key[candidate_key]
        assert candidate_summary["recipe_document"] == entry["document"]
        assert candidate_summary["recipe_document"]["runtime"] == entry["document"]["runtime"]
        assert candidate_summary["recipe_document"]["release"] == entry["document"]["release"]
        assert candidate_summary["recipe_document"]["topology"] == entry["document"]["topology"]
        assert candidate_summary["recipe_document"]["settings"] == entry["document"]["settings"]
        assert candidate_summary["recipe_revision_id"] not in {
            candidate_summary["recipe_id"],
            candidate_summary["content_sha256"],
        }

        detail_response = client.get(
            f"/api/v1/library/recipes/{candidate_summary['recipe_id']}"
        )
        assert detail_response.status_code == 200, detail_response.text[:1024]
        detail_payload = detail_response.json()
        detail = LibraryRecipeDetail.from_dict(detail_payload)
        assert detail.schema_version == 2
        assert detail_payload["recipe"] == {
            key: candidate_summary[key]
            for key in (
                "recipe_id",
                "recipe_revision_id",
                "publisher",
                "slug",
                "content_sha256",
                "title",
                "description",
            )
        }
        assert detail_payload["definition"] == entry["document"]
        selections = entry["document"]["models"]
        assert [item["selection"] for item in detail_payload["model_documents"]] == selections
        expected_model_documents = {
            _identity_key(row["document"], row["content_sha256"]): row["document"]
            for row in index["catalog_entities"]
            if isinstance(row, dict)
            and isinstance(row.get("document"), dict)
            and row["document"].get("kind") == "model"
        }
        assert [
            item["model_document"]
            for item in detail_payload["model_documents"]
        ] == [
            expected_model_documents[
                _identity_key(selection["model"], selection["model"]["content_sha256"])
            ]
            for selection in selections
        ]
        assert [
            item["model_document"]["files"]
            for item in detail_payload["model_documents"]
        ] == [
            expected_model_documents[
                _identity_key(selection["model"], selection["model"]["content_sha256"])
            ]["files"]
            for selection in selections
        ]

    cli = _path("VONK_ACCEPTANCE_CLI", str(ROOT / "bin/vonkctl"))
    assert cli is not None and cli.is_file(), f"vonkctl is missing: {cli}"
    cli_env = {
        **os.environ,
        "VONK_CONTROL_URL": base_url,
        "VONK_CONTROL_TOKEN_FILE": os.fspath(token_path),
    }
    listed = subprocess.run(
        [os.fspath(cli), "--json", "library", "list", "--all"],
        check=False,
        capture_output=True,
        text=True,
        env=cli_env,
        timeout=timeout,
    )
    assert listed.returncode == 0, listed.stderr[-2048:]
    cli_snapshot_payload = json.loads(listed.stdout)
    LibrarySnapshot.from_dict(cli_snapshot_payload)
    cli_recipe_keys = {
        _identity_key(recipe, recipe["content_sha256"])
        for recipe in _recipe_summaries(cli_snapshot_payload)
    }
    assert cli_recipe_keys == expected_recipe_keys
    cli_candidate = next(
        recipe
        for recipe in _recipe_summaries(cli_snapshot_payload)
        if recipe["content_sha256"] == entry["content_sha256"]
    )
    assert cli_candidate["recipe_revision_id"] == candidate_summary["recipe_revision_id"]
    assert cli_candidate["recipe_document"] == entry["document"]

    shown = subprocess.run(
        [os.fspath(cli), "--json", "library", "show", candidate_summary["recipe_id"]],
        check=False,
        capture_output=True,
        text=True,
        env=cli_env,
        timeout=timeout,
    )
    assert shown.returncode == 0, shown.stderr[-2048:]
    cli_detail_payload = json.loads(shown.stdout)
    LibraryRecipeDetail.from_dict(cli_detail_payload)
    assert cli_detail_payload["recipe"]["recipe_revision_id"] == candidate_summary[
        "recipe_revision_id"
    ]
    assert cli_detail_payload["definition"] == entry["document"]
