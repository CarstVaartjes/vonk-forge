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
from vonk_control.recipe_library_types import RecipeLibrarySnapshot
from vonk_control.recipe_packages import (
    PACKAGE_MEDIA_TYPE,
    PACKAGE_REPOSITORY,
    RecipePackageClient,
    RecipePackageError,
)
from vonk_control.recipe_release import RecipeReleaseError
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, document_sha256


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
                },
                "version": "1.0.0",
                "variant": "default",
            },
            "metadata": {
                "description": "A deterministic package fixture.",
                "tags": ["fixture"],
            },
            "requires_token": False,
            "dependencies": [],
            "modalities": ["text"],
            "source": {
                "repository": "https://example.invalid/fixture",
                "revision": "a" * 40,
            },
            "format": {
                "precision": "fp16",
                "quantization": "none",
            },
            "license": {
                "spdx": "Apache-2.0",
                "url": "https://www.apache.org/licenses/LICENSE-2.0",
                "attribution": [],
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
            "capabilities": ["text-generation"],
        }
    )
    model_document = model.model_dump(mode="json", exclude_none=True)
    model_digest = document_sha256(model_document)
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
                            "mount": {"target": "/models"},
                        }
                    ],
                }
            ],
            "execution": {
                "build": {
                    "base_image": {"repository": "fixture/tiny", "digest": "d" * 64},
                    "context": {"path": "build"},
                    "dockerfile": "build/Dockerfile",
                    "patches": [],
                    "network": {"hosts": []},
                },
            },
            "runtime": {
                "engine": "vllm",
                "entrypoint": ["serve"],
                "arguments": [],
                "environment": [],
                "lifecycle": {"stop_timeout_seconds": 30},
            },
            "topology": {
                "name": "single",
                "node_count": 1,
                "roles": [
                    {
                        "name": "worker",
                        "count": 1,
                        "endpoint_owner": True,
                        "resources": {
                            "memory": {"peak_bytes": 1, "reserve_bytes": 0},
                            "disk": {
                                "image_bytes": 1,
                                "artifact_bytes": 0,
                                "working_bytes": 0,
                                "safety_margin_bytes": 0,
                            },
                        },
                    }
                ],
                "parallelism": {
                    "tensor": 1,
                    "pipeline": 1,
                    "data": 1,
                    "backend": "none",
                },
                "start_order": ["worker"],
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
                "source_reference": "fixture",
                "attribution": [],
            },
            "settings": {
                "kind": "generation",
                "context_tokens": {"value": 128, "change_effect": "none"},
            },
            "release": {"version": "1.0.0", "released_at": "2026-01-01"},
        }
    )
    recipe_document = recipe.model_dump(mode="json", exclude_none=True)
    recipe_digest = document_sha256(recipe_document)
    base_image = "fixture/tiny@sha256:" + "d" * 64
    files = {
        "build/Dockerfile": f"FROM {base_image}\n".encode(),
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
        "build_inputs": [
            {"kind": "oci-image", "reference": base_image, "platform": "linux/arm64"}
        ],
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
        "contract_version": "2.0.0",
        "updated_at": "2026-09-28T12:00:00Z",
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


def _tar(members: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name in sorted(members):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(members[name]), 0o644
            archive.addfile(info, io.BytesIO(members[name]))
    return stream.getvalue()


class _Release:
    """A GitHub release API serving the one library bundle via a redirect.

    ``members`` are the bundle's files; SHA256SUMS signs the listed assets
    and a test may tamper with any member before the next read.
    """

    def __init__(self, tag: str, assets: dict[str, bytes]) -> None:
        self.tag = tag
        self.members = {"SHA256SUMS": _checksums(assets), **assets}
        self.members["SHA256SUMS.sigstore.json"] = SIGNED_BUNDLE
        self.published = True
        self.redirect_host = "release-assets.githubusercontent.com"
        self.requests: list[str] = []

    @property
    def library(self) -> bytes:
        return _tar(self.members)

    @property
    def library_downloads(self) -> int:
        return sum("recipe-library.tar?" in url for url in self.requests)

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(str(request.url))
        path = request.url.path
        library = self.library
        release = {
            "tag_name": self.tag,
            "draft": False,
            "assets": [
                {
                    "id": 7,
                    "name": "recipe-library.tar",
                    "state": "uploaded",
                    "digest": f"sha256:{hashlib.sha256(library).hexdigest()}",
                }
            ]
            if self.published
            else [],
        }
        if path == RELEASES:
            # The reader follows the newest non-draft release of its own
            # contract major, whatever the listing order.
            return httpx2.Response(
                200,
                json=[
                    {"tag_name": "v3.0.0", "draft": False, "assets": []},
                    {"tag_name": "v2.9.0", "draft": True, "assets": []},
                    release,
                    {"tag_name": "v2.0.5", "draft": False, "assets": []},
                ],
            )
        if path == f"{RELEASES}/tags/{self.tag}":
            return httpx2.Response(200, json=release)
        download = f"/CarstVaartjes/vonk-forge-recipes/releases/download/{self.tag}/"
        if path == f"{download}recipe-library.tar" and self.published:
            return httpx2.Response(
                302,
                headers={
                    "location": f"https://{self.redirect_host}/github-production-"
                    "release-asset/1336002555/recipe-library.tar?sig=opaque%3D"
                },
            )
        if path == "/github-production-release-asset/1336002555/recipe-library.tar":
            assert request.url.query == b"sig=opaque%3D"
            return httpx2.Response(
                200,
                headers={"content-type": "application/octet-stream"},
                content=library,
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


def _release_for(index: bytes, package: bytes, *, tag: str = "v2.1.0") -> _Release:
    return _Release(tag, {"catalog-index.json": index, "tiny-recipe.tar.gz": package})


def _client(served: _Release, cache: Path, **options: object) -> RecipePackageClient:
    return RecipePackageClient(
        api_url="http://127.0.0.1",
        cache_root=cache,
        transport=httpx2.MockTransport(served.handler),
        **options,  # type: ignore[arg-type]
    )


def test_production_reader_downloads_one_signed_bundle(
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
    client.prepare(snapshot)
    item = client.fetch(snapshot.items[0].uri)

    assert signed_releases == [release.members["SHA256SUMS"]]
    # One listing call and one bundle download; nothing per package.
    assert release.requests == [
        f"http://127.0.0.1:8083{RELEASES}?per_page=100",
        (
            "http://127.0.0.1:8085/CarstVaartjes/vonk-forge-recipes/releases/download/"
            "v2.1.0/recipe-library.tar"
        ),
        (
            "http://127.0.0.1:8085/github-production-release-asset/1336002555/"
            "recipe-library.tar?sig=opaque%3D"
        ),
    ]
    package_metadata = require_mapping(
        row["package"], "canonical fixture package metadata"
    )
    assert snapshot.commit == SIGNED_COMMIT
    assert snapshot.version == "2.0.0"
    assert snapshot.updated_at is not None
    assert snapshot.updated_at.isoformat() == "2026-09-28T12:00:00+00:00"
    assert item.package_handle is not None
    assert item.package_handle.publication_commit == SIGNED_COMMIT
    assert item.package_handle.package_sha256 == package_metadata["sha256"]
    assert item.package_handle.closure_path.is_dir()
    client.close()


def test_production_reader_can_hold_an_exact_release_tag(
    tmp_path: Path, signed_releases: list[bytes]
) -> None:
    index, _, package = _canonical_package_fixture()
    release = _release_for(index, package, tag="v2.0.0")
    client = _client(release, tmp_path / "packages", release="v2.0.0")
    assert client.list().commit == SIGNED_COMMIT
    # Without a relay the reader downloads from github.com and follows only
    # the redirect to GitHub's release asset origin.
    assert release.requests == [
        f"http://127.0.0.1{RELEASES}/tags/v2.0.0",
        (
            "https://github.com/CarstVaartjes/vonk-forge-recipes/releases/download/"
            "v2.0.0/recipe-library.tar"
        ),
        (
            "https://release-assets.githubusercontent.com/github-production-release-"
            "asset/1336002555/recipe-library.tar?sig=opaque%3D"
        ),
    ]
    client.close()


def test_unsigned_bundle_is_refused_even_with_a_previous_generation(
    tmp_path: Path, signed_releases: list[bytes]
) -> None:
    index, _, package = _canonical_package_fixture()
    release = _release_for(index, package)
    client = _client(release, tmp_path / "packages")
    client.prepare(client.list())
    release.members["SHA256SUMS.sigstore.json"] = b'{"forged": true}'
    with pytest.raises(RecipeReleaseError, match="unsigned"):
        client.list()
    client.close()


@pytest.mark.parametrize(
    ("tamper", "error"),
    [
        ("index", "catalog-index.json does not match SHA256SUMS"),
        ("source", "not built from the signed release commit"),
        ("redirect", "redirect leaves the GitHub asset origin"),
        ("nested", "member <invalid asset name> is not a unique flat file"),
        ("sums", "lacks a bounded SHA256SUMS"),
    ],
)
def test_only_the_signed_envelope_rejects_the_whole_bundle(
    tmp_path: Path, signed_releases: list[bytes], tamper: str, error: str
) -> None:
    index, _, package = _canonical_package_fixture()
    if tamper == "source":
        document = json.loads(index)
        document["source_commit"] = "b" * 40
        index = _canonical(document) + b"\n"
    release = _release_for(index, package)
    if tamper == "index":
        release.members["catalog-index.json"] = index.replace(b"tiny", b"tinY", 1)
    elif tamper == "redirect":
        release.redirect_host = "objects.example.invalid"
    elif tamper == "nested":
        release.members["packages/tiny-recipe.tar.gz"] = package
    elif tamper == "sums":
        del release.members["SHA256SUMS"]
    client = _client(release, tmp_path / "packages")
    with pytest.raises(RecipePackageError, match=error):
        client.list()
    client.close()


@pytest.mark.parametrize(
    ("tamper", "named"),
    [
        ("bytes", "tiny-recipe.tar.gz"),
        ("missing", "tiny-recipe.tar.gz"),
        ("sums", "package.sha256"),
    ],
)
def test_one_bad_package_skips_only_its_recipe_and_names_it(
    tmp_path: Path, signed_releases: list[bytes], tamper: str, named: str
) -> None:
    # A signed bundle whose package is absent or altered (for example a
    # publish that died mid-update) applies every other recipe; the skipped
    # one is reported by name instead of refusing the whole library.
    index, row, package = _canonical_package_fixture()
    release = _release_for(index, package)
    if tamper == "bytes":
        release.members["tiny-recipe.tar.gz"] = _package_with_extra_member(package)
    elif tamper == "missing":
        del release.members["tiny-recipe.tar.gz"]
    else:
        release.members["SHA256SUMS"] = (
            f"{hashlib.sha256(index).hexdigest()}  catalog-index.json\n"
            + "0" * 64
            + "  tiny-recipe.tar.gz\n"
        ).encode()
    client = _client(release, tmp_path / "packages")
    snapshot = client.list()
    client.prepare(snapshot)
    assert snapshot.items == ()
    [problem] = snapshot.problems
    assert problem["code"] == "recipe_package.release_incomplete"
    assert named in str(problem["detail"])
    assert "fixture/tiny-recipe" in str(problem["detail"])
    assert problem["recipe_uri"] == (
        f"vonk://catalog/fixture/tiny-recipe@sha256:{row['content_sha256']}"
    )
    client.close()


def test_release_mid_update_keeps_the_previous_verified_library(
    tmp_path: Path, signed_releases: list[bytes]
) -> None:
    index, _, package = _canonical_package_fixture()
    release = _release_for(index, package)
    client = _client(release, tmp_path / "packages")
    first = client.list()
    client.prepare(first)
    client.close()
    # The bundle asset is being replaced: the restarted reader keeps serving
    # the last verified generation instead of failing or blocking.
    release.published = False
    restarted = _client(release, tmp_path / "packages")
    assert restarted.list().commit == first.commit
    restarted.close()


def test_bundle_member_names_are_bounded_and_safe() -> None:
    packages = {
        "vonk-forge/recipe-0": {
            "location": "packages/../etc/passwd",
            "package_sha256": "a" * 64,
        },
        "vonk-forge/recipe-1": {
            "location": "packages/bad\nname.tar.gz",
            "package_sha256": "a" * 64,
        },
        "vonk-forge/recipe-2": {
            "location": "packages/recipe-2.tar.gz",
            "package_sha256": "a" * 64,
        },
    }
    snapshot, kept = recipe_packages._bind_release(
        RecipeLibrarySnapshot(commit=SIGNED_COMMIT, items=()),
        packages,
        recipe_packages._VerifiedRelease(
            tag="v2.1.0",
            commit=SIGNED_COMMIT,
            assets=frozenset({"recipe-2.tar.gz", "unlisted.tar.gz"}),
            checksums={"recipe-2.tar.gz": "a" * 64},
            checksums_raw=b"",
            bundle_raw=b"",
        ),
    )
    assert list(kept) == ["vonk-forge/recipe-2"]
    details = [str(problem["detail"]) for problem in snapshot.problems]
    assert "package.path" in details[0] and "vonk-forge/recipe-0" in details[0]
    assert "\n" not in details[1] and all(len(item) <= 256 for item in details)


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
    assert signed_releases == [release.members["SHA256SUMS"]]
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
    # The unchanged bundle digest is recognised: it is downloaded once.
    assert release.library_downloads == 1
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
    snapshot = client.list()
    client.prepare(snapshot)
    assert state["release"].library_downloads == 1
    handle = client.fetch(snapshot.items[0].uri).package_handle
    assert handle is not None
    assert handle.package_sha256 == hashlib.sha256(changed).hexdigest()
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


def test_unchanged_release_is_noticed_cheaply_and_a_new_one_is_fetched(
    tmp_path: Path, signed_releases: list[bytes]
) -> None:
    """Checking for a new recipes release costs one conditional request."""

    index, _, package = _canonical_package_fixture()
    release = _release_for(index, package)
    conditional: list[str | None] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == RELEASES:
            conditional.append(request.headers.get("if-none-match"))
            release.requests.append(str(request.url))
            if request.headers.get("if-none-match") == f'"{release.tag}"':
                return httpx2.Response(304)
            response = release.handler(request)
            response.headers["etag"] = f'"{release.tag}"'
            return response
        return release.handler(request)

    client = RecipePackageClient(
        api_url="http://127.0.0.1",
        cache_root=tmp_path / "packages",
        transport=httpx2.MockTransport(handler),
    )
    first = client.list()
    downloads = len(release.requests)
    assert client.list() == first
    assert conditional == [None, '"v2.1.0"']
    assert len(release.requests) == downloads + 1  # only the conditional check

    # A new release changes the listing: the check no longer answers 304.
    release.tag = "v2.1.1"
    client.list()
    assert len(release.requests) > downloads + 2
    client.close()
