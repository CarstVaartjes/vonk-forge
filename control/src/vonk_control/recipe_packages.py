"""Schema-2 recipe package reader for the Controller catalog sync."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tarfile
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlsplit

import httpx
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256
from vonk_forge_contracts.resolver import (
    validate_model_references,
    validate_recipe_models,
)

from .recipe_library import (
    RecipeLibraryChange,
    RecipeLibraryError,
    RecipeLibraryItem,
    RecipeLibraryRelease,
    RecipeLibrarySnapshot,
)

PACKAGE_SCHEMA_VERSION = 2
PACKAGE_INDEX_PATH = "/v1/recipe-library/index.json"
PACKAGE_MEDIA_TYPE = "application/vnd.vonk-forge.recipe-package.v2+tar+gzip"
MAX_INDEX_BYTES = 12 * 1024 * 1024
MAX_PACKAGE_BYTES = 256 * 1024 * 1024
MAX_PACKAGE_FILES = 2048
MAX_PACKAGE_FILE_BYTES = 128 * 1024 * 1024
MAX_PACKAGE_TOTAL_BYTES = 256 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


class RecipePackageError(RecipeLibraryError):
    """The trusted package descriptor or package contents are invalid."""


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(path.parts) and not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _json(raw: bytes) -> object:
    return json.loads(raw)


def _release_history(recipe: RecipeDefinition, digest: str) -> tuple[RecipeLibraryRelease, ...]:
    result: list[RecipeLibraryRelease] = []
    for index, entry in enumerate(recipe.release.history):
        changes = tuple(
            RecipeLibraryChange(
                change.kind,
                change.summary,
                change.details,
                tuple(change.references),
            )
            for change in entry.changes
        )
        result.append(
            RecipeLibraryRelease(
                entry.version,
                entry.released_at,
                digest if index == 0 else (entry.prior_recipe_content_sha256 or digest),
                entry.upgrade_effect,
                changes,
            )
        )
    return tuple(result)


def _validate_package_paths(
    recipe: RecipeDefinition, package_paths: set[str], build_inputs: object
) -> None:
    """Validate source and fixture closure using BuildContext as a prefix."""
    if recipe.execution.mode == "build":
        build = recipe.execution.build
        context = build.context.path.rstrip("/")
        if not any(path == context or path.startswith(f"{context}/") for path in package_paths):
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
                raise ValueError(f"job serving package files are missing: {', '.join(missing)}")


class RecipePackageClient:
    """Fetch complete recipe packages and persist verified bytes by digest."""

    def __init__(self, base_url: str, *, cache_root: Path, transport: httpx.BaseTransport | None = None, timeout_seconds: float = 8.0) -> None:
        parsed = urlsplit(base_url.rstrip("/"))
        if not parsed.hostname or (parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1", "caddy"}):
            raise RecipePackageError("recipe_package.url_insecure", "recipe package URL must use HTTPS")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise RecipePackageError("recipe_package.url_invalid", "recipe package URL contains forbidden components")
        self._base_url = base_url.rstrip("/")
        self._cache_root = cache_root.resolve()
        self._cache_root.mkdir(parents=True, exist_ok=True)
        self._client = httpx.Client(base_url=self._base_url, timeout=httpx.Timeout(timeout_seconds), follow_redirects=False, trust_env=False, transport=transport, headers={"Accept": "application/json"})
        self._snapshot: RecipeLibrarySnapshot | None = None
        self._previous_snapshot: RecipeLibrarySnapshot | None = None
        self._packages: dict[str, dict[str, object]] = {}
        self._prepared: dict[str, RecipeLibraryItem] = {}

    def close(self) -> None:
        self._client.close()

    def list(self) -> RecipeLibrarySnapshot:
        try:
            response = self._client.get(PACKAGE_INDEX_PATH)
        except (httpx.HTTPError, OSError) as error:
            raise RecipePackageError("recipe_package.unavailable", "recipe package index is unavailable") from error
        if response.status_code != 200 or response.is_redirect:
            raise RecipePackageError("recipe_package.unavailable", "recipe package index is unavailable")
        if response.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json" or len(response.content) > MAX_INDEX_BYTES:
            raise RecipePackageError("recipe_package.response_invalid", "recipe package index response is invalid")
        try:
            index = _json(response.content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RecipePackageError("recipe_package.response_invalid", "recipe package index is invalid JSON") from error
        if not isinstance(index, Mapping) or index.get("schema_version") != PACKAGE_SCHEMA_VERSION or index.get("kind") != "recipe-library-index":
            raise RecipePackageError("recipe_package.schema_incompatible", "recipe package index schema is unsupported")
        repository, commit, raw_recipes = index.get("repository"), index.get("source_commit"), index.get("recipes")
        contract = index.get("package_contract")
        if not isinstance(repository, str) or not isinstance(commit, str) or not _SHA1.fullmatch(commit) or not isinstance(contract, Mapping) or contract.get("schema_version") != PACKAGE_SCHEMA_VERSION or contract.get("media_type") != PACKAGE_MEDIA_TYPE or not isinstance(raw_recipes, list) or len(raw_recipes) > 256:
            raise RecipePackageError("recipe_package.response_invalid", "recipe package index identity is invalid")
        raw_packages: list[Mapping[str, object]] = []
        for recipe in raw_recipes:
            if not isinstance(recipe, Mapping) or not isinstance(recipe.get("document"), Mapping) or not isinstance(recipe.get("package"), Mapping):
                raise RecipePackageError("recipe_package.response_invalid", "recipe package entry is invalid")
            try:
                document = RecipeDefinition.model_validate(recipe["document"])
            except (TypeError, ValueError) as error:
                raise RecipePackageError("recipe_package.response_invalid", "recipe package recipe document is invalid") from error
            package = recipe["package"]
            raw_packages.append({
                "publisher": document.identity.publisher, "slug": document.identity.slug,
                "source_path": recipe.get("source_path"), "recipe_content_sha256": recipe.get("content_sha256"),
                "package_sha256": package.get("sha256"), "size": package.get("expected_bytes"),
                "location": package.get("path"), "title": document.metadata.title,
                "description": document.metadata.description, "tags": document.metadata.tags,
            })
        packages: dict[str, dict[str, object]] = {}
        items: list[RecipeLibraryItem] = []
        for raw in raw_packages:
            if not isinstance(raw, Mapping):
                raise RecipePackageError("recipe_package.response_invalid", "recipe package entry is invalid")
            publisher, slug, digest = raw.get("publisher"), raw.get("slug"), raw.get("recipe_content_sha256")
            package_digest, location, size, source_path = raw.get("package_sha256"), raw.get("location"), raw.get("size"), raw.get("source_path")
            location_url = urlsplit(str(location))
            if not all(isinstance(value, str) for value in (publisher, slug, digest, package_digest, location, source_path)) or source_path != f"recipes/{slug}.json" or not _SLUG.fullmatch(str(publisher)) or not _SLUG.fullmatch(str(slug)) or not _SHA256.fullmatch(str(digest)) or not _SHA256.fullmatch(str(package_digest)) or not _safe_path(str(location)) or str(location).startswith("/") or location_url.scheme or location_url.netloc or not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= MAX_PACKAGE_BYTES:
                raise RecipePackageError("recipe_package.response_invalid", "recipe package entry identity is invalid")
            key = f"{publisher}/{slug}"
            if key in packages:
                raise RecipePackageError("recipe_package.response_invalid", "recipe package identity is duplicated")
            packages[key] = dict(raw)
            tags = raw.get("tags", [])
            items.append(RecipeLibraryItem(library_commit=commit, source_path=str(source_path), publisher=str(publisher), slug=str(slug), title=str(raw.get("title", "")), description=str(raw.get("description", "")), tags=tuple(str(tag) for tag in tags) if isinstance(tags, list) else (), content_sha256=str(digest), uri=f"vonk://catalog/{publisher}/{slug}@sha256:{digest}", document={}))
        if [(item.publisher, item.slug) for item in items] != sorted((item.publisher, item.slug) for item in items):
            raise RecipePackageError("recipe_package.response_invalid", "recipe package index is not sorted")
        self._previous_snapshot = self._snapshot
        self._packages = packages
        self._snapshot = RecipeLibrarySnapshot(commit=commit, items=tuple(items), repository=repository)
        self._prepared = {}
        return self._snapshot

    def prepare(self, snapshot: RecipeLibrarySnapshot) -> None:
        if self._snapshot is None or self._snapshot.commit != snapshot.commit:
            raise RecipePackageError("recipe_package.snapshot_changed", "package index changed during preparation")
        previous = {item.uri: item for item in self._previous_snapshot.items} if self._previous_snapshot else {}
        self._prepared = {
            item.uri: self.fetch(item.uri)
            for item in snapshot.items
            if item.uri not in previous or previous[item.uri].content_sha256 != item.content_sha256
        }

    def fetch(self, uri: str) -> RecipeLibraryItem:
        match = re.fullmatch(r"vonk://catalog/([a-z0-9][a-z0-9-]{1,62})/([a-z0-9][a-z0-9-]{1,62})@sha256:([0-9a-f]{64})", uri)
        if match is None:
            raise RecipePackageError("recipe_package.uri_invalid", "recipe URI is invalid")
        snapshot = self._snapshot or self.list()
        publisher, slug, digest = match.groups()
        item = next((candidate for candidate in snapshot.items if candidate.publisher == publisher and candidate.slug == slug), None)
        if item is None or item.content_sha256 != digest:
            raise RecipePackageError("recipe_package.not_found", "recipe is not in the current package index")
        if uri in self._prepared:
            return self._prepared[uri]
        package = self._packages[f"{publisher}/{slug}"]
        archive = self._cached_or_download(str(package["package_sha256"]), str(package["location"]), int(package["size"]))
        return self._decode_package(archive, item)

    def _cached_or_download(self, digest: str, location: str, expected_size: int) -> bytes:
        target = self._cache_root / digest[:2] / f"{digest}.tar.gz"
        try:
            cached = target.read_bytes()
            if len(cached) == expected_size and _sha256(cached) == digest:
                return cached
        except OSError:
            pass
        try:
            response = self._client.get(urljoin(self._base_url + "/", location))
        except (httpx.HTTPError, OSError) as error:
            raise RecipePackageError("recipe_package.unavailable", "recipe package is unavailable") from error
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
        if response.status_code != 200 or response.is_redirect or media_type != PACKAGE_MEDIA_TYPE or len(response.content) != expected_size or len(response.content) > MAX_PACKAGE_BYTES or _sha256(response.content) != digest:
            raise RecipePackageError("recipe_package.digest_mismatch", "recipe package bytes do not match the trusted index")
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{digest}.", suffix=".tmp", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(response.content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        return response.content

    def _decode_package(self, archive: bytes, item: RecipeLibraryItem) -> RecipeLibraryItem:
        files: dict[str, bytes] = {}
        total = 0
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tar:
                members = tar.getmembers()
                if len(members) > MAX_PACKAGE_FILES:
                    raise ValueError("too many package files")
                for member in members:
                    if not _safe_path(member.name) or not member.isfile() or member.name in files or member.size < 0 or member.size > MAX_PACKAGE_FILE_BYTES:
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
            raise RecipePackageError("recipe_package.extract_invalid", "recipe package extraction failed") from error
        try:
            manifest = _json(files["manifest.json"])
            if not isinstance(manifest, Mapping) or manifest.get("schema_version") != PACKAGE_SCHEMA_VERSION or manifest.get("kind") != "recipe-package" or manifest.get("package_type") != "recipe" or manifest.get("recipe_content_sha256") != item.content_sha256:
                raise ValueError("manifest identity is invalid")
            entries = manifest.get("files")
            if not isinstance(entries, list) or len(entries) != len(files) - 1 or {entry.get("path") for entry in entries if isinstance(entry, Mapping)} != set(files) - {"manifest.json"}:
                raise ValueError("manifest file inventory is invalid")
            for entry in entries:
                if not isinstance(entry, Mapping) or not isinstance(entry.get("path"), str) or not _SHA256.fullmatch(str(entry.get("sha256"))) or entry.get("size") != len(files[entry["path"]]) or _sha256(files[entry["path"]]) != entry["sha256"]:
                    raise ValueError("manifest file digest is invalid")
            if [path for path in files if PurePosixPath(path).name == "recipe.json"] != ["recipe.json"]:
                raise ValueError("package must contain exactly one recipe.json entrypoint")
            recipe = RecipeDefinition.model_validate(_json(files["recipe.json"]))
            if content_sha256(recipe) != item.content_sha256 or recipe.identity.publisher != item.publisher or recipe.identity.slug != item.slug:
                raise ValueError("recipe identity or digest is invalid")
            model_paths = [path for path in files if path.startswith("models/") and path.endswith(".json")]
            if not model_paths or any(path != f"models/{Path(path).stem}.json" for path in model_paths):
                raise ValueError("model snapshot paths are invalid")
            models = [ModelDefinition.model_validate(_json(files[path])) for path in model_paths]
            if {f"models/{model.identity.slug}.json" for model in models} != set(model_paths):
                raise ValueError("model snapshot identity does not match its path")
            validate_model_references(models)
            validate_recipe_models(recipe, models)
            _validate_package_paths(recipe, set(files) - {"manifest.json"}, manifest.get("build_inputs"))
            release_history = _release_history(recipe, item.content_sha256)
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RecipePackageError("recipe_package.package_invalid", "recipe package identity or contents are invalid") from error
        metadata = recipe.metadata
        return RecipeLibraryItem(library_commit=item.library_commit, source_path=item.source_path, publisher=item.publisher, slug=item.slug, title=metadata.title, description=metadata.description, tags=tuple(metadata.tags), content_sha256=item.content_sha256, uri=item.uri, document=recipe.model_dump(mode="json"), release_history=release_history, dependencies=tuple(model.model_dump(mode="json") for model in models))


def load_recipe_package(path: Path, *, package_sha256: str, publisher: str, slug: str, recipe_content_sha256: str, library_commit: str, source_path: str) -> RecipeLibraryItem:
    try:
        archive = path.read_bytes()
    except OSError as error:
        raise RecipePackageError("recipe_package.unavailable", "offline recipe package is unavailable") from error
    if len(archive) > MAX_PACKAGE_BYTES or _sha256(archive) != package_sha256:
        raise RecipePackageError("recipe_package.digest_mismatch", "offline recipe package digest does not match")
    item = RecipeLibraryItem(library_commit=library_commit, source_path=source_path, publisher=publisher, slug=slug, title="", description="", tags=(), content_sha256=recipe_content_sha256, uri=f"vonk://catalog/{publisher}/{slug}@sha256:{recipe_content_sha256}", document={})
    decoder = RecipePackageClient.__new__(RecipePackageClient)
    return decoder._decode_package(archive, item)


__all__ = ["PACKAGE_INDEX_PATH", "PACKAGE_MEDIA_TYPE", "RecipePackageClient", "RecipePackageError", "load_recipe_package"]
