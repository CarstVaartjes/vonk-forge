from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import tarfile
from copy import deepcopy
from pathlib import Path

import httpx2
import pytest
from vonk_control import recipe_packages
from vonk_control.bounded_json import require_mapping
from vonk_control.recipe_packages import (
    PACKAGE_MEDIA_TYPE,
    PACKAGE_REPOSITORY,
    RecipePackageClient,
    RecipePackageError,
)
from vonk_control.recipe_release import RecipeReleaseError
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


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


def _canonical_package_fixture() -> tuple[bytes, dict[str, object], bytes]:
    """Create a tiny valid package without depending on another checkout."""
    model = ModelDefinition.model_validate(
        {
            "identity": {
                "publisher": "fixture",
                "slug": "tiny-model",
                "family": {
                    "publisher": "fixture",
                    "slug": "tiny-model",
                    "title": "Tiny Model",
                },
                "model": {
                    "publisher": "fixture",
                    "slug": "tiny-model",
                    "title": "Tiny Model",
                    "architecture": "transformer",
                },
                "version": "1.0.0",
                "variant": "default",
            },
            "metadata": {
                "description": "A deterministic package fixture.",
                "tags": ["fixture"],
            },
            "access": {
                "visibility": "public",
                "gated": False,
                "authentication": "none",
            },
            "lineage": {
                "publisher": "fixture",
                "relation": "official",
                "source_model": {"publisher": "fixture", "slug": "tiny-model"},
                "derivation": "Published fixture.",
            },
            "dependencies": [],
            "modalities": ["text"],
            "source": {
                "repository": "https://example.invalid/fixture",
                "revision": "a" * 40,
            },
            "format": {
                "container": "safetensors",
                "precision": "fp16",
                "quantization": "none",
            },
            "parameters": {"total": 1, "active": 1},
            "limits": {
                "context_tokens": 128,
                "resolution_pixels": None,
                "frames": None,
                "sample_rate_hz": None,
            },
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
            "metadata": {
                "title": "Tiny Recipe",
                "description": "A deterministic package fixture.",
                "tags": ["fixture"],
            },
            "models": [
                {
                    "id": "primary",
                    "model": {
                        "publisher": "fixture",
                        "slug": "tiny-model",
                        "content_sha256": model_digest,
                    },
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
                "image": {
                    "repository": "fixture/tiny",
                    "digest": "d" * 64,
                    "platform": "linux/arm64",
                },
            },
            "runtime": {
                "engine": "vllm",
                "entrypoint": ["serve"],
                "arguments": [],
                "environment": [],
                "lifecycle": {
                    "pre_start": [],
                    "post_stop": [],
                    "stop_timeout_seconds": 30,
                },
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
                "parallelism": {
                    "world_size": 1,
                    "tensor": 1,
                    "pipeline": 1,
                    "data": 1,
                    "backend": "none",
                },
                "fabric": {"connectivity": "none", "minimum_bandwidth_mbps": 0},
                "start_order": ["worker"],
                "stop_order": ["worker"],
            },
            "interfaces": [
                {
                    "adapter": "openai",
                    "port": 8000,
                    "model_aliases": ["tiny"],
                    "health_path": "/health",
                }
            ],
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
                                "body": {
                                    "messages": [{"role": "user", "content": "hello"}]
                                },
                            },
                            "assertions": ["chat.nonempty"],
                        }
                    ],
                },
            },
            "provenance": {
                "source_kind": "global",
                "source_reference": "fixture",
                "attribution": [],
            },
            "settings": {
                "kind": "generation",
                "context_tokens": {"value": 128, "change_effect": "none"},
            },
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
            {
                "path": path,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
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
        "catalog_entities": [
            {
                "source_path": "models/tiny-model.json",
                "document": model_document,
                "content_sha256": model_digest,
            }
        ],
        "recipes": [row],
    }
    return _canonical(index) + b"\n", row, package


def _package_with_extra_member(package: bytes) -> bytes:
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(package), mode="r:*") as archive:
        for member in archive.getmembers():
            stream = archive.extractfile(member)
            assert stream is not None
            files[member.name] = stream.read()
    files["fixture.txt"] = b"same recipe, new package bytes\n"
    manifest = json.loads(files["manifest.json"])
    manifest["files"] = [
        {
            "path": path,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }
        for path, content in sorted(files.items())
        if path != "manifest.json"
    ]
    files["manifest.json"] = _canonical(manifest) + b"\n"
    return _repack(files)


SIGNED_COMMIT = "a" * 40
SIGNED_BUNDLE = b'{"fixture": "signed"}'
RELEASES = "/repos/CarstVaartjes/vonk-forge-recipes/releases"


def _checksums(assets: dict[str, bytes]) -> bytes:
    return "".join(
        f"{hashlib.sha256(payload).hexdigest()}  {name}\n"
        for name, payload in sorted(assets.items())
    ).encode()


class _Release:
    """A GitHub release API plus its redirecting asset origin."""

    def __init__(self, tag: str, assets: dict[str, bytes]) -> None:
        self.tag = tag
        self.assets = {"SHA256SUMS": _checksums(assets), **assets}
        self.assets["SHA256SUMS.sigstore.json"] = SIGNED_BUNDLE
        self.redirect_host = "release-assets.githubusercontent.com"
        self.requests: list[str] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(str(request.url))
        path = request.url.path
        if path in {f"{RELEASES}/latest", f"{RELEASES}/tags/{self.tag}"}:
            return httpx2.Response(
                200,
                json={
                    "tag_name": self.tag,
                    "draft": False,
                    "assets": [
                        {"id": 7, "name": name, "state": "uploaded"}
                        for name in self.assets
                    ],
                },
            )
        download = f"/CarstVaartjes/vonk-forge-recipes/releases/download/{self.tag}/"
        if path.startswith(download) and path[len(download) :] in self.assets:
            name = path[len(download) :]
            return httpx2.Response(
                302,
                headers={
                    "location": f"https://{self.redirect_host}/github-production-"
                    f"release-asset/1336002555/{name}?sig=opaque%3D"
                },
            )
        if path.startswith("/github-production-release-asset/1336002555/"):
            assert request.url.query == b"sig=opaque%3D"
            return httpx2.Response(
                200,
                headers={"content-type": "application/octet-stream"},
                content=self.assets[path.rsplit("/", 1)[1]],
            )
        return httpx2.Response(404)


@pytest.fixture
def signed_releases(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Replace Sigstore with a verifier accepting only SIGNED_BUNDLE.

    The real verifier and the recorded production signature are covered by
    test_recipe_release.py; these cases exercise everything the reader must
    check after the signature: digests, commit binding, and transport.
    """
    verified: list[bytes] = []

    def verify(checksums: bytes, bundle: bytes) -> str:
        if bundle != SIGNED_BUNDLE:
            raise RecipeReleaseError("recipe_release.signature_invalid", "unsigned")
        verified.append(checksums)
        return SIGNED_COMMIT

    monkeypatch.setattr(recipe_packages, "verify_release_checksums", verify)
    return verified


def _release_for(index: bytes, package: bytes, *, tag: str = "v1.2.3") -> _Release:
    return _Release(tag, {"catalog-index.json": index, "tiny-recipe.tar.gz": package})


def test_production_reader_accepts_only_the_signed_release_assets(
    tmp_path: Path, signed_releases: list[bytes]
) -> None:
    index, row, package = _canonical_package_fixture()
    release = _release_for(index, package)
    client = RecipePackageClient(
        api_url="http://127.0.0.1:8083",
        asset_url="http://127.0.0.1:8085",
        cache_root=tmp_path / "packages",
        transport=httpx2.MockTransport(release.handler),
    )
    snapshot = client.list()
    item = client.fetch(snapshot.items[0].uri)

    assert signed_releases == [release.assets["SHA256SUMS"]]
    download = "http://127.0.0.1:8085/CarstVaartjes/vonk-forge-recipes/releases/download/v1.2.3"
    asset = "http://127.0.0.1:8085/github-production-release-asset/1336002555"
    assert release.requests == [
        f"http://127.0.0.1:8083{RELEASES}/latest",
        *(
            url
            for name in (
                "SHA256SUMS",
                "SHA256SUMS.sigstore.json",
                "catalog-index.json",
                "tiny-recipe.tar.gz",
            )
            for url in (f"{download}/{name}", f"{asset}/{name}?sig=opaque%3D")
        ),
    ]
    package_metadata = require_mapping(
        row["package"], "canonical fixture package metadata"
    )
    assert snapshot.commit == SIGNED_COMMIT
    assert item.package_handle is not None
    assert item.package_handle.publication_commit == SIGNED_COMMIT
    assert item.package_handle.package_sha256 == package_metadata["sha256"]
    assert item.package_handle.closure_path.is_dir()
    client.close()


def test_production_reader_can_hold_an_exact_release_tag(
    tmp_path: Path, signed_releases: list[bytes]
) -> None:
    index, _, package = _canonical_package_fixture()
    release = _release_for(index, package, tag="v1.0.0")
    client = RecipePackageClient(
        api_url="http://127.0.0.1",
        release="v1.0.0",
        cache_root=tmp_path / "packages",
        transport=httpx2.MockTransport(release.handler),
    )
    assert client.list().commit == SIGNED_COMMIT
    # Without a relay the reader downloads from github.com and follows only
    # the redirect to GitHub's release asset origin.
    assert release.requests[:3] == [
        f"http://127.0.0.1{RELEASES}/tags/v1.0.0",
        (
            "https://github.com/CarstVaartjes/vonk-forge-recipes/releases/download/"
            "v1.0.0/SHA256SUMS"
        ),
        (
            "https://release-assets.githubusercontent.com/github-production-release-"
            "asset/1336002555/SHA256SUMS?sig=opaque%3D"
        ),
    ]
    client.close()


def test_unsigned_release_is_refused_even_with_a_previous_generation(
    tmp_path: Path, signed_releases: list[bytes]
) -> None:
    index, _, package = _canonical_package_fixture()
    release = _release_for(index, package)
    cache = tmp_path / "packages"
    client = RecipePackageClient(
        api_url="http://127.0.0.1",
        cache_root=cache,
        transport=httpx2.MockTransport(release.handler),
    )
    client.prepare(client.list())
    release.assets["SHA256SUMS.sigstore.json"] = b'{"forged": true}'
    with pytest.raises(RecipeReleaseError, match="unsigned"):
        client.list()
    client.close()


@pytest.mark.parametrize(
    ("tamper", "error"),
    [
        ("index", "catalog-index.json does not match SHA256SUMS"),
        ("package", "bytes do not match|does not match SHA256SUMS"),
        ("source", "not built from the signed release commit"),
        ("missing", "does not contain tiny-recipe.tar.gz|not in the signed release"),
        ("redirect", "redirect leaves the GitHub asset origin"),
    ],
)
def test_release_assets_must_match_the_signed_manifest(
    tmp_path: Path, signed_releases: list[bytes], tamper: str, error: str
) -> None:
    index, _, package = _canonical_package_fixture()
    if tamper == "source":
        document = json.loads(index)
        document["source_commit"] = "b" * 40
        index = _canonical(document) + b"\n"
    release = _release_for(index, package)
    if tamper == "index":
        release.assets["catalog-index.json"] = index.replace(b"tiny", b"tinY", 1)
    elif tamper == "package":
        release.assets["tiny-recipe.tar.gz"] = _package_with_extra_member(package)
    elif tamper == "missing":
        del release.assets["tiny-recipe.tar.gz"]
    elif tamper == "redirect":
        release.redirect_host = "objects.example.invalid"
    client = RecipePackageClient(
        api_url="http://127.0.0.1",
        cache_root=tmp_path / "packages",
        transport=httpx2.MockTransport(release.handler),
    )
    with pytest.raises(RecipePackageError, match=error):
        client.prepare(client.list())
    client.close()


def test_restart_offline_reverifies_the_persisted_release(
    tmp_path: Path, signed_releases: list[bytes]
) -> None:
    index, _, package = _canonical_package_fixture()
    release = _release_for(index, package)
    cache = tmp_path / "packages"
    first = RecipePackageClient(
        api_url="http://127.0.0.1",
        cache_root=cache,
        transport=httpx2.MockTransport(release.handler),
    )
    first.prepare(first.list())
    first.close()
    signed_releases.clear()

    def offline(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("offline", request=request)

    restarted = RecipePackageClient(
        api_url="http://127.0.0.1",
        cache_root=cache,
        transport=httpx2.MockTransport(offline),
    )
    snapshot = restarted.list()
    restarted.prepare(snapshot)
    assert snapshot.commit == SIGNED_COMMIT
    assert signed_releases == [release.assets["SHA256SUMS"]]
    handle = restarted.fetch(snapshot.items[0].uri).package_handle
    assert handle is not None
    assert handle.closure_path.is_dir()
    restarted.close()

    # A generation cached without its signature material (for example by the
    # retired raw-commit reader) is never served by the release reader.
    persisted = json.loads((cache / "snapshot.json").read_text())
    del persisted["release"]
    (cache / "snapshot.json").write_text(json.dumps(persisted))
    unsigned = RecipePackageClient(
        api_url="http://127.0.0.1",
        cache_root=cache,
        transport=httpx2.MockTransport(offline),
    )
    with pytest.raises(RecipePackageError, match="unavailable"):
        unsigned.list()
    unsigned.close()


# Opt-in only: downloads every package of a real release (~140 MB) over the
# public network.
@pytest.mark.slow(60)
def test_release_network_smoke(tmp_path: Path) -> None:
    """Opt-in smoke for the real signed GitHub release boundary."""
    if os.environ.get("VONK_RUN_RECIPE_NETWORK_SMOKE") != "1":
        pytest.skip("set VONK_RUN_RECIPE_NETWORK_SMOKE=1 for the public release smoke")
    client = RecipePackageClient(
        release=os.environ.get("VONK_RECIPE_LIBRARY_RELEASE", "latest"),
        cache_root=tmp_path / "packages",
        timeout_seconds=60,
    )
    try:
        snapshot = client.list()
        assert snapshot.repository == "CarstVaartjes/vonk-forge-recipes"
        assert snapshot.items
        client.prepare(snapshot)
        for entry in snapshot.items:
            item = client.fetch(entry.uri)
            assert item.package_handle is not None
            assert item.package_handle.publication_commit == snapshot.commit
            assert item.package_handle.closure_path.is_dir()
    finally:
        client.close()


def test_double_list_keeps_unvalidated_candidate_out_of_previous_good_state(
    tmp_path: Path, signed_releases: list[bytes]
) -> None:
    index, _, package = _canonical_package_fixture()
    release = _release_for(index, package)
    client = RecipePackageClient(
        api_url="http://127.0.0.1",
        cache_root=tmp_path / "packages",
        transport=httpx2.MockTransport(release.handler),
    )
    client.list()
    candidate = client.list()
    client.prepare(candidate)
    assert len([url for url in release.requests if "/tiny-recipe.tar.gz?" in url]) == 1
    client.close()


def test_same_recipe_digest_but_changed_package_bytes_are_fetched(
    tmp_path: Path, signed_releases: list[bytes]
) -> None:
    index, row, package = _canonical_package_fixture()
    changed = _package_with_extra_member(package)
    changed_row = deepcopy(row)
    changed_package = dict(
        require_mapping(row["package"], "canonical fixture package metadata")
    )
    changed_package["sha256"] = hashlib.sha256(changed).hexdigest()
    changed_package["expected_bytes"] = len(changed)
    changed_row["package"] = changed_package
    changed_index = json.loads(index)
    changed_index["recipes"] = [changed_row]
    state = {"release": _release_for(index, package)}

    def handler(request: httpx2.Request) -> httpx2.Response:
        return state["release"].handler(request)

    client = RecipePackageClient(
        api_url="http://127.0.0.1",
        cache_root=tmp_path / "packages",
        transport=httpx2.MockTransport(handler),
    )
    client.prepare(client.list())
    state["release"] = _release_for(_canonical(changed_index) + b"\n", changed)
    client.prepare(client.list())
    assert (
        len([url for url in state["release"].requests if "/tiny-recipe.tar.gz?" in url])
        == 1
    )
    client.close()


def test_failed_candidate_can_retry_against_previous_good_snapshot(
    tmp_path: Path, signed_releases: list[bytes]
) -> None:
    index, row, package = _canonical_package_fixture()
    bad = b"signed, but not a recipe package"
    bad_row = deepcopy(row)
    bad_package = dict(
        require_mapping(row["package"], "canonical fixture package metadata")
    )
    bad_package["sha256"] = hashlib.sha256(bad).hexdigest()
    bad_package["expected_bytes"] = len(bad)
    bad_row["package"] = bad_package
    bad_index = json.loads(index)
    bad_index["recipes"] = [bad_row]
    state = {"release": _release_for(index, package)}

    def handler(request: httpx2.Request) -> httpx2.Response:
        return state["release"].handler(request)

    client = RecipePackageClient(
        api_url="http://127.0.0.1",
        cache_root=tmp_path / "packages",
        transport=httpx2.MockTransport(handler),
    )
    client.prepare(client.list())
    state["release"] = _release_for(_canonical(bad_index) + b"\n", bad)
    with pytest.raises(RecipePackageError, match="extraction failed"):
        client.prepare(client.list())
    state["release"] = _release_for(index, package)
    retried = client.list()
    client.prepare(retried)
    assert client.fetch(retried.items[0].uri).package_handle is not None
    client.close()
