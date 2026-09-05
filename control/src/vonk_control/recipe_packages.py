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
from .source_bundles import SourceBundleError, generate_source_bundle

PACKAGE_SCHEMA_VERSION = 2
PACKAGE_INDEX_PATH = "/v1/recipe-library/index.json"
PACKAGE_MEDIA_TYPE = "application/vnd.vonk-forge.recipe-package.v2+tar+gzip"
PACKAGE_REPOSITORY = "CarstVaartjes/vonk-forge-recipes"
PACKAGE_RAW_ORIGIN = "https://raw.githubusercontent.com"
PACKAGE_API_ORIGIN = "https://api.github.com"
MAX_INDEX_BYTES = 12 * 1024 * 1024
MAX_PACKAGE_BYTES = 256 * 1024 * 1024
MAX_PACKAGE_FILES = 2048
MAX_PACKAGE_FILE_BYTES = 128 * 1024 * 1024
MAX_PACKAGE_TOTAL_BYTES = 256 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_INDEX_MEDIA_TYPES = {"application/json", "text/plain"}
_PACKAGE_MEDIA_TYPES = {"application/octet-stream", PACKAGE_MEDIA_TYPE}


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
    models: tuple[ModelDefinition, ...]

    @property
    def recipe_identity(self) -> tuple[str, str, str]:
        identity = self.recipe.identity
        return identity.publisher, identity.slug, self.recipe_content_sha256

    @property
    def model_identities(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(
            (model.identity.publisher, model.identity.slug, content_sha256(model))
            for model in self.models
        )


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

    def __init__(
        self,
        base_url: str | None = None,
        *,
        cache_root: Path,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 8.0,
        publication_commit: str | None = None,
        api_url: str = PACKAGE_API_ORIGIN,
    ) -> None:
        production = base_url is None
        origin = PACKAGE_RAW_ORIGIN if production else base_url.rstrip("/")
        parsed = urlsplit(origin)
        if not parsed.hostname or (parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1", "caddy"}):
            raise RecipePackageError("recipe_package.url_insecure", "recipe package URL must use HTTPS")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise RecipePackageError("recipe_package.url_invalid", "recipe package URL contains forbidden components")
        api = urlsplit(api_url.rstrip("/"))
        if not api.hostname or (api.scheme != "https" and api.hostname not in {"localhost", "127.0.0.1", "::1", "caddy"}) or api.username or api.password or api.query or api.fragment or api.path not in {"", "/"}:
            raise RecipePackageError("recipe_package.url_invalid", "recipe package API URL is invalid")
        self._production = production
        self._api_url = api_url.rstrip("/")
        self._base_url = origin
        self._cache_root = cache_root.resolve()
        self._cache_root.mkdir(parents=True, exist_ok=True)
        if publication_commit is not None and not _SHA1.fullmatch(publication_commit):
            raise RecipePackageError(
                "recipe_package.commit_invalid", "publication commit is invalid"
            )
        self._publication_commit = publication_commit
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=httpx.Timeout(timeout_seconds),
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
            publication = (
                self._publication_commit
                or self._resolve_publication_commit()
                if self._production
                else None
            )
            index_path = self._raw_path(publication, "catalog-index.json") if publication else PACKAGE_INDEX_PATH
            response = self._client.get(index_path)
        except (httpx.HTTPError, OSError) as error:
            persisted = self._read_persisted_snapshot()
            if persisted is not None:
                return persisted
            raise RecipePackageError("recipe_package.unavailable", "recipe package index is unavailable") from error
        except RecipePackageError:
            persisted = self._read_persisted_snapshot()
            if persisted is not None:
                return persisted
            raise
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if response.status_code != 200 or response.is_redirect:
            persisted = self._read_persisted_snapshot()
            if persisted is not None:
                return persisted
            raise RecipePackageError("recipe_package.unavailable", "recipe package index is unavailable")
        if media_type not in _INDEX_MEDIA_TYPES or len(response.content) > MAX_INDEX_BYTES:
            raise RecipePackageError("recipe_package.response_invalid", "recipe package index response is invalid")
        publication = publication or response.headers.get("x-vonk-publication-commit") or response.headers.get(
            "x-recipe-library-publication-commit"
        )
        if publication is not None and not _SHA1.fullmatch(publication):
            raise RecipePackageError("recipe_package.response_invalid", "recipe publication identity is invalid")
        snapshot, packages = self._parse_index(response.content, publication_commit=publication)
        self._persist_index(response.content, publication_commit=publication)
        self._candidate_active = True
        self._packages = packages
        self._snapshot = snapshot
        self._prepared = {}
        return snapshot

    def _raw_path(self, publication_commit: str, path: str) -> str:
        if not _SHA1.fullmatch(publication_commit) or not _safe_path(path):
            raise RecipePackageError("recipe_package.url_invalid", "recipe publication path is invalid")
        return f"/{PACKAGE_REPOSITORY}/{publication_commit}/{path}"

    def _resolve_publication_commit(self) -> str:
        try:
            response = self._client.get(
                f"{self._api_url}/repos/{PACKAGE_REPOSITORY}/commits/main",
                headers={"Accept": "application/vnd.github+json"},
            )
        except (httpx.HTTPError, OSError) as error:
            raise RecipePackageError("recipe_package.unavailable", "recipe publication is unavailable") from error
        if response.status_code != 200 or response.is_redirect or len(response.content) > 128 * 1024:
            raise RecipePackageError("recipe_package.unavailable", "recipe publication is unavailable")
        try:
            payload = _json(response.content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RecipePackageError("recipe_package.response_invalid", "recipe publication response is invalid") from error
        commit = payload.get("sha") if isinstance(payload, Mapping) else None
        if not isinstance(commit, str) or not _SHA1.fullmatch(commit):
            raise RecipePackageError("recipe_package.response_invalid", "recipe publication identity is invalid")
        return commit

    def _parse_index(
        self, raw: bytes, *, publication_commit: str | None = None
    ) -> tuple[RecipeLibrarySnapshot, dict[str, dict[str, object]]]:
        try:
            index = _json(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RecipePackageError("recipe_package.response_invalid", "recipe package index is invalid JSON") from error
        if not isinstance(index, Mapping) or index.get("schema_version") != PACKAGE_SCHEMA_VERSION or index.get("kind") != "recipe-library-index":
            raise RecipePackageError("recipe_package.schema_incompatible", "recipe package index schema is unsupported")
        repository, commit, raw_recipes = index.get("repository"), index.get("source_commit"), index.get("recipes")
        raw_entities = index.get("catalog_entities")
        contract = index.get("package_contract")
        if repository != PACKAGE_REPOSITORY or not isinstance(commit, str) or not _SHA1.fullmatch(commit) or not isinstance(contract, Mapping) or contract.get("schema_version") != PACKAGE_SCHEMA_VERSION or contract.get("media_type") != PACKAGE_MEDIA_TYPE or not isinstance(raw_recipes, list) or not isinstance(raw_entities, list):
            raise RecipePackageError("recipe_package.response_invalid", "recipe package index identity is invalid")
        index_publication = index.get("publication_commit")
        if index_publication is not None and (
            not isinstance(index_publication, str) or not _SHA1.fullmatch(index_publication)
        ):
            raise RecipePackageError(
                "recipe_package.response_invalid", "recipe publication identity is invalid"
            )
        resolved_publication = publication_commit or self._publication_commit or index_publication or commit
        catalog_entities: list[dict[str, object]] = []
        identities: set[tuple[str, str]] = set()
        for entry in raw_entities:
            if not isinstance(entry, Mapping) or not isinstance(entry.get("document"), Mapping):
                raise RecipePackageError("recipe_package.response_invalid", "catalog model entry is invalid")
            try:
                model = ModelDefinition.model_validate(entry["document"])
            except (TypeError, ValueError) as error:
                raise RecipePackageError("recipe_package.response_invalid", "catalog model document is invalid") from error
            digest = entry.get("content_sha256")
            if not isinstance(digest, str) or digest != content_sha256(model):
                raise RecipePackageError("recipe_package.response_invalid", "catalog model digest is invalid")
            identity = (model.identity.publisher, model.identity.slug)
            if identity in identities:
                raise RecipePackageError("recipe_package.response_invalid", "catalog model identity is duplicated")
            identities.add(identity)
            catalog_entities.append(model.model_dump(mode="json"))
        raw_packages: list[Mapping[str, object]] = []
        for recipe in raw_recipes:
            if not isinstance(recipe, Mapping) or not isinstance(recipe.get("document"), Mapping) or not isinstance(recipe.get("package"), Mapping):
                raise RecipePackageError("recipe_package.response_invalid", "recipe package entry is invalid")
            try:
                document = RecipeDefinition.model_validate(recipe["document"])
            except (TypeError, ValueError) as error:
                raise RecipePackageError("recipe_package.response_invalid", "recipe package recipe document is invalid") from error
            package = recipe["package"]
            package_media_type = package.get("media_type")
            if package_media_type not in _PACKAGE_MEDIA_TYPES:
                raise RecipePackageError(
                    "recipe_package.response_invalid", "recipe package media type is unsupported"
                )
            if package.get("recipe_content_sha256") not in {None, recipe.get("content_sha256")}:
                raise RecipePackageError("recipe_package.response_invalid", "package recipe identity is inconsistent")
            raw_packages.append({
                "publisher": document.identity.publisher, "slug": document.identity.slug,
                "source_path": recipe.get("source_path"), "recipe_content_sha256": recipe.get("content_sha256"),
                "package_sha256": package.get("sha256"), "size": package.get("expected_bytes"),
                "location": package.get("path"), "title": document.metadata.title,
                "description": document.metadata.description, "tags": document.metadata.tags,
                "document": document.model_dump(mode="json"),
            })
        packages: dict[str, dict[str, object]] = {}
        items: list[RecipeLibraryItem] = []
        for package_entry in raw_packages:
            if not isinstance(package_entry, Mapping):
                raise RecipePackageError("recipe_package.response_invalid", "recipe package entry is invalid")
            publisher, slug, digest = package_entry.get("publisher"), package_entry.get("slug"), package_entry.get("recipe_content_sha256")
            package_digest, location, size, source_path = package_entry.get("package_sha256"), package_entry.get("location"), package_entry.get("size"), package_entry.get("source_path")
            location_url = urlsplit(str(location))
            if not all(isinstance(value, str) for value in (publisher, slug, digest, package_digest, location, source_path)) or source_path != f"recipes/{slug}.json" or not _SLUG.fullmatch(str(publisher)) or not _SLUG.fullmatch(str(slug)) or not _SHA256.fullmatch(str(digest)) or not _SHA256.fullmatch(str(package_digest)) or not _safe_path(str(location)) or str(location).startswith("/") or location_url.scheme or location_url.netloc or location_url.query or location_url.fragment or not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= MAX_PACKAGE_BYTES:
                raise RecipePackageError("recipe_package.response_invalid", "recipe package entry identity is invalid")
            key = f"{publisher}/{slug}"
            if key in packages:
                raise RecipePackageError("recipe_package.response_invalid", "recipe package identity is duplicated")
            packages[key] = dict(package_entry)
            packages[key]["publication_commit"] = resolved_publication
            tags = package_entry.get("tags", [])
            items.append(RecipeLibraryItem(library_commit=commit, source_path=str(source_path), publisher=str(publisher), slug=str(slug), title=str(package_entry.get("title", "")), description=str(package_entry.get("description", "")), tags=tuple(str(tag) for tag in tags) if isinstance(tags, list) else (), content_sha256=str(digest), uri=f"vonk://catalog/{publisher}/{slug}@sha256:{digest}", document=package_entry["document"]))
        if [(item.publisher, item.slug) for item in items] != sorted((item.publisher, item.slug) for item in items):
            raise RecipePackageError("recipe_package.response_invalid", "recipe package index is not sorted")
        return RecipeLibrarySnapshot(
            commit=commit,
            items=tuple(items),
            repository=repository,
            catalog_entities=tuple(catalog_entities),
        ), packages

    def _persist_index(self, raw: bytes, *, publication_commit: str | None) -> None:
        payload = {"index": raw.decode("utf-8"), "publication_commit": publication_commit}
        temporary = self._candidate_path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
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
        self._candidate_active = False
        try:
            # A candidate left by an interrupted process is never eligible for
            # promotion by an offline prepare.
            self._candidate_path.unlink()
        except OSError:
            pass
        try:
            payload = _json(self._snapshot_path.read_bytes())
            if not isinstance(payload, Mapping) or not isinstance(payload.get("index"), str):
                return None
            publication = payload.get("publication_commit")
            if publication is not None and not isinstance(publication, str):
                return None
            snapshot, packages = self._parse_index(payload["index"].encode("utf-8"), publication_commit=publication)
        except (OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError, RecipePackageError):
            return None
        self._packages = packages
        self._snapshot = snapshot
        self._previous_snapshot = snapshot
        self._previous_packages = {
            key: dict(value) for key, value in packages.items()
        }
        return snapshot

    def prepare(self, snapshot: RecipeLibrarySnapshot) -> None:
        if self._snapshot is None or self._snapshot.commit != snapshot.commit:
            raise RecipePackageError("recipe_package.snapshot_changed", "package index changed during preparation")
        previous = {item.uri: item for item in self._previous_snapshot.items} if self._previous_snapshot else {}
        self._prepared = {
            item.uri: self.fetch(item.uri)
            for item in snapshot.items
            if item.uri not in previous
            or previous[item.uri].content_sha256 != item.content_sha256
            or not self._same_package(item)
        }
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
        package_digest = str(package["package_sha256"])
        archive, archive_path = self._cached_or_download(
            package_digest,
            str(package["location"]),
            int(package["size"]),
            expected_publication=str(package.get("publication_commit", "")) or None,
        )
        return self._decode_package(archive, item, package=package, archive_path=archive_path)

    def _cached_or_download(
        self,
        digest: str,
        location: str,
        expected_size: int,
        *,
        expected_publication: str | None = None,
    ) -> tuple[bytes, Path]:
        target = self._cache_root / digest[:2] / f"{digest}.tar.gz"
        try:
            cached = target.read_bytes()
            if len(cached) == expected_size and _sha256(cached) == digest:
                return cached, target
        except OSError:
            pass
        try:
            download_url = (
                self._raw_path(expected_publication, location)
                if self._production and expected_publication is not None
                else urljoin(self._base_url + "/", location)
            )
            response = self._client.get(download_url)
        except (httpx.HTTPError, OSError) as error:
            raise RecipePackageError("recipe_package.unavailable", "recipe package is unavailable") from error
        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        publication = response.headers.get("x-vonk-publication-commit") or response.headers.get(
            "x-recipe-library-publication-commit"
        )
        if (
            publication is not None
            and not _SHA1.fullmatch(publication)
            or expected_publication is not None
            and publication is not None
            and publication != expected_publication
        ):
            raise RecipePackageError("recipe_package.snapshot_changed", "recipe package publication changed")
        if response.status_code != 200 or response.is_redirect or media_type not in _PACKAGE_MEDIA_TYPES or len(response.content) != expected_size or len(response.content) > MAX_PACKAGE_BYTES or _sha256(response.content) != digest:
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
        return response.content, target

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
        package_digest = str(package.get("package_sha256")) if package is not None else _sha256(archive)
        package_size = int(package.get("size", len(archive))) if package is not None else len(archive)
        package_path = str(package.get("location", "")) if package is not None else ""
        publication_commit = str(package.get("publication_commit", item.library_commit)) if package is not None else item.library_commit
        if archive_path is None:
            archive_path = Path(getattr(self, "_archive_path", "")) if getattr(self, "_archive_path", None) else Path(".")
        closure_path = self._materialize_closure(files, package_digest, archive_path)
        source_bundle: bytes | None = None
        source_bundle_sha256: str | None = None
        if recipe.execution.mode == "build":
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
                    "recipe_package.package_invalid", "recipe build source closure is invalid"
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
            models=tuple(models),
        )
        return replace(
            item,
            title=metadata.title,
            description=metadata.description,
            tags=tuple(metadata.tags),
            document=recipe.model_dump(mode="json"),
            release_history=release_history,
            dependencies=tuple(model.model_dump(mode="json") for model in models),
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


def load_recipe_package(path: Path, *, package_sha256: str, publisher: str, slug: str, recipe_content_sha256: str, library_commit: str, source_path: str) -> RecipeLibraryItem:
    try:
        archive = path.read_bytes()
    except OSError as error:
        raise RecipePackageError("recipe_package.unavailable", "offline recipe package is unavailable") from error
    if not _SHA256.fullmatch(package_sha256) or len(archive) > MAX_PACKAGE_BYTES or _sha256(archive) != package_sha256:
        raise RecipePackageError("recipe_package.digest_mismatch", "offline recipe package digest does not match")
    item = RecipeLibraryItem(library_commit=library_commit, source_path=source_path, publisher=publisher, slug=slug, title="", description="", tags=(), content_sha256=recipe_content_sha256, uri=f"vonk://catalog/{publisher}/{slug}@sha256:{recipe_content_sha256}", document={})
    decoder = RecipePackageClient.__new__(RecipePackageClient)
    decoder._cache_root = path.parent / ".recipe-package-cache"
    return decoder._decode_package(archive, item, package={"package_sha256": package_sha256, "size": len(archive), "location": str(path)}, archive_path=path)


__all__ = [
    "PACKAGE_INDEX_PATH",
    "PACKAGE_MEDIA_TYPE",
    "PACKAGE_REPOSITORY",
    "RecipePackageClient",
    "RecipePackageError",
    "RecipePackageHandle",
    "load_recipe_package",
]
