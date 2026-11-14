from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import tarfile
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import (
    CatalogSyncState,
    RecipeBuildCode,
    RecipePackageCode,
    WaitReason,
    canonical_message,
)
from vonk_control import recipe_packages
from vonk_control.auth import TokenCodec
from vonk_control.bounded_json import require_mapping
from vonk_control.catalog_service import CatalogService
from vonk_control.catalog_sync import ManagedRecipeCatalogSyncService
from vonk_control.catalog_sync_contract import (
    CatalogSyncTrigger,
    ManagedCatalogSyncRequest,
)
from vonk_control.inventory_repository import (
    InventoryRepository,
    NodeInventoryReport,
)
from vonk_control.library_projection import LibraryProjection
from vonk_control.models import Base, CatalogDocumentRevision, RecipeBuild
from vonk_control.recipe_builds import RecipeBuildService, RecipeBuildUnknown
from vonk_control.recipe_library_types import RecipeLibrarySnapshot, RecipePackageEntry
from vonk_control.recipe_packages import (
    RecipePackageClient,
    RecipePackageError,
)
from vonk_control.recipe_packages.contracts import RecipePackageUnsettled
from vonk_control.recipe_release import RecipeReleaseError
from vonk_control.source_bundles import SourceBundleStore, generate_source_bundle
from vonk_forge_contracts import (
    PACKAGE_MEDIA_TYPE,
    ChatApi,
    ModelDefinition,
    RecipeDefinition,
    document_sha256,
    published_json,
)
from vonk_forge_contracts.qualification_status import QualificationStatus

from .display_fixtures import display_of_model, display_of_recipe
from .index_envelope import index_envelope
from .signed_recipe_release import SignedRecipeRelease, signed_recipe_releases
from .test_recipe_builds import setup


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


def _canonical_package_fixture(
    *, adapter_source: bytes | None = None
) -> tuple[bytes, dict[str, object], bytes]:
    """Create a tiny valid package without depending on another checkout."""
    model = ModelDefinition.model_validate(
        {
            "identity": {
                "publisher": "fixture",
                "slug": "tiny-model",
                "family": {"publisher": "fixture", "slug": "tiny-model"},
                "model": {"publisher": "fixture", "slug": "tiny-model"},
                "version": "1.0.0",
                "variant": "default",
            },
            "requires_token": False,
            "dependencies": [],
            "source": {
                "repository": "https://example.invalid/fixture",
                "revision": "a" * 40,
            },
            "format": {
                "precision": "fp16",
                "quantization": "none",
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
        }
    )
    model_document = model.model_dump(mode="json", exclude_none=True)
    model_digest = document_sha256(model_document)
    recipe = RecipeDefinition.model_validate(
        {
            "identity": {"publisher": "fixture", "slug": "tiny-recipe"},
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
    if adapter_source is not None:
        files["build/adapter.py"] = adapter_source
        files["build/Dockerfile"] += b"COPY adapter.py /adapter.py\n"
    source_digest = generate_source_bundle(
        {
            path.removeprefix("build/"): content
            for path, content in files.items()
            if path.startswith("build/")
        }
    ).sha256
    manifest = {
        "source_bundle_sha256": source_digest,
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
        "source_bundle_sha256": source_digest,
        "document": recipe_document,
        "display": published_json(
            display_of_recipe(
                recipe_document,
                title="Tiny Recipe",
                description="A deterministic package fixture.",
                tags=["fixture"],
            )
        ),
        "content_sha256": recipe_digest,
        "package": {
            "path": "packages/tiny-recipe.tar.gz",
            "source_bundle_sha256": source_digest,
            "sha256": hashlib.sha256(package).hexdigest(),
            "expected_bytes": len(package),
            "recipe_content_sha256": recipe_digest,
            "media_type": PACKAGE_MEDIA_TYPE,
            "minimum_consumer_schema": 2,
        },
        "release": recipe_document["release"],
        "api": None,
        "qualification": {"status": "Not yet verified", "required_sparks": 1},
    }
    index = index_envelope(
        catalog_entities=[
            {
                "source_path": "models/tiny-model.json",
                "document": model_document,
                "display": published_json(
                    display_of_model(
                        model_document,
                        family_title="Tiny Model",
                        model_title="Tiny Model",
                        description="A deterministic package fixture.",
                        tags=["fixture"],
                    )
                ),
                "content_sha256": model_digest,
            }
        ],
        recipes=[row],
    )
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
                    {"tag_name": "v4.0.0", "draft": False, "assets": []},
                    {"tag_name": "v4.9.0", "draft": True, "assets": []},
                    release,
                    {"tag_name": "v4.0.5", "draft": False, "assets": []},
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
    check after the signature: digests, repository authority, and transport.
    """
    verified: list[bytes] = []

    def verify(checksums: bytes, bundle: bytes) -> str:
        if bundle != SIGNED_BUNDLE:
            raise RecipeReleaseError("recipe_release.signature_invalid", "unsigned")
        verified.append(checksums)
        return SIGNED_COMMIT

    monkeypatch.setattr(recipe_packages, "verify_release_checksums", verify)
    return verified


def _release_for(index: bytes, package: bytes, *, tag: str = "v4.1.0") -> _Release:
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
            "v4.1.0/recipe-library.tar"
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
    assert snapshot.version == "4.0.0"
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
    release = _release_for(index, package, tag="v4.0.0")
    client = _client(release, tmp_path / "packages", release="v4.0.0")
    assert client.list().commit == SIGNED_COMMIT
    # Without a relay the reader downloads from github.com and follows only
    # the redirect to GitHub's release asset origin.
    assert release.requests == [
        f"http://127.0.0.1{RELEASES}/tags/v4.0.0",
        (
            "https://github.com/CarstVaartjes/vonk-forge-recipes/releases/download/"
            "v4.0.0/recipe-library.tar"
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
    with pytest.raises(RecipeReleaseError):
        client.list()
    client.close()


@pytest.mark.parametrize("tamper", ["index", "redirect", "nested", "sums"])
def test_only_the_signed_envelope_rejects_the_whole_bundle(
    tmp_path: Path, signed_releases: list[bytes], tamper: str
) -> None:
    index, _, package = _canonical_package_fixture()
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
    with pytest.raises(RecipePackageError):
        client.list()
    assert not tuple((tmp_path / "packages").rglob("*.tar.gz"))
    # Repair the ingress; the failed attempt must not poison a fresh read.
    repaired = _release_for(index, package)
    release.members = repaired.members
    release.redirect_host = repaired.redirect_host
    repaired_snapshot = client.list()
    client.prepare(repaired_snapshot)
    assert client.fetch(repaired_snapshot.items[0].uri).package_handle is not None
    client.close()


_DROPPED = object()


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("kind", "something-else", RecipePackageCode.SCHEMA_INCOMPATIBLE),
        ("schema_version", 3, RecipePackageCode.SCHEMA_INCOMPATIBLE),
        ("source_commit", "not-a-commit", RecipePackageCode.RESPONSE_INVALID),
        ("updated_at", "2026-13-40T00:00:00Z", RecipePackageCode.RESPONSE_INVALID),
        ("vocabulary", _DROPPED, RecipePackageCode.RESPONSE_INVALID),
    ],
)
def test_an_envelope_the_contract_refuses_rejects_the_signed_index(
    tmp_path: Path,
    signed_releases: list[bytes],
    field: str,
    value: object,
    code: RecipePackageCode,
) -> None:
    """The envelope is read through the contract; its refusal keeps its code."""
    index, _, package = _canonical_package_fixture()
    document = json.loads(index)
    if value is _DROPPED:
        del document[field]
    else:
        document[field] = value
    release = _release_for(_canonical(document) + b"\n", package)
    client = _client(release, tmp_path / "packages")
    try:
        with pytest.raises(RecipePackageError) as refused:
            client.list()
        assert refused.value.code == code
    finally:
        client.close()


def test_one_entry_the_contract_cannot_read_never_rejects_the_envelope(
    tmp_path: Path, signed_releases: list[bytes]
) -> None:
    """Principle 1: the envelope is read through the contract, the entries one by one."""
    index, _, package = _canonical_package_fixture()
    document = json.loads(index)
    newer = {"kind": "from-a-newer-contract"}
    document["catalog_entities"][0]["document"] = newer
    document["catalog_entities"][0]["content_sha256"] = document_sha256(newer)
    release = _release_for(_canonical(document) + b"\n", package)
    client = _client(release, tmp_path / "packages")
    try:
        snapshot = client.list()
        [problem] = snapshot.problems
        assert problem.detail.startswith("index catalog_entities entry")
        assert "is skipped: document" in problem.detail
        assert snapshot.items and not snapshot.catalog_entities
    finally:
        client.close()


def test_unreadable_parts_cost_only_their_entry_or_block(
    tmp_path: Path, signed_releases: list[bytes]
) -> None:
    """Unknown words, a malformed entry and bad blocks never stop the sync."""
    index_bytes, _, package = _canonical_package_fixture()
    index = json.loads(index_bytes)
    index["catalog_entities"][0]["display"]["capabilities"].append("teleportation")
    recipe_entry = index["recipes"][0]
    del recipe_entry["display"]
    recipe_entry["prebuilt_image"] = {
        "reference": "ghcr.io/fixture/tiny:latest",
        "build_key": "0" * 64,
    }
    api = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-api-source-build.json")
        .read_text(encoding="utf-8")
    )
    api["identity"] = {"publisher": "fixture", "slug": "tiny-recipe"}
    endpoints = list(api["api"]["endpoints"])
    api["api"]["endpoints"].append("/v1/teleport")
    recipe_entry["api"] = api
    recipe_entry["qualification"]["status"] = "Blessed"
    index["recipes"].append({"source_path": "recipes/broken.json"})
    release = _release_for(_canonical(index) + b"\n", package)
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 11, 14, tzinfo=UTC)
    cursors = TokenCodec(b"s" * 32).cursor_codec()
    catalog = CatalogService(
        sessions,
        clock=lambda: now,
        cursors=cursors,
        source_bundles=SourceBundleStore(tmp_path / "bundles"),
    )
    sync = ManagedRecipeCatalogSyncService(
        sessions,
        catalog=catalog,
        reader=_client(release, tmp_path / "packages"),
        clock=lambda: now,
    )

    sync.sync(
        ManagedCatalogSyncRequest(
            request_key=str(uuid.uuid4()),
            trigger=CatalogSyncTrigger.MANUAL,
            actor="test",
        )
    )

    status = sync.latest()
    assert status is not None
    assert status.state == CatalogSyncState.PARTIAL
    assert (status.imported_count, status.skipped_count) == (1, 1)
    details = "\n".join(problem.detail for problem in status.problems)
    assert "index recipes entry recipes/broken.json is skipped" in details
    assert "recipes prebuilt_image dropped for 1 entry (fixture/tiny-recipe)" in details
    assert "recipes display dropped for 1 entry (fixture/tiny-recipe)" in details
    for word in ("teleportation", "/v1/teleport", "Blessed"):
        assert f"'{word}' is unknown to this Controller" in details
    # What the Library serves: each block read without its unknown word. The
    # recipe is titled by its identity, builds from source, keeps its API facts
    # minus the endpoint and reads "not yet verified"; the Model keeps its
    # known capabilities.
    library = LibraryProjection(sessions, cursors=cursors, clock=lambda: now)
    library.refresh_catalog(observe_storage=False)
    (recipe,) = library.recipe_library(limit=10).recipes
    assert recipe.identity.title == "tiny-recipe"
    assert recipe.recipe_display is None and recipe.prebuilt_image is None
    assert recipe.projected.api is not None
    assert isinstance(recipe.projected.api.api, ChatApi)
    assert list(recipe.projected.api.api.endpoints) == endpoints
    assert recipe.qualification is not None
    assert recipe.qualification.status == QualificationStatus.NOT_YET_VERIFIED
    (model,) = library.models(limit=10).models
    assert model.model_display is not None
    assert list(model.model_display.capabilities) == [
        "image-generation",
        "text-generation",
    ]


def test_a_document_that_differs_from_its_digest_refuses_the_index(
    tmp_path: Path, signed_releases: list[bytes]
) -> None:
    index_bytes, _, package = _canonical_package_fixture()
    index = json.loads(index_bytes)
    index["recipes"][0]["document"]["release"]["version"] = "1.0.1"
    # Signed as published: only the reader's digest check can refuse it.
    client = _client(_release_for(_canonical(index) + b"\n", package), tmp_path)
    try:
        with pytest.raises(RecipePackageError) as refused:
            client.list()
        assert refused.value.code == RecipePackageCode.DIGEST_MISMATCH
    finally:
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
    assert named in str(problem.detail)
    assert "fixture/tiny-recipe" in str(problem.detail)
    assert problem.recipe_uri == (
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
    def entry(slug: str, location: str) -> RecipePackageEntry:
        return RecipePackageEntry(
            publisher="vonk-forge",
            slug=slug,
            source_path="recipes/x.json",
            recipe_content_sha256="b" * 64,
            package_sha256="a" * 64,
            size=1,
            location=location,
            publication_commit=SIGNED_COMMIT,
        )

    packages = {
        "vonk-forge/recipe-0": entry("recipe-0", "packages/../etc/passwd"),
        "vonk-forge/recipe-1": entry("recipe-1", "packages/bad\nname.tar.gz"),
        "vonk-forge/recipe-2": entry("recipe-2", "packages/recipe-2.tar.gz"),
    }
    snapshot, kept = recipe_packages._bind_release(
        RecipeLibrarySnapshot(commit=SIGNED_COMMIT, items=()),
        packages,
        recipe_packages._VerifiedRelease(
            tag="v4.1.0",
            commit=SIGNED_COMMIT,
            assets=frozenset({"recipe-2.tar.gz", "unlisted.tar.gz"}),
            checksums={"recipe-2.tar.gz": "a" * 64},
            checksums_raw=b"",
            bundle_raw=b"",
        ),
    )
    assert list(kept) == ["vonk-forge/recipe-2"]
    details = [str(problem.detail) for problem in snapshot.problems]
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
    with pytest.raises(RecipePackageError):
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
    with pytest.raises(RecipePackageError):
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
    assert conditional == [None, '"v4.1.0"']
    assert len(release.requests) == downloads + 1  # only the conditional check

    # A new release changes the listing: the check no longer answers 304.
    release.tag = "v4.1.1"
    client.list()
    assert len(release.requests) > downloads + 2
    client.close()


def test_signed_content_is_prepared_despite_different_source_provenance(
    tmp_path: Path, signed_releases: list[bytes]
) -> None:
    """Catches rejecting verified package bytes only because a commit label moved."""
    index, _, package = _canonical_package_fixture()
    document = json.loads(index)
    document["source_commit"] = "b" * 40
    index = _canonical(document) + b"\n"
    release = _release_for(index, package)
    client = _client(release, tmp_path / "packages")
    try:
        snapshot = client.list()
        assert snapshot.items
        client.prepare(snapshot)
        first = client.fetch(snapshot.items[0].uri).package_handle
        assert first is not None
        client.prepare(client.list())
        second = client.fetch(snapshot.items[0].uri).package_handle
        assert second == first
        assert first.closure_path.is_dir()
        assert not snapshot.problems
    finally:
        client.close()


def test_a_running_client_waits_visibly_for_its_major_and_adopts_it_on_the_next_tick(
    tmp_path: Path, signed_releases: list[bytes]
) -> None:
    """The window where this Controller runs but its contract major is unpublished.

    Only an older major exists: the read is a typed, visible wait
    (UNAVAILABLE, an unavailable observation), never a refusal of the stored
    catalog. The same client adopts the major on the tick after it appears.
    """
    index, _row, package = _canonical_package_fixture()
    release = _release_for(index, package)
    older_only = True

    def transport(request: httpx2.Request) -> httpx2.Response:
        if older_only and request.url.path == RELEASES:
            return httpx2.Response(
                200, json=[{"tag_name": "v3.2.0", "draft": False, "assets": []}]
            )
        return release.handler(request)

    client = RecipePackageClient(
        api_url="http://127.0.0.1",
        cache_root=tmp_path / "packages",
        transport=httpx2.MockTransport(transport),
    )
    try:
        for _tick in range(2):  # retried every tick, with the same typed answer
            with pytest.raises(RecipePackageUnsettled) as waiting:
                client.list()
            assert waiting.value.code == RecipePackageCode.UNAVAILABLE
            assert waiting.value.typed_reason == WaitReason.OBSERVATION_UNAVAILABLE
            assert "contract v4" in waiting.value.detail
        older_only = False
        snapshot = client.list()
        assert snapshot.version == "4.0.0" and snapshot.items
    finally:
        client.close()


def test_a_published_newer_major_is_noted_on_every_tick_and_the_catalog_still_applies(
    tmp_path: Path, signed_releases: list[bytes]
) -> None:
    """The consumer-first backstop: a Controller behind the newest contract says so.

    The note follows the release listing, so it is present on the tick that
    answers 304 and on one served from the cached snapshot, and it clears when
    the newer major is withdrawn; the catalog of this Controller's own major is
    applied throughout.
    """
    index, _row, package = _canonical_package_fixture()
    release = _release_for(index, package)
    newer: list[dict[str, object]] = [
        {"tag_name": "v5.0.0", "draft": False, "assets": []}
    ]

    def transport(request: httpx2.Request) -> httpx2.Response:
        if request.url.path != RELEASES:
            return release.handler(request)
        etag = f'"listing-{len(newer)}"'
        if request.headers.get("if-none-match") == etag:
            return httpx2.Response(304)
        listing = json.loads(release.handler(request).content) + newer
        return httpx2.Response(200, json=listing, headers={"etag": etag})

    client = RecipePackageClient(
        api_url="http://127.0.0.1",
        cache_root=tmp_path / "packages",
        transport=httpx2.MockTransport(transport),
    )
    try:
        notes = []
        for _tick in range(2):  # the second tick answers 304
            snapshot = client.list()
            assert snapshot.items
            notes.append([note.detail for note in snapshot.display_notes])
        assert notes == [["contract v5 is published; this Controller reads v4"]] * 2
        assert not snapshot.problems

        newer.clear()
        assert client.list().display_notes == ()
    finally:
        client.close()


@pytest.mark.usefixtures(signed_recipe_releases.__name__)
def test_context_publication_must_precede_controller_rollout(tmp_path: Path) -> None:
    # Wrong implementation: silently deriving an unpublished identity lets this
    # Controller roll out before the publisher, hiding the required deploy order.
    index_bytes, row, package = _canonical_package_fixture()
    index = json.loads(index_bytes)
    del index["recipes"][0]["source_bundle_sha256"]
    release = SignedRecipeRelease(
        index, {str(require_mapping(row["package"], "package")["path"]): package}
    )
    client = release.client(tmp_path / "cache")
    snapshot = client.list()
    assert snapshot.items == ()
    [problem] = snapshot.problems
    assert "fixture/tiny-recipe is skipped: source_bundle_sha256" in problem.detail


@pytest.mark.usefixtures(signed_recipe_releases.__name__)
def test_adapter_only_publication_changes_accepted_build_identity(
    tmp_path: Path, postgres_engine: Engine
) -> None:
    # Wrong implementations: dropping source identity at ingress, retaining the
    # old projection on equal recipe JSON, or accepting the reviewed old context.
    sessions, bundles, now, node_id, _ = setup(tmp_path, engine=postgres_engine)
    now += timedelta(microseconds=1)
    InventoryRepository(sessions, clock=lambda: now).record(
        NodeInventoryReport.model_validate_json(
            canonical_message(
                {
                    "node_id": node_id,
                    "observed_at": now,
                    "disk_total_bytes": 2 * 1024**4,
                    "disk_free_bytes": 1024**4,
                    "host_memory_total_bytes": 128 * 1024**3,
                    "host_memory_free_bytes": 100 * 1024**3,
                    "gpu_memory_total_bytes": 128 * 1024**3,
                    "gpu_memory_free_bytes": 100 * 1024**3,
                    "gpu_count": 1,
                    "artifact_store_read_only": False,
                    "capabilities": ("recipe.build.v1", "recipe.build.egress-proxy.v1"),
                    "memory_pool": "separate",
                    "schema_version": 1,
                    "nvidia_driver_version": "unknown",
                    "container_runtime_version": "unknown",
                }
            )
        )
    )
    catalog = CatalogService(
        sessions,
        clock=lambda: now,
        cursors=TokenCodec(b"s" * 32).cursor_codec(),
        source_bundles=bundles,
    )
    builds = RecipeBuildService(sessions, bundles=bundles)
    index_bytes, _, package = _canonical_package_fixture(
        adapter_source=b"print('old')\n"
    )
    index = json.loads(index_bytes)
    location = index["recipes"][0]["package"]["path"]
    release = SignedRecipeRelease(index, {location: package})
    client = release.client(tmp_path / "packages")
    sync = ManagedRecipeCatalogSyncService(
        sessions, catalog=catalog, reader=client, clock=lambda: now
    )

    def synchronize() -> None:
        result = sync.sync(
            ManagedCatalogSyncRequest(
                request_key=str(uuid.uuid4()),
                trigger=CatalogSyncTrigger.MANUAL,
                actor="test",
            )
        )
        assert result.state == CatalogSyncState.CURRENT
        assert not result.problems

    synchronize()
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.publisher == "fixture",
                CatalogDocumentRevision.slug == "tiny-recipe",
            )
        )
        assert revision is not None
        revision_id = revision.id
        original = revision.source_bundle_sha256
    old = builds.plan(revision_id, node_id, now=now)
    builds.record_success(
        old.build_id,
        build_input_sha256=old.build_input_sha256,
        image_digest="sha256:" + "b" * 64,
        oci_layout_sha256="c" * 64,
        image_bytes=500,
        now=now,
    )
    reviewed = builds.prepare_plan(revision_id, node_id, now=now)
    changed_bytes, _, changed_package = _canonical_package_fixture(
        adapter_source=b"print('repaired')\n"
    )
    changed = json.loads(changed_bytes)
    assert changed["recipes"][0]["document"] == index["recipes"][0]["document"]
    # Keep Git provenance fixed too: the new key must follow the changed bytes.
    release.publish(changed, {location: changed_package})
    synchronize()
    with sessions() as session:
        revision = session.get(CatalogDocumentRevision, revision_id)
        assert revision is not None
        current = revision.source_bundle_sha256
        assert current != original
        assert current == changed["recipes"][0]["source_bundle_sha256"]
        count_before = len(session.scalars(select(RecipeBuild)).all())
    assert original is not None
    assert current is not None
    before = dict(bundles.get(original).files)
    after = dict(bundles.get(current).files)
    assert {path for path in before if before[path] != after[path]} == {"adapter.py"}
    assert not builds.resolve(revision_id).cached
    with pytest.raises(RecipeBuildUnknown) as stale, sessions.begin() as session:
        builds.persist_plan_in_session(session, reviewed, now=now)
    assert stale.value.code == RecipeBuildCode.DEPENDENCIES_STALE
    with sessions() as session:
        assert len(session.scalars(select(RecipeBuild)).all()) == count_before
    fresh = builds.prepare_plan(revision_id, node_id, now=now)
    assert fresh.source_bundle_sha256 == current
    assert fresh.build_input_sha256 != old.build_input_sha256
    with sessions.begin() as session:
        accepted = builds.persist_plan_in_session(session, fresh, now=now)
    assert accepted.build_id != old.build_id
    assert accepted.agent_payload["source_bundle_sha256"] == current
