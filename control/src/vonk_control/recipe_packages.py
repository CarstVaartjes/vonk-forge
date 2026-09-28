"""Schema-2 recipe package reader for the Controller catalog sync."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import httpx2
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError
from vonk_forge_contracts import (
    CONTRACT_MAJOR,
    ModelDefinition,
    RecipeDefinition,
    document_sha256,
    read_model,
    read_recipe,
)
from vonk_forge_contracts.resolver import validate_recipe_models

from .bounded_json import integer, require_integer
from .recipe_library_types import (
    RecipeLibraryError,
    RecipeLibraryItem,
    RecipeLibraryRelease,
    RecipeLibrarySnapshot,
)
from .recipe_release import (
    MAX_BUNDLE_BYTES,
    MAX_CHECKSUMS_BYTES,
    RELEASE_BUNDLE,
    RELEASE_CHECKSUMS,
    RELEASE_INDEX,
    parse_release_checksums,
    verify_release_checksums,
)
from .source_bundles import SourceBundleError, generate_source_bundle

PACKAGE_SCHEMA_VERSION = 2
PACKAGE_MEDIA_TYPE = "application/vnd.vonk-forge.recipe-package.v2+tar+gzip"
PACKAGE_REPOSITORY = "CarstVaartjes/vonk-forge-recipes"
PACKAGE_API_ORIGIN = "https://api.github.com"
# Release assets download from github.com (not the rate-limited REST API),
# which redirects to this origin with a short-lived signed query. A deployment
# may route both through one fixed internal relay.
RELEASE_DOWNLOAD_ORIGIN = "https://github.com"
RELEASE_ASSET_HOST = "release-assets.githubusercontent.com"
RELEASE_ASSET_ORIGIN = f"https://{RELEASE_ASSET_HOST}"
MAX_RELEASE_BYTES = 2 * 1024 * 1024
MAX_RELEASE_LIST_BYTES = 16 * 1024 * 1024
MAX_INDEX_BYTES = 12 * 1024 * 1024
MAX_PACKAGE_BYTES = 256 * 1024 * 1024
MAX_PACKAGE_FILES = 2048
MAX_PACKAGE_FILE_BYTES = 128 * 1024 * 1024
MAX_PACKAGE_TOTAL_BYTES = 256 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_RELEASE_TAG = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_CONTRACT_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_REDIRECTS = {301, 302, 303, 307, 308}
_ASSET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PACKAGE_MEDIA_TYPES = {"application/octet-stream", PACKAGE_MEDIA_TYPE}


class _ReleaseAsset(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    name: str
    state: str


class _ReleaseResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")

    tag_name: str
    draft: bool
    assets: list[_ReleaseAsset]


_RELEASE_LIST = TypeAdapter(list[_ReleaseResponse])


def _select_release(
    releases: list[_ReleaseResponse], selector: str
) -> _ReleaseResponse | None:
    """Pick the newest published release within this Controller's contract major."""

    best: tuple[tuple[int, int], _ReleaseResponse] | None = None
    for release in releases:
        tag = _RELEASE_TAG.fullmatch(release.tag_name)
        if release.draft or tag is None or int(tag[1]) != CONTRACT_MAJOR:
            continue
        if selector != "latest" and release.tag_name != selector:
            continue
        key = (int(tag[2]), int(tag[3]))
        if best is None or key > best[0]:
            best = (key, release)
    return None if best is None else best[1]


@dataclass(frozen=True, slots=True)
class _VerifiedRelease:
    """A release whose SHA256SUMS verified against the pinned publisher."""

    tag: str
    commit: str
    assets: frozenset[str]
    checksums: Mapping[str, str]
    checksums_raw: bytes
    bundle_raw: bytes


class RecipePackageError(RecipeLibraryError):
    """The trusted package descriptor or package contents are invalid."""


@dataclass(frozen=True, slots=True)
class RecipePackageHandle:
    """Immutable package identity plus durable local archive/closure paths."""

    publication_commit: str
    source_commit: str
    package_sha256: str
    package_size: int
    package_path: str
    recipe_content_sha256: str
    archive_path: Path
    closure_path: Path
    recipe: RecipeDefinition
    # Model snapshots keyed by the document digest recipes reference.
    models: Mapping[str, ModelDefinition]

    @property
    def recipe_identity(self) -> tuple[str, str, str]:
        identity = self.recipe.identity
        return identity.publisher, identity.slug, self.recipe_content_sha256

    @property
    def model_identities(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(
            (model.identity.publisher, model.identity.slug, digest)
            for digest, model in self.models.items()
        )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_path(value: str) -> bool:
    if not value or "\0" in value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return (
        bool(path.parts)
        and value == path.as_posix()
        and not path.is_absolute()
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _json(raw: bytes) -> object:
    return json.loads(raw)


def _validate_package_paths(
    recipe: RecipeDefinition, package_paths: set[str], build_inputs: object
) -> None:
    """Validate source and fixture closure using BuildContext as a prefix."""
    build = recipe.execution.build
    context = build.context.path.rstrip("/")
    if not any(
        path == context or path.startswith(f"{context}/") for path in package_paths
    ):
        raise ValueError("build context is missing from package")
    required = {build.dockerfile, *(patch.path for patch in build.patches)}
    missing = sorted(required - package_paths)
    if missing:
        raise ValueError(f"build package files are missing: {', '.join(missing)}")
    if not isinstance(build_inputs, list) or not any(
        isinstance(value, Mapping)
        and value.get("kind") == "oci-image"
        and isinstance(value.get("reference"), str)
        and value["reference"].endswith(f"@sha256:{build.base_image.digest}")
        for value in build_inputs
    ):
        raise ValueError("build base image digest is not in package inputs")
    for check in recipe.validation.serving.checks:
        request = check.request
        fixture = getattr(request, "fixture", None)
        slots = getattr(request, "input_slots", {})
        if fixture is not None:
            required = {fixture, *slots.values()}
            missing = sorted(required - package_paths)
            if missing:
                raise ValueError(
                    f"job serving package files are missing: {', '.join(missing)}"
                )


class RecipePackageClient:
    """Fetch complete recipe packages and persist verified bytes by digest.

    The reader consumes signed GitHub releases of the recipe repository. The
    library's release version is its contract version, so ``latest`` follows
    the newest published release whose major version is this Controller's
    contract major (an exact tag pins one). A release is trusted only after its
    ``SHA256SUMS`` verifies against the pinned Sigstore publisher identity, and
    every index and package byte is then checked against the digest
    ``SHA256SUMS`` lists.
    """

    def __init__(
        self,
        *,
        cache_root: Path,
        transport: httpx2.BaseTransport | None = None,
        timeout_seconds: float = 8.0,
        api_url: str = PACKAGE_API_ORIGIN,
        asset_url: str | None = None,
        release: str = "latest",
    ) -> None:
        origin = (asset_url or RELEASE_DOWNLOAD_ORIGIN).rstrip("/")
        parsed = urlsplit(origin)
        if not parsed.hostname or (
            parsed.scheme != "https"
            and parsed.hostname not in {"localhost", "127.0.0.1", "::1", "caddy"}
        ):
            raise RecipePackageError(
                "recipe_package.url_insecure", "recipe package URL must use HTTPS"
            )
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise RecipePackageError(
                "recipe_package.url_invalid",
                "recipe package URL contains forbidden components",
            )
        api = urlsplit(api_url.rstrip("/"))
        if (
            not api.hostname
            or (
                api.scheme != "https"
                and api.hostname not in {"localhost", "127.0.0.1", "::1", "caddy"}
            )
            or api.username
            or api.password
            or api.query
            or api.fragment
            or api.path not in {"", "/"}
        ):
            raise RecipePackageError(
                "recipe_package.url_invalid", "recipe package API URL is invalid"
            )
        tag = _RELEASE_TAG.fullmatch(release)
        if release != "latest" and (tag is None or int(tag[1]) != CONTRACT_MAJOR):
            raise RecipePackageError(
                "recipe_package.release_invalid",
                "recipe release must be latest or an exact "
                f"v{CONTRACT_MAJOR}.MINOR.PATCH tag",
            )
        self._api_url = api_url.rstrip("/")
        self._download_origin = origin
        self._redirect_origin = origin if asset_url else RELEASE_ASSET_ORIGIN
        self._release_selector = release
        self._release: _VerifiedRelease | None = None
        self._cache_root = cache_root.resolve()
        self._cache_root.mkdir(parents=True, exist_ok=True)
        self._client = httpx2.Client(
            base_url=self._download_origin,
            timeout=httpx2.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={"Accept": "application/json, text/plain"},
        )
        self._snapshot: RecipeLibrarySnapshot | None = None
        self._previous_snapshot: RecipeLibrarySnapshot | None = None
        self._previous_packages: dict[str, dict[str, object]] = {}
        self._packages: dict[str, dict[str, object]] = {}
        self._prepared: dict[str, RecipeLibraryItem] = {}
        self._snapshot_path = self._cache_root / "snapshot.json"
        self._candidate_path = self._cache_root / "snapshot.candidate.json"
        self._candidate_active = False

    def close(self) -> None:
        self._client.close()

    def list(self) -> RecipeLibrarySnapshot:
        try:
            raw, publication, release = self._fetch_release()
        except (httpx2.HTTPError, OSError) as error:
            persisted = self._read_persisted_snapshot()
            if persisted is not None:
                return persisted
            raise RecipePackageError(
                "recipe_package.unavailable", "recipe package index is unavailable"
            ) from error
        except RecipePackageError as error:
            # Only an unreachable publication falls back to the previous
            # verified generation; an integrity failure is always surfaced.
            if error.code == "recipe_package.unavailable":
                persisted = self._read_persisted_snapshot()
                if persisted is not None:
                    return persisted
            raise
        snapshot, packages = self._parse_index(raw, publication_commit=publication)
        _bind_release(snapshot, packages, release)
        self._persist_index(raw, publication_commit=publication, release=release)
        self._candidate_active = True
        self._release = release
        self._packages = packages
        self._snapshot = snapshot
        self._prepared = {}
        return snapshot

    def _fetch_release(self) -> tuple[bytes, str, _VerifiedRelease]:
        tag, assets = self._resolve_release()
        checksums_raw = self._download_asset(
            tag, assets, RELEASE_CHECKSUMS, MAX_CHECKSUMS_BYTES
        )
        bundle_raw = self._download_asset(tag, assets, RELEASE_BUNDLE, MAX_BUNDLE_BYTES)
        commit = verify_release_checksums(checksums_raw, bundle_raw)
        checksums = parse_release_checksums(checksums_raw)
        raw = self._download_asset(
            tag,
            assets,
            RELEASE_INDEX,
            MAX_INDEX_BYTES,
            sha256=checksums[RELEASE_INDEX],
        )
        return (
            raw,
            commit,
            _VerifiedRelease(
                tag=tag,
                commit=commit,
                assets=assets,
                checksums=checksums,
                checksums_raw=checksums_raw,
                bundle_raw=bundle_raw,
            ),
        )

    def _resolve_release(self) -> tuple[str, frozenset[str]]:
        if self._release_selector == "latest":
            path, maximum = "releases?per_page=100", MAX_RELEASE_LIST_BYTES
        else:
            path = f"releases/tags/{self._release_selector}"
            maximum = MAX_RELEASE_BYTES
        response = self._client.get(
            f"{self._api_url}/repos/{PACKAGE_REPOSITORY}/{path}",
            headers={"Accept": "application/vnd.github+json"},
        )
        if response.status_code != 200 or response.is_redirect:
            raise RecipePackageError(
                "recipe_package.unavailable", "recipe release is unavailable"
            )
        if len(response.content) > maximum:
            raise RecipePackageError(
                "recipe_package.response_invalid", "recipe release response is invalid"
            )
        try:
            payload = _json(response.content)
            releases = (
                _RELEASE_LIST.validate_python(payload)
                if self._release_selector == "latest"
                else [_ReleaseResponse.model_validate(payload)]
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
            raise RecipePackageError(
                "recipe_package.response_invalid", "recipe release response is invalid"
            ) from error
        release = _select_release(releases, self._release_selector)
        if release is None:
            raise RecipePackageError(
                "recipe_package.unavailable",
                f"no published recipe library release for contract v{CONTRACT_MAJOR}",
            )
        assets: set[str] = set()
        for asset in release.assets:
            if asset.state != "uploaded":
                continue
            if asset.name in assets or not _ASSET_NAME.fullmatch(asset.name):
                raise RecipePackageError(
                    "recipe_package.response_invalid",
                    "recipe release asset identity is invalid",
                )
            assets.add(asset.name)
        return release.tag_name, frozenset(assets)

    def _download_asset(
        self,
        tag: str,
        assets: frozenset[str],
        name: str,
        maximum_bytes: int,
        *,
        sha256: str | None = None,
    ) -> bytes:
        if name not in assets:
            raise RecipePackageError(
                "recipe_package.release_incomplete",
                f"recipe release does not contain {name}",
            )
        response = self._client.get(
            f"{self._download_origin}/{PACKAGE_REPOSITORY}/releases/download/{tag}/{name}",
            headers={"Accept": "application/octet-stream"},
        )
        if response.status_code in _REDIRECTS:
            target = urlsplit(response.headers.get("location", ""))
            if (
                target.scheme != "https"
                or target.hostname != RELEASE_ASSET_HOST
                or target.port is not None
                or target.username
                or target.password
                or target.fragment
                or not target.path.startswith("/")
            ):
                raise RecipePackageError(
                    "recipe_package.response_invalid",
                    "recipe release asset redirect leaves the GitHub asset origin",
                )
            # The signed query is opaque; the relay forwards it unchanged.
            query = f"?{target.query}" if target.query else ""
            response = self._client.get(f"{self._redirect_origin}{target.path}{query}")
        if response.status_code != 200 or response.is_redirect:
            raise RecipePackageError(
                "recipe_package.unavailable",
                f"recipe release asset {name} is unavailable",
            )
        content = response.content
        if len(content) > maximum_bytes:
            raise RecipePackageError(
                "recipe_package.response_invalid",
                f"recipe release asset {name} exceeds its size bound",
            )
        if sha256 is not None and _sha256(content) != sha256:
            raise RecipePackageError(
                "recipe_package.digest_mismatch",
                f"recipe release asset {name} does not match SHA256SUMS",
            )
        return content

    def _parse_index(
        self, raw: bytes, *, publication_commit: str
    ) -> tuple[RecipeLibrarySnapshot, dict[str, dict[str, object]]]:
        try:
            index = _json(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RecipePackageError(
                "recipe_package.response_invalid",
                "recipe package index is invalid JSON",
            ) from error
        if (
            not isinstance(index, Mapping)
            or index.get("schema_version") != PACKAGE_SCHEMA_VERSION
            or index.get("kind") != "recipe-library-index"
        ):
            raise RecipePackageError(
                "recipe_package.schema_incompatible",
                "recipe package index schema is unsupported",
            )
        repository, commit, raw_recipes = (
            index.get("repository"),
            index.get("source_commit"),
            index.get("recipes"),
        )
        raw_entities = index.get("catalog_entities")
        contract = index.get("package_contract")
        version, updated_at = _library_release(index)
        if (
            repository != PACKAGE_REPOSITORY
            or not isinstance(commit, str)
            or not _SHA1.fullmatch(commit)
            or not isinstance(contract, Mapping)
            or contract.get("schema_version") != PACKAGE_SCHEMA_VERSION
            or contract.get("media_type") != PACKAGE_MEDIA_TYPE
            or not isinstance(raw_recipes, list)
            or not isinstance(raw_entities, list)
        ):
            raise RecipePackageError(
                "recipe_package.response_invalid",
                "recipe package index identity is invalid",
            )
        catalog_entities: list[dict[str, object]] = []
        identities: set[tuple[str, str]] = set()
        # Each model and recipe document is validated on its own: one document
        # this Controller's contract cannot read (for example from a newer
        # release) is skipped and reported, and the rest of the signed index
        # still applies. Only the index envelope itself is all-or-nothing.
        problems: list[dict[str, object]] = []
        for entry in raw_entities:
            if not isinstance(entry, Mapping) or not isinstance(
                entry.get("document"), Mapping
            ):
                problems.append(
                    _index_problem(None, "catalog model entry is invalid", entry)
                )
                continue
            try:
                model = read_model(entry["document"])
            except (TypeError, ValueError) as error:
                problems.append(
                    _index_problem(
                        None, "catalog model document is invalid", entry, error
                    )
                )
                continue
            digest = entry.get("content_sha256")
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                problems.append(
                    _index_problem(None, "catalog model digest is invalid", entry)
                )
                continue
            identity = (model.identity.publisher, model.identity.slug)
            if identity in identities:
                raise RecipePackageError(
                    "recipe_package.response_invalid",
                    "catalog model identity is duplicated",
                )
            identities.add(identity)
            catalog_entities.append(dict(entry["document"]))
        raw_packages: list[Mapping[str, object]] = []
        for recipe in raw_recipes:
            if (
                not isinstance(recipe, Mapping)
                or not isinstance(recipe.get("document"), Mapping)
                or not isinstance(recipe.get("package"), Mapping)
            ):
                problems.append(
                    _index_problem(None, "recipe package entry is invalid", recipe)
                )
                continue
            try:
                document = read_recipe(recipe["document"])
            except (TypeError, ValueError) as error:
                problems.append(
                    _index_problem(
                        _entry_uri(recipe),
                        "recipe package recipe document is invalid",
                        recipe,
                        error,
                    )
                )
                continue
            package = recipe["package"]
            package_media_type = package.get("media_type")
            if package_media_type not in _PACKAGE_MEDIA_TYPES:
                problems.append(
                    _index_problem(
                        _entry_uri(recipe),
                        "recipe package media type is unsupported",
                        recipe,
                    )
                )
                continue
            if package.get("recipe_content_sha256") not in {
                None,
                recipe.get("content_sha256"),
            }:
                problems.append(
                    _index_problem(
                        _entry_uri(recipe),
                        "package recipe identity is inconsistent",
                        recipe,
                    )
                )
                continue
            raw_packages.append(
                {
                    "publisher": document.identity.publisher,
                    "slug": document.identity.slug,
                    "source_path": recipe.get("source_path"),
                    "recipe_content_sha256": recipe.get("content_sha256"),
                    "package_sha256": package.get("sha256"),
                    "size": package.get("expected_bytes"),
                    "location": package.get("path"),
                    "title": document.metadata.title,
                    "description": document.metadata.description,
                    "tags": document.metadata.tags,
                    "document": dict(recipe["document"]),
                }
            )
        packages: dict[str, dict[str, object]] = {}
        items: list[RecipeLibraryItem] = []
        for package_entry in raw_packages:
            if not isinstance(package_entry, Mapping):
                raise RecipePackageError(
                    "recipe_package.response_invalid", "recipe package entry is invalid"
                )
            publisher, slug, digest = (
                package_entry.get("publisher"),
                package_entry.get("slug"),
                package_entry.get("recipe_content_sha256"),
            )
            package_digest, location, size, source_path = (
                package_entry.get("package_sha256"),
                package_entry.get("location"),
                package_entry.get("size"),
                package_entry.get("source_path"),
            )
            document = package_entry.get("document")
            location_url = urlsplit(str(location))
            if (
                not isinstance(document, Mapping)
                or not all(
                    isinstance(value, str)
                    for value in (
                        publisher,
                        slug,
                        digest,
                        package_digest,
                        location,
                        source_path,
                    )
                )
                or not _safe_path(str(source_path))
                or not _SLUG.fullmatch(str(publisher))
                or not _SLUG.fullmatch(str(slug))
                or not _SHA256.fullmatch(str(digest))
                or not _SHA256.fullmatch(str(package_digest))
                or not _safe_path(str(location))
                or str(location).startswith("/")
                or location_url.scheme
                or location_url.netloc
                or location_url.query
                or location_url.fragment
                or not isinstance(size, int)
                or isinstance(size, bool)
                or not 1 <= size <= MAX_PACKAGE_BYTES
            ):
                raise RecipePackageError(
                    "recipe_package.response_invalid",
                    "recipe package entry identity is invalid",
                )
            key = f"{publisher}/{slug}"
            if key in packages:
                raise RecipePackageError(
                    "recipe_package.response_invalid",
                    "recipe package identity is duplicated",
                )
            packages[key] = dict(package_entry)
            packages[key]["publication_commit"] = publication_commit
            tags = package_entry.get("tags", [])
            items.append(
                RecipeLibraryItem(
                    library_commit=commit,
                    source_path=str(source_path),
                    publisher=str(publisher),
                    slug=str(slug),
                    title=str(package_entry.get("title", "")),
                    description=str(package_entry.get("description", "")),
                    tags=tuple(str(tag) for tag in tags)
                    if isinstance(tags, list)
                    else (),
                    content_sha256=str(digest),
                    uri=f"vonk://catalog/{publisher}/{slug}@sha256:{digest}",
                    document=dict(document),
                )
            )
        if [(item.publisher, item.slug) for item in items] != sorted(
            (item.publisher, item.slug) for item in items
        ):
            raise RecipePackageError(
                "recipe_package.response_invalid", "recipe package index is not sorted"
            )
        return RecipeLibrarySnapshot(
            commit=commit,
            items=tuple(items),
            repository=repository,
            version=version,
            updated_at=updated_at,
            catalog_entities=tuple(catalog_entities),
            problems=tuple(problems),
        ), packages

    def _persist_index(
        self,
        raw: bytes,
        *,
        publication_commit: str,
        release: _VerifiedRelease,
    ) -> None:
        # Keep the signature material so a restart re-verifies the previous
        # generation instead of trusting local state.
        payload: dict[str, object] = {
            "index": raw.decode("utf-8"),
            "publication_commit": publication_commit,
            "release": {
                "tag": release.tag,
                "assets": sorted(release.assets),
                "checksums": release.checksums_raw.decode("ascii"),
                "bundle": release.bundle_raw.decode("utf-8"),
            },
        }
        temporary = self._candidate_path.with_suffix(".tmp")
        try:
            temporary.write_text(
                json.dumps(payload, separators=(",", ":")), encoding="utf-8"
            )
            os.replace(temporary, self._candidate_path)
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass

    def _promote_candidate(self) -> None:
        if not self._candidate_active:
            return
        try:
            os.replace(self._candidate_path, self._snapshot_path)
        except OSError as error:
            raise RecipePackageError(
                "recipe_package.cache_unavailable",
                "recipe package snapshot could not be committed",
            ) from error
        # The candidate becomes the only previous-good generation after every
        # package has been fetched and decoded.  A second list() in the same
        # process must still compare against this generation, rather than the
        # unvalidated candidate it replaced.
        self._previous_snapshot = self._snapshot
        self._previous_packages = {
            key: dict(value) for key, value in self._packages.items()
        }
        self._candidate_active = False

    def _read_persisted_snapshot(self) -> RecipeLibrarySnapshot | None:
        """Read the offline index cache, or ``None`` when it is unusable.

        This file is only a re-fetchable cache of the remote authoritative
        index. A missing or corrupt cache is deliberately discarded so the
        caller fails closed with "index is unavailable"; it is never served as
        fresh data, so this is not the corruption-becomes-absent case.
        """

        self._candidate_active = False
        try:
            # A candidate left by an interrupted process is never eligible for
            # promotion by an offline prepare.
            self._candidate_path.unlink()
        except OSError:
            pass
        try:
            payload = _json(self._snapshot_path.read_bytes())
            if not isinstance(payload, Mapping) or not isinstance(
                payload.get("index"), str
            ):
                return None
            publication = payload.get("publication_commit")
            raw = payload["index"].encode("utf-8")
            release = _persisted_release(payload.get("release"), raw)
            if (
                release is None
                or not isinstance(publication, str)
                or release.commit != publication
            ):
                # A generation without verifiable release signatures (for
                # example one cached by an older reader) is never served.
                return None
            snapshot, packages = self._parse_index(raw, publication_commit=publication)
            _bind_release(snapshot, packages, release)
        except (
            OSError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecipeLibraryError,
        ):
            return None
        self._release = release
        self._packages = packages
        self._snapshot = snapshot
        self._previous_snapshot = snapshot
        self._previous_packages = {key: dict(value) for key, value in packages.items()}
        return snapshot

    def prepare(self, snapshot: RecipeLibrarySnapshot) -> None:
        if self._snapshot is None or self._snapshot.commit != snapshot.commit:
            raise RecipePackageError(
                "recipe_package.snapshot_changed",
                "package index changed during preparation",
            )
        previous = (
            {item.uri: item for item in self._previous_snapshot.items}
            if self._previous_snapshot
            else {}
        )
        prepared: dict[str, RecipeLibraryItem] = {}
        for item in snapshot.items:
            if (
                item.uri in previous
                and previous[item.uri].content_sha256 == item.content_sha256
                and self._same_package(item)
            ):
                continue
            try:
                prepared[item.uri] = self.fetch(item.uri)
            except RecipeLibraryError as error:
                # Integrity and transport failures fail the whole candidate
                # generation so the previous verified one stays active. A
                # package whose documents this Controller's contract cannot
                # read belongs to that recipe alone: the sync fetches it again
                # and reports it as that recipe's problem.
                if error.code != "recipe_package.document_incompatible":
                    raise
        self._prepared = prepared
        self._promote_candidate()

    def _same_package(self, item: RecipeLibraryItem) -> bool:
        """Return whether the active generation points at the same bytes."""
        current = self._packages.get(f"{item.publisher}/{item.slug}")
        previous = self._previous_packages.get(f"{item.publisher}/{item.slug}")
        if current is None or previous is None:
            return False
        return all(
            current.get(field) == previous.get(field)
            for field in ("package_sha256", "size", "location", "recipe_content_sha256")
        )

    def fetch(self, uri: str) -> RecipeLibraryItem:
        match = re.fullmatch(
            r"vonk://catalog/([a-z0-9][a-z0-9-]{1,62})/([a-z0-9][a-z0-9-]{1,62})@sha256:([0-9a-f]{64})",
            uri,
        )
        if match is None:
            raise RecipePackageError(
                "recipe_package.uri_invalid", "recipe URI is invalid"
            )
        snapshot = self._snapshot or self.list()
        publisher, slug, digest = match.groups()
        item = next(
            (
                candidate
                for candidate in snapshot.items
                if candidate.publisher == publisher and candidate.slug == slug
            ),
            None,
        )
        if item is None or item.content_sha256 != digest:
            raise RecipePackageError(
                "recipe_package.not_found", "recipe is not in the current package index"
            )
        if uri in self._prepared:
            return self._prepared[uri]
        package = self._packages[f"{publisher}/{slug}"]
        package_digest = str(package["package_sha256"])
        archive, archive_path = self._cached_or_download(
            package_digest,
            str(package["location"]),
            require_integer(package["size"], "package size"),
        )
        return self._decode_package(
            archive, item, package=package, archive_path=archive_path
        )

    def _cached_or_download(
        self, digest: str, location: str, expected_size: int
    ) -> tuple[bytes, Path]:
        target = self._cache_root / digest[:2] / f"{digest}.tar.gz"
        try:
            cached = target.read_bytes()
            if len(cached) == expected_size and _sha256(cached) == digest:
                return cached, target
        except OSError:
            pass
        content = self._download_release_package(digest, location, expected_size)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{digest}.", suffix=".tmp", dir=target.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        return content, target

    def _download_release_package(
        self, digest: str, location: str, expected_size: int
    ) -> bytes:
        release = self._release
        name = PurePosixPath(location).name
        if release is None or release.checksums.get(name) != digest:
            raise RecipePackageError(
                "recipe_package.snapshot_changed",
                "recipe package is not in the verified release",
            )
        try:
            content = self._download_asset(
                release.tag, release.assets, name, MAX_PACKAGE_BYTES, sha256=digest
            )
        except (httpx2.HTTPError, OSError) as error:
            raise RecipePackageError(
                "recipe_package.unavailable", "recipe package is unavailable"
            ) from error
        if len(content) != expected_size:
            raise RecipePackageError(
                "recipe_package.digest_mismatch",
                "recipe package bytes do not match the trusted index",
            )
        return content

    def _decode_package(
        self,
        archive: bytes,
        item: RecipeLibraryItem,
        *,
        package: Mapping[str, object] | None = None,
        archive_path: Path | None = None,
    ) -> RecipeLibraryItem:
        files: dict[str, bytes] = {}
        total = 0
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tar:
                members = tar.getmembers()
                if len(members) > MAX_PACKAGE_FILES:
                    raise ValueError("too many package files")
                for member in members:
                    if (
                        not _safe_path(member.name)
                        or not member.isfile()
                        or member.name in files
                        or member.size < 0
                        or member.size > MAX_PACKAGE_FILE_BYTES
                    ):
                        raise ValueError("unsafe package member")
                    stream = tar.extractfile(member)
                    if stream is None:
                        raise ValueError("package member is unreadable")
                    content = stream.read(member.size + 1)
                    if len(content) != member.size:
                        raise ValueError("package member size mismatch")
                    total += len(content)
                    if total > MAX_PACKAGE_TOTAL_BYTES:
                        raise ValueError("package is too large")
                    files[member.name] = content
        except (OSError, tarfile.TarError, ValueError) as error:
            raise RecipePackageError(
                "recipe_package.extract_invalid", "recipe package extraction failed"
            ) from error
        try:
            manifest = _json(files["manifest.json"])
            if (
                not isinstance(manifest, Mapping)
                or manifest.get("schema_version") != PACKAGE_SCHEMA_VERSION
                or manifest.get("kind") != "recipe-package"
                or manifest.get("package_type") != "recipe"
                or manifest.get("recipe_content_sha256") != item.content_sha256
            ):
                raise ValueError("manifest identity is invalid")
            entries = manifest.get("files")
            if (
                not isinstance(entries, list)
                or len(entries) != len(files) - 1
                or {
                    entry.get("path") for entry in entries if isinstance(entry, Mapping)
                }
                != set(files) - {"manifest.json"}
            ):
                raise ValueError("manifest file inventory is invalid")
            for entry in entries:
                if (
                    not isinstance(entry, Mapping)
                    or not isinstance(entry.get("path"), str)
                    or not _SHA256.fullmatch(str(entry.get("sha256")))
                    or entry.get("size") != len(files[entry["path"]])
                    or _sha256(files[entry["path"]]) != entry["sha256"]
                ):
                    raise ValueError("manifest file digest is invalid")
            if [
                path for path in files if PurePosixPath(path).name == "recipe.json"
            ] != ["recipe.json"]:
                raise ValueError(
                    "package must contain exactly one recipe.json entrypoint"
                )
            recipe_document = _json(files["recipe.json"])
            if not isinstance(recipe_document, dict):
                raise TypeError("recipe.json is not a JSON object")
            recipe = read_recipe(recipe_document)
            if (
                document_sha256(recipe_document) != item.content_sha256
                or recipe.identity.publisher != item.publisher
                or recipe.identity.slug != item.slug
            ):
                raise ValueError("recipe identity or digest is invalid")
            model_paths = [
                path
                for path in files
                if path.startswith("models/") and path.endswith(".json")
            ]
            if not model_paths or any(
                path != f"models/{Path(path).stem}.json" for path in model_paths
            ):
                raise ValueError("model snapshot paths are invalid")
            model_documents: list[dict[str, object]] = []
            for path in model_paths:
                model_document = _json(files[path])
                if not isinstance(model_document, dict):
                    raise TypeError("model snapshot is not a JSON object")
                model_documents.append(model_document)
            models = {
                document_sha256(value): read_model(value) for value in model_documents
            }
            if {
                f"models/{model.identity.slug}.json" for model in models.values()
            } != set(model_paths):
                raise ValueError("model snapshot identity does not match its path")
            validate_recipe_models(recipe, models)
            _validate_package_paths(
                recipe, set(files) - {"manifest.json"}, manifest.get("build_inputs")
            )
        except ValidationError as error:
            # The package bytes are the signed ones, but a document in it does
            # not fit this Controller's contract: that recipe alone is skipped.
            raise RecipePackageError(
                "recipe_package.document_incompatible",
                _incompatible_detail(
                    f"recipe package document is incompatible {item.publisher}/"
                    f"{item.slug}",
                    error,
                ),
            ) from error
        except (
            KeyError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as error:
            raise RecipePackageError(
                "recipe_package.package_invalid",
                "recipe package identity or contents are invalid",
            ) from error
        metadata = recipe.metadata
        package_digest = (
            str(package.get("package_sha256"))
            if package is not None
            else _sha256(archive)
        )
        package_size = len(archive)
        if package is not None:
            declared_size = integer(package.get("size"), default=package_size)
            if declared_size is not None:
                package_size = declared_size
        package_path = str(package.get("location", "")) if package is not None else ""
        publication_commit = (
            str(package.get("publication_commit", item.library_commit))
            if package is not None
            else item.library_commit
        )
        if archive_path is None:
            archive_path = (
                Path(getattr(self, "_archive_path", ""))
                if getattr(self, "_archive_path", None)
                else Path(".")
            )
        closure_path = self._materialize_closure(files, package_digest, archive_path)
        context = recipe.execution.build.context.path.rstrip("/")
        context_files = {
            path.removeprefix(f"{context}/"): content
            for path, content in files.items()
            if path.startswith(f"{context}/")
        }
        try:
            bundle = generate_source_bundle(context_files)
        except (SourceBundleError, ValueError) as error:
            raise RecipePackageError(
                "recipe_package.package_invalid",
                "recipe build source closure is invalid",
            ) from error
        source_bundle, source_bundle_sha256 = bundle.archive, bundle.sha256
        handle = RecipePackageHandle(
            publication_commit=publication_commit,
            source_commit=item.library_commit,
            package_sha256=package_digest,
            package_size=package_size,
            package_path=package_path,
            recipe_content_sha256=item.content_sha256,
            archive_path=archive_path,
            closure_path=closure_path,
            recipe=recipe,
            models=models,
        )
        return replace(
            item,
            title=metadata.title,
            description=metadata.description,
            tags=tuple(metadata.tags),
            document=recipe_document,
            release=RecipeLibraryRelease(
                recipe.release.version, recipe.release.released_at
            ),
            dependencies=tuple(model_documents),
            source_bundle=source_bundle,
            package_handle=handle,
            package_sha256=package_digest,
            source_bundle_sha256=source_bundle_sha256,
        )

    def _materialize_closure(
        self, files: Mapping[str, bytes], digest: str, archive_path: Path
    ) -> Path:
        root = getattr(self, "_cache_root", archive_path.parent)
        # Keep the closure beside its digest-addressed package object.  The
        # directory itself is the durable reference persisted in the import
        # receipt (``<cache>/<digest>/closure``).
        target = root / digest / "closure"
        marker = target / ".complete"
        if marker.is_file():
            try:
                if marker.read_text(encoding="ascii") == digest:
                    return target
            except (OSError, UnicodeDecodeError):
                pass
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            # A process interrupted before publishing the completion marker.
            # It is never exposed as a usable closure.
            shutil.rmtree(target, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{digest}.", dir=target.parent))
        try:
            for name, content in files.items():
                destination = temporary / Path(*PurePosixPath(name).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            marker_path = temporary / ".complete"
            marker_path.write_text(digest, encoding="ascii")
            try:
                os.replace(temporary, target)
            except FileExistsError:
                pass
        finally:
            if temporary.exists():
                for path in sorted(temporary.rglob("*"), reverse=True):
                    if path.is_file() or path.is_symlink():
                        path.unlink()
                    elif path.is_dir():
                        path.rmdir()
                temporary.rmdir()
        return target


def _library_release(index: Mapping[str, object]) -> tuple[str, datetime]:
    """Read the library version and last recipe update from the index."""

    version, updated = index.get("contract_version"), index.get("updated_at")
    match = _CONTRACT_VERSION.fullmatch(version) if isinstance(version, str) else None
    if match is None or int(match[1]) != CONTRACT_MAJOR:
        raise RecipePackageError(
            "recipe_package.schema_incompatible",
            f"recipe library index is not a contract v{CONTRACT_MAJOR} release",
        )
    try:
        parsed = datetime.fromisoformat(str(updated))
    except ValueError as error:
        raise RecipePackageError(
            "recipe_package.response_invalid",
            "recipe library index update time is invalid",
        ) from error
    if parsed.tzinfo is None:
        raise RecipePackageError(
            "recipe_package.response_invalid",
            "recipe library index update time is invalid",
        )
    return str(version), parsed.astimezone(UTC)


def _entry_uri(entry: Mapping[str, object]) -> str | None:
    """Return the catalog URI an index entry claims, when it is well formed."""
    document = entry.get("document")
    identity = document.get("identity") if isinstance(document, Mapping) else None
    digest = entry.get("content_sha256")
    if not isinstance(identity, Mapping) or not isinstance(digest, str):
        return None
    publisher, slug = identity.get("publisher"), identity.get("slug")
    if (
        not isinstance(publisher, str)
        or not isinstance(slug, str)
        or not _SLUG.fullmatch(publisher)
        or not _SLUG.fullmatch(slug)
        or not _SHA256.fullmatch(digest)
    ):
        return None
    return f"vonk://catalog/{publisher}/{slug}@sha256:{digest}"


def _index_problem(
    uri: str | None,
    detail: str,
    entry: object,
    error: Exception | None = None,
) -> dict[str, object]:
    """Describe one skipped index document without echoing untrusted values."""
    name = ""
    if isinstance(entry, Mapping):
        document = entry.get("document")
        identity = document.get("identity") if isinstance(document, Mapping) else None
        if isinstance(identity, Mapping):
            publisher, slug = identity.get("publisher"), identity.get("slug")
            if (
                isinstance(publisher, str)
                and isinstance(slug, str)
                and _SLUG.fullmatch(publisher)
                and _SLUG.fullmatch(slug)
            ):
                name = f" {publisher}/{slug}"
    return {
        "recipe_uri": uri,
        "code": "recipe_package.document_incompatible"
        if error is not None
        else "recipe_package.response_invalid",
        "detail": _incompatible_detail(f"{detail}{name}", error),
    }


def _incompatible_detail(detail: str, error: Exception | None) -> str:
    """Name the document fields a contract mismatch concerns, bounded."""
    if not isinstance(error, ValidationError):
        return detail[:256]
    locations = sorted(
        {".".join(str(part) for part in item["loc"]) for item in error.errors()}
    )
    return f"{detail}: {', '.join(locations)}"[:256] if locations else detail[:256]


def _bind_release(
    snapshot: RecipeLibrarySnapshot,
    packages: Mapping[str, Mapping[str, object]],
    release: _VerifiedRelease,
) -> None:
    """Require the index and every package it names to be the signed ones."""
    if snapshot.commit != release.commit:
        raise RecipePackageError(
            "recipe_package.response_invalid",
            "recipe index was not built from the signed release commit",
        )
    for package in packages.values():
        location = str(package.get("location"))
        name = PurePosixPath(location).name
        if (
            location != f"packages/{name}"
            or name not in release.assets
            or release.checksums.get(name) != package.get("package_sha256")
        ):
            raise RecipePackageError(
                "recipe_package.response_invalid",
                "recipe package is not in the signed release",
            )


def _persisted_release(value: object, index: bytes) -> _VerifiedRelease | None:
    if not isinstance(value, Mapping):
        return None
    tag, assets = value.get("tag"), value.get("assets")
    checksums_text, bundle_text = value.get("checksums"), value.get("bundle")
    if (
        not isinstance(tag, str)
        or not _RELEASE_TAG.fullmatch(tag)
        or not isinstance(assets, list)
        or not all(
            isinstance(name, str) and _ASSET_NAME.fullmatch(name) for name in assets
        )
        or not isinstance(checksums_text, str)
        or not isinstance(bundle_text, str)
    ):
        return None
    checksums_raw = checksums_text.encode("ascii")
    bundle_raw = bundle_text.encode("utf-8")
    commit = verify_release_checksums(checksums_raw, bundle_raw)
    checksums = parse_release_checksums(checksums_raw)
    if _sha256(index) != checksums[RELEASE_INDEX]:
        return None
    return _VerifiedRelease(
        tag=tag,
        commit=commit,
        assets=frozenset(assets),
        checksums=checksums,
        checksums_raw=checksums_raw,
        bundle_raw=bundle_raw,
    )


def load_recipe_package(
    path: Path,
    *,
    package_sha256: str,
    publisher: str,
    slug: str,
    recipe_content_sha256: str,
    library_commit: str,
    source_path: str,
) -> RecipeLibraryItem:
    try:
        archive = path.read_bytes()
    except OSError as error:
        raise RecipePackageError(
            "recipe_package.unavailable", "offline recipe package is unavailable"
        ) from error
    if (
        not _SHA256.fullmatch(package_sha256)
        or len(archive) > MAX_PACKAGE_BYTES
        or _sha256(archive) != package_sha256
    ):
        raise RecipePackageError(
            "recipe_package.digest_mismatch",
            "offline recipe package digest does not match",
        )
    item = RecipeLibraryItem(
        library_commit=library_commit,
        source_path=source_path,
        publisher=publisher,
        slug=slug,
        title="",
        description="",
        tags=(),
        content_sha256=recipe_content_sha256,
        uri=f"vonk://catalog/{publisher}/{slug}@sha256:{recipe_content_sha256}",
        document={},
    )
    decoder = RecipePackageClient.__new__(RecipePackageClient)
    decoder._cache_root = path.parent / ".recipe-package-cache"
    return decoder._decode_package(
        archive,
        item,
        package={
            "package_sha256": package_sha256,
            "size": len(archive),
            "location": str(path),
        },
        archive_path=path,
    )


__all__ = [
    "PACKAGE_MEDIA_TYPE",
    "PACKAGE_REPOSITORY",
    "RecipePackageClient",
    "RecipePackageError",
    "RecipePackageHandle",
    "load_recipe_package",
]
