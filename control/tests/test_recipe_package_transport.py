from __future__ import annotations

import json
import gzip
import hashlib
import io
import os
import tarfile
from pathlib import Path

import httpx
import pytest
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256

from vonk_control.recipe_packages import (
    PACKAGE_MEDIA_TYPE,
    PACKAGE_REPOSITORY,
    RecipePackageClient,
)


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _repack(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(files):
            info = tarfile.TarInfo(path)
            info.size = len(files[path])
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            archive.addfile(info, io.BytesIO(files[path]))
    return gzip.compress(stream.getvalue(), compresslevel=9, mtime=0)


def _canonical_package_fixture() -> tuple[bytes, dict[str, object], dict[str, object]]:
    """Create a tiny valid package without depending on another checkout."""
    model = ModelDefinition.model_validate(
        {
            "identity": {
                "publisher": "fixture",
                "slug": "tiny-model",
                "family": {"publisher": "fixture", "slug": "tiny-model", "title": "Tiny Model"},
                "model": {
                    "publisher": "fixture",
                    "slug": "tiny-model",
                    "title": "Tiny Model",
                    "architecture": "transformer",
                },
                "version": "1.0.0",
                "variant": "default",
            },
            "metadata": {"description": "A deterministic package fixture.", "tags": ["fixture"]},
            "access": {"visibility": "public", "gated": False, "authentication": "none"},
            "lineage": {
                "publisher": "fixture",
                "relation": "official",
                "source_model": {"publisher": "fixture", "slug": "tiny-model"},
                "derivation": "Published fixture.",
            },
            "dependencies": [],
            "modalities": ["text"],
            "source": {"repository": "https://example.invalid/fixture", "revision": "a" * 40},
            "format": {"container": "safetensors", "precision": "fp16", "quantization": "none"},
            "parameters": {"total": 1, "active": 1},
            "limits": {"context_tokens": 128, "resolution_pixels": None, "frames": None, "sample_rate_hz": None},
            "license": {
                "spdx": "Apache-2.0",
                "url": "https://www.apache.org/licenses/LICENSE-2.0",
                "attribution": [],
                "operator_acceptance_required": False,
            },
            "files": [
                {
                    "id": "weights",
                    "path": "weights/model.safetensors",
                    "sha256": "b" * 64,
                    "size_bytes": 1,
                    "roles": ["weights"],
                }
            ],
            "capabilities": {
                "facts": [
                    {
                        "capability": "text-generation",
                        "support": "supported",
                        "evidence_status": "declared",
                        "evidence_digest": None,
                    }
                ],
                "provenance": {
                    "source_url": "https://example.invalid/fixture",
                    "source_revision": "a" * 40,
                    "evidence_digest": "c" * 64,
                },
            },
            "provenance": {
                "source_url": "https://example.invalid/fixture",
                "source_revision": "a" * 40,
                "evidence_digest": "c" * 64,
                "attribution": [],
            },
        }
    )
    model_document = model.model_dump(mode="json")
    model_digest = content_sha256(model)
    recipe = RecipeDefinition.model_validate(
        {
            "identity": {"publisher": "fixture", "slug": "tiny-recipe"},
            "metadata": {"title": "Tiny Recipe", "description": "A deterministic package fixture.", "tags": ["fixture"]},
            "models": [
                {
                    "id": "primary",
                    "model": {"publisher": "fixture", "slug": "tiny-model", "content_sha256": model_digest},
                    "files": [
                        {
                            "id": "weights",
                            "file_id": "weights",
                            "roles": ["worker"],
                            "mount": {"target": "/models", "read_only": True},
                        }
                    ],
                }
            ],
            "execution": {
                "mode": "image",
                "image": {"repository": "fixture/tiny", "digest": "d" * 64, "platform": "linux/arm64"},
            },
            "runtime": {
                "engine": "vllm",
                "entrypoint": ["serve"],
                "arguments": [],
                "environment": [],
                "lifecycle": {"pre_start": [], "post_stop": [], "stop_timeout_seconds": 30},
            },
            "topology": {
                "name": "single",
                "mode": "single",
                "node_count": 1,
                "roles": [
                    {
                        "name": "worker",
                        "count": 1,
                        "endpoint_owner": True,
                        "resources": {
                            "memory": {
                                "kind": "unified",
                                "startup_peak_bytes": 1,
                                "steady_state_bytes": 1,
                                "runtime_growth_bytes": 0,
                                "system_reserve_bytes": 0,
                            },
                            "disk": {
                                "image_bytes": 1,
                                "artifact_bytes": 0,
                                "staging_bytes": 0,
                                "cache_bytes": 0,
                                "rollback_bytes": 0,
                                "safety_margin_bytes": 0,
                            },
                        },
                    }
                ],
                "parallelism": {"world_size": 1, "tensor": 1, "pipeline": 1, "data": 1, "backend": "none"},
                "fabric": {"connectivity": "none", "minimum_bandwidth_mbps": 0},
                "start_order": ["worker"],
                "stop_order": ["worker"],
            },
            "interfaces": [{"adapter": "openai", "port": 8000, "model_aliases": ["tiny"], "health_path": "/health"}],
            "validation": {
                "benchmarks": [],
                "serving": {
                    "interface": "openai",
                    "checks": [
                        {
                            "name": "health-and-generation",
                            "kind": "openai.chat",
                            "request": {
                                "transport": "http",
                                "method": "POST",
                                "path": "/v1/chat/completions",
                                "body": {"messages": [{"role": "user", "content": "hello"}]},
                            },
                            "assertions": ["chat.nonempty"],
                        }
                    ],
                },
            },
            "provenance": {"source_kind": "global", "source_reference": "fixture", "attribution": []},
            "settings": {"kind": "generation", "context_tokens": {"value": 128, "change_effect": "none"}},
            "release": {
                "version": "1.0.0",
                "released_at": "2026-01-01",
                "history": [
                    {
                        "version": "1.0.0",
                        "released_at": "2026-01-01",
                        "upgrade_effect": "none",
                        "changes": [{"kind": "initial", "summary": "Initial"}],
                    }
                ],
            },
        }
    )
    recipe_document = recipe.model_dump(mode="json")
    recipe_digest = content_sha256(recipe)
    files = {
        "models/tiny-model.json": _canonical(model_document) + b"\n",
        "recipe.json": _canonical(recipe_document) + b"\n",
    }
    manifest = {
        "schema_version": 2,
        "kind": "recipe-package",
        "package_type": "recipe",
        "recipe_content_sha256": recipe_digest,
        "files": [
            {"path": path, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
            for path, content in sorted(files.items())
        ],
        "build_inputs": [],
    }
    package = _repack({"manifest.json": _canonical(manifest) + b"\n", **files})
    row = {
        "source_path": "recipes/tiny-recipe.json",
        "document": recipe_document,
        "content_sha256": recipe_digest,
        "package": {
            "path": "packages/tiny-recipe.tar.gz",
            "sha256": hashlib.sha256(package).hexdigest(),
            "expected_bytes": len(package),
            "recipe_content_sha256": recipe_digest,
            "media_type": PACKAGE_MEDIA_TYPE,
            "minimum_consumer_schema": 2,
        },
    }
    index = {
        "schema_version": 2,
        "kind": "recipe-library-index",
        "repository": PACKAGE_REPOSITORY,
        "source_commit": "a" * 40,
        "package_contract": {"schema_version": 2, "media_type": PACKAGE_MEDIA_TYPE},
        "recipes": [row],
    }
    return _canonical(index) + b"\n", row, package


def test_production_reader_pins_raw_index_and_package_to_resolved_commit(tmp_path: Path) -> None:
    index, row, package = _canonical_package_fixture()
    publication = "2" * 40
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path.endswith("/commits/main"):
            return httpx.Response(200, json={"sha": publication})
        if request.url.path.endswith("/catalog-index.json"):
            return httpx.Response(200, headers={"content-type": "text/plain"}, content=index)
        if request.url.path.endswith("tiny-recipe.tar.gz"):
            return httpx.Response(
                200,
                headers={"content-type": "application/octet-stream"},
                content=package,
            )
        return httpx.Response(404)

    client = RecipePackageClient(
        None,
        api_url="http://127.0.0.1",
        cache_root=tmp_path / "packages",
        transport=httpx.MockTransport(handler),
    )
    snapshot = client.list()
    item = client.fetch(
        next(
            entry.uri
            for entry in snapshot.items
            if entry.slug == "tiny-recipe"
        )
    )
    assert requests[0].endswith("/repos/CarstVaartjes/vonk-forge-recipes/commits/main")
    assert requests[1] == (
        "https://raw.githubusercontent.com/CarstVaartjes/vonk-forge-recipes/"
        f"{publication}/catalog-index.json"
    )
    assert requests[2] == (
        "https://raw.githubusercontent.com/CarstVaartjes/vonk-forge-recipes/"
        f"{publication}/{row['package']['path']}"
    )
    assert item.package_handle is not None
    assert item.package_handle.publication_commit == publication
    assert item.package_handle.package_size == row["package"]["expected_bytes"]
    assert item.package_handle.package_sha256 == row["package"]["sha256"]
    assert item.package_handle.archive_path.is_file()
    assert item.package_handle.closure_path.is_dir()


def test_publication_network_smoke_at_published_commit(tmp_path: Path) -> None:
    """Opt-in smoke for the real GitHub API/raw publication boundary."""
    if os.environ.get("VONK_RUN_RECIPE_NETWORK_SMOKE") != "1":
        pytest.skip("set VONK_RUN_RECIPE_NETWORK_SMOKE=1 for the public publication smoke")
    client = RecipePackageClient(
        None,
        publication_commit="2001c6502bfdc66141dd7224bfde5d77734e9959",
        cache_root=tmp_path / "packages",
    )
    try:
        snapshot = client.list()
        assert snapshot.repository == "CarstVaartjes/vonk-forge-recipes"
        assert len(snapshot.items) == 84
        item = client.fetch(
            next(
                entry.uri
                for entry in snapshot.items
                if entry.slug == "deepseek-v4-flash-0731-mia-dual"
            )
        )
        assert item.package_handle is not None
        assert item.package_handle.publication_commit == (
            "2001c6502bfdc66141dd7224bfde5d77734e9959"
        )
        assert item.package_handle.package_sha256 == (
            "eb408d2559ab16b7aa3697eb4cf66495eb22cd6da31cec496f540ac76898a581"
        )
        assert item.package_handle.package_size == 72879
        assert item.package_handle.archive_path.is_file()
        assert item.package_handle.closure_path.is_dir()
    finally:
        client.close()
