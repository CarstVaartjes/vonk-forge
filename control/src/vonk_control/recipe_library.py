"""Read the live reviewed recipe library from its public Git repository."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, replace
from datetime import date
from itertools import pairwise
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from .catalog_contract import (
    CatalogContractError,
    CatalogKind,
    CatalogReference,
    catalog_content_sha256,
    parse_catalog_json,
    parse_catalog_reference,
    validate_catalog_document,
)
from .recipe_contract import (
    RecipeContractError,
    parse_recipe_json,
    recipe_content_sha256,
    recipe_references,
    validate_recipe,
)
from .source_bundles import SourceBundleError, generate_source_bundle

_URI = re.compile(
    r"^vonk://catalog/(?P<publisher>[a-z0-9][a-z0-9-]{1,62})/"
    r"(?P<slug>[a-z0-9][a-z0-9-]{1,62})@sha256:(?P<digest>[0-9a-f]{64})$"
)
_SHA = re.compile(r"^[0-9a-f]{40}$")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
# GitHub's Contents API omits inline content for files larger than 1 MiB. Keep
# the catalog index bounded independently of the smaller metadata response.
# https://docs.github.com/rest/repos/contents#get-repository-content
_MAX_INDEX_BYTES = 12 * 1024 * 1024
# A 12 MiB Git blob expands beyond 16 MiB once GitHub base64-wraps it in JSON.
_MAX_BLOB_RESPONSE_BYTES = 18 * 1024 * 1024
_MAX_RECIPES = 256
_MAX_CATALOG_ENTITIES = 512
_MAX_SOURCE_CONTEXTS = 128
_MAX_SOURCE_FILES = 4096
_MAX_RELEASES = 32
_MAX_RELEASE_CHANGES = 16
_CONTENTS_ACCEPT = "application/vnd.github.object+json"
_SEMVER = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_REPOSITORY = "CarstVaartjes/vonk-forge-recipes"
_INDEX_PATH = "catalog-index.json"
_ENTITY_DIRECTORY = {
    CatalogKind.MODEL_GROUP: "model-groups",
    CatalogKind.MODEL: "models",
    CatalogKind.MODEL_VERSION: "model-versions",
    CatalogKind.RUNTIME_DISTRIBUTION: "runtime-distributions",
    CatalogKind.PATCH_BUNDLE: "patch-bundles",
}
_ENTITY_ORDER = {
    CatalogKind.EXECUTION_HARNESS: 0,
    CatalogKind.MODEL_GROUP: 1,
    CatalogKind.MODEL: 2,
    CatalogKind.MODEL_VERSION: 3,
    CatalogKind.RUNTIME_DISTRIBUTION: 4,
    CatalogKind.PATCH_BUNDLE: 5,
}


class RecipeLibraryError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail[:256]
        super().__init__(self.detail)


@dataclass(frozen=True, slots=True)
class RecipeLibrarySourceFile:
    path: str
    blob_sha: str
    size: int


@dataclass(frozen=True, slots=True)
class RecipeLibrarySourceContext:
    path: str
    content_sha256: str
    expected_bytes: int
    files: tuple[RecipeLibrarySourceFile, ...]


@dataclass(frozen=True, slots=True)
class RecipeLibraryChange:
    kind: str
    summary: str
    details: str | None = None
    references: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RecipeLibraryRelease:
    version: str
    released_at: str
    content_sha256: str
    upgrade_effect: str
    changes: tuple[RecipeLibraryChange, ...]


@dataclass(frozen=True, slots=True)
class RecipeLibraryItem:
    library_commit: str
    source_path: str
    publisher: str
    slug: str
    title: str
    description: str
    tags: tuple[str, ...]
    content_sha256: str
    uri: str
    document: dict[str, object]
    release_history: tuple[RecipeLibraryRelease, ...] = ()
    dependencies: tuple[dict[str, object], ...] = ()
    source_context: RecipeLibrarySourceContext | None = None
    source_bundle: bytes | None = None


@dataclass(frozen=True, slots=True)
class RecipeLibrarySnapshot:
    commit: str
    items: tuple[RecipeLibraryItem, ...]
    repository: str = _REPOSITORY


def recipe_release_is_older(older: str, newer: str) -> bool:
    def parts(value: str) -> tuple[tuple[int, int, int], tuple[str, ...]]:
        core, separator, prerelease = value.partition("-")
        return (
            tuple(int(item) for item in core.split(".")),  # type: ignore[return-value]
            tuple(prerelease.split(".")) if separator else (),
        )

    older_core, older_prerelease = parts(older)
    newer_core, newer_prerelease = parts(newer)
    if older_core != newer_core:
        return older_core < newer_core
    if not older_prerelease:
        return False
    if not newer_prerelease:
        return True
    for older_item, newer_item in zip(older_prerelease, newer_prerelease, strict=False):
        if older_item == newer_item:
            continue
        older_numeric = older_item.isdigit()
        newer_numeric = newer_item.isdigit()
        if older_numeric and newer_numeric:
            return int(older_item) < int(newer_item)
        if older_numeric != newer_numeric:
            return older_numeric
        return older_item < newer_item
    return len(older_prerelease) < len(newer_prerelease)


class RecipeLibraryClient:
    """Fetch exact recipe documents from the current repository main branch."""

    def __init__(
        self,
        *,
        base_url: str = "https://api.github.com",
        repository: str = _REPOSITORY,
        ref: str = "main",
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 8.0,
        cache_ttl_seconds: float = 60.0,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise RecipeLibraryError(
                "recipe_library.url_insecure",
                "recipe library URL must use HTTPS",
            )
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RecipeLibraryError(
                "recipe_library.url_invalid",
                "recipe library URL contains forbidden components",
            )
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise RecipeLibraryError(
                "recipe_library.repository_invalid",
                "recipe library repository is invalid",
            )
        self._repository = repository
        self._ref = ref
        self._cache_ttl_seconds = max(0.0, cache_ttl_seconds)
        self._cached_at = 0.0
        self._cached_snapshot: RecipeLibrarySnapshot | None = None
        self._hydrated_items: dict[str, RecipeLibraryItem] = {}
        self._hydrated_bundles: dict[str, bytes] = {}
        self._cache_lock = threading.Lock()
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def close(self) -> None:
        self._client.close()

    def list(self) -> RecipeLibrarySnapshot:
        cached = self._fresh_snapshot()
        if cached is not None:
            return cached
        with self._cache_lock:
            cached = self._fresh_snapshot()
            if cached is not None:
                return cached
            snapshot = self._load_snapshot()
            if (
                self._cached_snapshot is not None
                and self._cached_snapshot.commit != snapshot.commit
            ):
                self._hydrated_items.clear()
                self._hydrated_bundles.clear()
            self._cached_snapshot = snapshot
            self._cached_at = time.monotonic()
            return snapshot

    def _fresh_snapshot(self) -> RecipeLibrarySnapshot | None:
        if (
            self._cached_snapshot is not None
            and time.monotonic() - self._cached_at < self._cache_ttl_seconds
        ):
            return self._cached_snapshot
        return None

    def _load_snapshot(self) -> RecipeLibrarySnapshot:
        commit = self._current_revision()
        try:
            items = self._indexed_items(commit)
        except RecipeLibraryError as error:
            if error.code != "recipe_library.not_found":
                raise
            items = self._legacy_items(commit)
        return RecipeLibrarySnapshot(
            commit=commit,
            items=items,
            repository=self._repository,
        )

    def _legacy_items(self, commit: str) -> tuple[RecipeLibraryItem, ...]:
        tree = self._get_json(
            f"/repos/{self._repository}/git/trees/{commit}?recursive=1"
        )
        entries = tree.get("tree")
        if not isinstance(entries, list) or tree.get("truncated") is True:
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                "recipe library tree is incomplete",
            )
        paths = sorted(
            str(entry["path"])
            for entry in entries
            if isinstance(entry, dict)
            and entry.get("type") == "blob"
            and isinstance(entry.get("path"), str)
            and re.fullmatch(r"recipes/[a-z0-9][a-z0-9-]{1,62}\.json", entry["path"])
        )
        return tuple(self._item(commit, path) for path in paths)

    def _indexed_items(self, commit: str) -> tuple[RecipeLibraryItem, ...]:
        encoded = self._get_json(
            f"/repos/{self._repository}/contents/{_INDEX_PATH}"
            f"?ref={quote(commit, safe='')}",
            accept=_CONTENTS_ACCEPT,
        )
        document = self._decode_contents_document(
            encoded,
            "recipe library index",
            max_content_bytes=_MAX_INDEX_BYTES,
        )
        recipes = document.get("recipes")
        if (
            document.get("schema_version") not in {1, 2}
            or document.get("repository") != self._repository
            or not isinstance(recipes, list)
            or len(recipes) > _MAX_RECIPES
        ):
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                "recipe library index is invalid",
            )
        entities: dict[CatalogReference, dict[str, object]] = {}
        contexts: dict[str, RecipeLibrarySourceContext] = {}
        if document["schema_version"] == 2:
            entities = self._indexed_entities(document)
            contexts = self._indexed_source_contexts(document)
        items: list[RecipeLibraryItem] = []
        for entry in recipes:
            if not isinstance(entry, dict):
                raise RecipeLibraryError(
                    "recipe_library.response_invalid",
                    "recipe library index entry is invalid",
                )
            source_path = entry.get("source_path")
            expected_digest = entry.get("content_sha256")
            recipe_document = entry.get("document")
            if (
                not isinstance(source_path, str)
                or not isinstance(expected_digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)
                or not isinstance(recipe_document, dict)
            ):
                raise RecipeLibraryError(
                    "recipe_library.response_invalid",
                    "recipe library index entry is incomplete",
                )
            item = self._item_from_document(commit, source_path, recipe_document)
            release_history = self._release_history(entry, item.content_sha256)
            dependencies = self._dependency_closure(item.document, entities)
            source_context = self._recipe_source_context(item.document, contexts)
            item = replace(
                item,
                release_history=release_history,
                dependencies=dependencies,
                source_context=source_context,
            )
            if item.content_sha256 != expected_digest:
                raise RecipeLibraryError(
                    "recipe_library.digest_mismatch",
                    "recipe library index digest does not match its document",
                )
            items.append(item)
        if [item.source_path for item in items] != sorted(
            item.source_path for item in items
        ):
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                "recipe library index is not sorted",
            )
        return tuple(items)

    @staticmethod
    def _release_history(
        entry: dict[str, object], current_digest: str
    ) -> tuple[RecipeLibraryRelease, ...]:
        raw_release = entry.get("release")
        if raw_release is None:
            return ()
        if not isinstance(raw_release, dict) or set(raw_release) != {
            "version",
            "released_at",
            "history",
        }:
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                "recipe library release metadata is invalid",
            )
        current_version = raw_release["version"]
        current_released_at = raw_release["released_at"]
        raw_releases = raw_release["history"]
        if (
            not isinstance(current_version, str)
            or _SEMVER.fullmatch(current_version) is None
            or not isinstance(current_released_at, str)
            or not isinstance(raw_releases, list)
            or not raw_releases
            or len(raw_releases) > _MAX_RELEASES
        ):
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                "recipe library release history is invalid",
            )
        categories = {
            "initial",
            "model",
            "runtime",
            "performance",
            "fix",
            "security",
            "compatibility",
            "metadata",
            "breaking",
        }
        effects = {"metadata-only", "restart", "reinstall", "rebuild"}
        releases: list[RecipeLibraryRelease] = []
        versions: set[str] = set()
        digests: set[str] = set()
        for raw_release in raw_releases:
            if not isinstance(raw_release, dict) or set(raw_release) != {
                "version",
                "released_at",
                "recipe_content_sha256",
                "upgrade_effect",
                "changes",
            }:
                raise RecipeLibraryError(
                    "recipe_library.response_invalid",
                    "recipe library release entry is invalid",
                )
            version = raw_release["version"]
            released_at = raw_release["released_at"]
            digest = raw_release["recipe_content_sha256"]
            effect = raw_release["upgrade_effect"]
            raw_changes = raw_release["changes"]
            if (
                not isinstance(version, str)
                or _SEMVER.fullmatch(version) is None
                or version in versions
                or not isinstance(released_at, str)
                or len(released_at) != 10
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or digest in digests
                or effect not in effects
                or not isinstance(raw_changes, list)
                or not raw_changes
                or len(raw_changes) > _MAX_RELEASE_CHANGES
            ):
                raise RecipeLibraryError(
                    "recipe_library.response_invalid",
                    "recipe library release entry is invalid",
                )
            try:
                parsed_date = date.fromisoformat(released_at)
            except ValueError as error:
                raise RecipeLibraryError(
                    "recipe_library.response_invalid",
                    "recipe library release date is invalid",
                ) from error
            if parsed_date.isoformat() != released_at:
                raise RecipeLibraryError(
                    "recipe_library.response_invalid",
                    "recipe library release date is invalid",
                )
            changes: list[RecipeLibraryChange] = []
            for raw_change in raw_changes:
                if (
                    not isinstance(raw_change, dict)
                    or not {"kind", "summary"}.issubset(raw_change)
                    or not set(raw_change).issubset(
                        {"kind", "summary", "details", "references"}
                    )
                ):
                    raise RecipeLibraryError(
                        "recipe_library.response_invalid",
                        "recipe library release change is invalid",
                    )
                kind = raw_change["kind"]
                summary = raw_change["summary"]
                details = raw_change.get("details")
                references = raw_change.get("references", [])
                if (
                    kind not in categories
                    or not isinstance(summary, str)
                    or not summary.strip()
                    or summary != summary.strip()
                    or len(summary) > 160
                    or (
                        details is not None
                        and (
                            not isinstance(details, str)
                            or not details.strip()
                            or details != details.strip()
                            or len(details) > 1000
                        )
                    )
                    or not isinstance(references, list)
                    or ("references" in raw_change and not references)
                    or len(references) > 8
                    or any(
                        not isinstance(reference, str)
                        or not reference.startswith("https://")
                        or len(reference) > 500
                        for reference in references
                    )
                    or len(references) != len(set(references))
                ):
                    raise RecipeLibraryError(
                        "recipe_library.response_invalid",
                        "recipe library release change is invalid",
                    )
                changes.append(
                    RecipeLibraryChange(
                        kind=str(kind),
                        summary=summary,
                        details=details if isinstance(details, str) else None,
                        references=tuple(str(reference) for reference in references),
                    )
                )
            digests.add(digest)
            versions.add(version)
            releases.append(
                RecipeLibraryRelease(
                    version=version,
                    released_at=released_at,
                    content_sha256=digest,
                    upgrade_effect=str(effect),
                    changes=tuple(changes),
                )
            )
        if releases[0].content_sha256 != current_digest:
            raise RecipeLibraryError(
                "recipe_library.digest_mismatch",
                "current recipe release does not match its document",
            )
        if (
            current_version != releases[0].version
            or current_released_at != releases[0].released_at
        ):
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                "current recipe release metadata is inconsistent",
            )
        if any(
            not recipe_release_is_older(older.version, newer.version)
            for newer, older in pairwise(releases)
        ):
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                "recipe library release versions are not newest-first",
            )
        return tuple(releases)

    def _indexed_entities(
        self, index: dict[str, object]
    ) -> dict[CatalogReference, dict[str, object]]:
        raw_entities = index.get("catalog_entities")
        if (
            not isinstance(raw_entities, list)
            or len(raw_entities) > _MAX_CATALOG_ENTITIES
        ):
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                "recipe library catalog entity index is invalid",
            )
        entities: dict[CatalogReference, dict[str, object]] = {}
        paths: list[str] = []
        for entry in raw_entities:
            if not isinstance(entry, dict):
                raise RecipeLibraryError(
                    "recipe_library.response_invalid",
                    "recipe library catalog entity entry is invalid",
                )
            source_path = entry.get("source_path")
            expected_digest = entry.get("content_sha256")
            value = entry.get("document")
            if (
                not isinstance(source_path, str)
                or not isinstance(expected_digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)
                or not isinstance(value, dict)
            ):
                raise RecipeLibraryError(
                    "recipe_library.response_invalid",
                    "recipe library catalog entity entry is incomplete",
                )
            try:
                document = dict(parse_catalog_json(json.dumps(value).encode()))
                validate_catalog_document(document)
                kind = CatalogKind(document["kind"])
                identity = document["identity"]
                assert isinstance(identity, dict)
                reference = CatalogReference(
                    kind,
                    str(identity["publisher"]),
                    str(identity["slug"]),
                    catalog_content_sha256(document),
                )
            except (
                CatalogContractError,
                KeyError,
                ValueError,
                AssertionError,
            ) as error:
                raise RecipeLibraryError(
                    "recipe_library.schema_incompatible",
                    "recipe library catalog entity is incompatible",
                ) from error
            directory = _ENTITY_DIRECTORY.get(kind)
            if (
                directory is None
                or source_path != f"{directory}/{reference.slug}.json"
                or reference.content_sha256 != expected_digest
                or reference in entities
            ):
                raise RecipeLibraryError(
                    "recipe_library.digest_mismatch",
                    "recipe library catalog entity identity is inconsistent",
                )
            entities[reference] = document
            paths.append(source_path)
        if paths != sorted(paths):
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                "recipe library catalog entity index is not sorted",
            )
        return entities

    def _indexed_source_contexts(
        self, index: dict[str, object]
    ) -> dict[str, RecipeLibrarySourceContext]:
        raw_contexts = index.get("source_contexts")
        if (
            not isinstance(raw_contexts, list)
            or len(raw_contexts) > _MAX_SOURCE_CONTEXTS
        ):
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                "recipe library source context index is invalid",
            )
        contexts: dict[str, RecipeLibrarySourceContext] = {}
        file_count = 0
        for entry in raw_contexts:
            if not isinstance(entry, dict):
                raise RecipeLibraryError(
                    "recipe_library.response_invalid",
                    "recipe library source context entry is invalid",
                )
            path = entry.get("context_path")
            digest = entry.get("content_sha256")
            expected_bytes = entry.get("expected_bytes")
            raw_files = entry.get("files")
            if (
                not isinstance(path, str)
                or not self._safe_relative_path(path)
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or not isinstance(expected_bytes, int)
                or isinstance(expected_bytes, bool)
                or not 1 <= expected_bytes <= 64 * 1024 * 1024
                or not isinstance(raw_files, list)
                or not raw_files
            ):
                raise RecipeLibraryError(
                    "recipe_library.response_invalid",
                    "recipe library source context entry is incomplete",
                )
            files: list[RecipeLibrarySourceFile] = []
            for raw_file in raw_files:
                file_count += 1
                if file_count > _MAX_SOURCE_FILES or not isinstance(raw_file, dict):
                    raise RecipeLibraryError(
                        "recipe_library.response_invalid",
                        "recipe library source context contains too many files",
                    )
                file_path = raw_file.get("path")
                blob_sha = raw_file.get("blob_sha")
                size = raw_file.get("size")
                if (
                    not isinstance(file_path, str)
                    or not self._safe_relative_path(file_path)
                    or not isinstance(blob_sha, str)
                    or not _SHA.fullmatch(blob_sha)
                    or not isinstance(size, int)
                    or isinstance(size, bool)
                    or not 0 <= size <= 12 * 1024 * 1024
                ):
                    raise RecipeLibraryError(
                        "recipe_library.response_invalid",
                        "recipe library source file entry is invalid",
                    )
                files.append(RecipeLibrarySourceFile(file_path, blob_sha, size))
            if [item.path for item in files] != sorted(item.path for item in files):
                raise RecipeLibraryError(
                    "recipe_library.response_invalid",
                    "recipe library source files are not sorted",
                )
            context = RecipeLibrarySourceContext(
                path, digest, expected_bytes, tuple(files)
            )
            if digest in contexts and contexts[digest] != context:
                raise RecipeLibraryError(
                    "recipe_library.digest_mismatch",
                    "recipe library source context digest is inconsistent",
                )
            contexts[digest] = context
        return contexts

    @staticmethod
    def _safe_relative_path(value: str) -> bool:
        path = PurePosixPath(value)
        return (
            bool(path.parts)
            and not path.is_absolute()
            and all(part not in {"", ".", ".."} for part in path.parts)
        )

    def _dependency_closure(
        self,
        recipe: dict[str, object],
        entities: dict[CatalogReference, dict[str, object]],
    ) -> tuple[dict[str, object], ...]:
        if not entities:
            return ()
        pending = list(recipe_references(recipe))
        selected: dict[CatalogReference, dict[str, object]] = {}
        while pending:
            reference = pending.pop()
            if reference in selected:
                continue
            document = entities.get(reference)
            if document is None:
                if reference.kind is CatalogKind.EXECUTION_HARNESS:
                    continue
                raise RecipeLibraryError(
                    "recipe_library.response_invalid",
                    f"recipe library dependency is missing: {reference.kind.value}/{reference.slug}",
                )
            selected[reference] = document
            field: str | None = None
            expected_kind: CatalogKind | None = None
            if reference.kind is CatalogKind.MODEL:
                field, expected_kind = "model_group", CatalogKind.MODEL_GROUP
            elif reference.kind is CatalogKind.MODEL_VERSION:
                field, expected_kind = "model", CatalogKind.MODEL
            elif reference.kind is CatalogKind.RUNTIME_DISTRIBUTION:
                field, expected_kind = (
                    "implements_harness",
                    CatalogKind.EXECUTION_HARNESS,
                )
            elif reference.kind is CatalogKind.PATCH_BUNDLE:
                field, expected_kind = "applies_to", CatalogKind.RUNTIME_DISTRIBUTION
            if field is not None:
                value = document.get(field)
                if not isinstance(value, dict):
                    raise RecipeLibraryError(
                        "recipe_library.response_invalid",
                        "recipe library dependency reference is invalid",
                    )
                try:
                    pending.append(
                        parse_catalog_reference(value, expected_kind=expected_kind)
                    )
                except CatalogContractError as error:
                    raise RecipeLibraryError(
                        "recipe_library.response_invalid",
                        "recipe library dependency reference is invalid",
                    ) from error
        ordered = sorted(
            selected.items(),
            key=lambda item: (
                _ENTITY_ORDER[item[0].kind],
                item[0].publisher,
                item[0].slug,
            ),
        )
        return tuple(document for _, document in ordered)

    @staticmethod
    def _recipe_source_context(
        recipe: dict[str, object],
        contexts: dict[str, RecipeLibrarySourceContext],
    ) -> RecipeLibrarySourceContext | None:
        if not contexts:
            return None
        build = recipe.get("build")
        context = build.get("context") if isinstance(build, dict) else None
        path = context.get("path") if isinstance(context, dict) else None
        digest = context.get("sha256") if isinstance(context, dict) else None
        expected_bytes = (
            context.get("expected_bytes") if isinstance(context, dict) else None
        )
        selected = contexts.get(digest) if isinstance(digest, str) else None
        if (
            selected is None
            or selected.path != path
            or selected.expected_bytes != expected_bytes
        ):
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                "recipe library source context is missing",
            )
        return selected

    def fetch(self, uri: str) -> RecipeLibraryItem:
        match = _URI.fullmatch(uri)
        if match is None:
            raise RecipeLibraryError(
                "recipe_library.uri_invalid",
                "use vonk://catalog/PUBLISHER/SLUG@sha256:DIGEST",
            )
        publisher, slug = (
            match.group("publisher"),
            match.group("slug"),
        )
        if publisher != "vonk-forge":
            raise RecipeLibraryError(
                "recipe_library.not_found",
                "recipe is not in the default recipe library",
            )
        matching_slug: RecipeLibraryItem | None = None
        for item in self.list().items:
            if item.publisher == publisher and item.slug == slug:
                matching_slug = item
                if item.uri == uri:
                    return self._hydrate_source_bundle(item)
        if matching_slug is not None:
            raise RecipeLibraryError(
                "recipe_library.digest_mismatch",
                "recipe library content does not match the requested digest",
            )
        raise RecipeLibraryError(
            "recipe_library.not_found",
            "recipe is not in the current default recipe library",
        )

    def _hydrate_source_bundle(self, item: RecipeLibraryItem) -> RecipeLibraryItem:
        if item.source_context is None:
            return item
        with self._cache_lock:
            cached = self._hydrated_items.get(item.uri)
            if cached is not None:
                return cached
            cached_bundle = self._hydrated_bundles.get(
                item.source_context.content_sha256
            )
            if cached_bundle is not None:
                hydrated = replace(item, source_bundle=cached_bundle)
                self._hydrated_items[item.uri] = hydrated
                return hydrated
            files: dict[str, bytes] = {}
            for source_file in item.source_context.files:
                encoded = self._get_json(
                    f"/repos/{self._repository}/git/blobs/{source_file.blob_sha}",
                    max_response_bytes=_MAX_BLOB_RESPONSE_BYTES,
                )
                content = self._decode_base64(encoded, "recipe library source blob")
                git_identity = hashlib.sha1(
                    f"blob {len(content)}\0".encode() + content,
                    usedforsecurity=False,
                ).hexdigest()
                if (
                    len(content) != source_file.size
                    or git_identity != source_file.blob_sha
                ):
                    raise RecipeLibraryError(
                        "recipe_library.digest_mismatch",
                        "recipe library source blob digest does not match",
                    )
                files[source_file.path] = content
            try:
                bundle = generate_source_bundle(files)
            except SourceBundleError as error:
                raise RecipeLibraryError(
                    "recipe_library.response_invalid",
                    "recipe library source bundle is invalid",
                ) from error
            if (
                bundle.sha256 != item.source_context.content_sha256
                or len(bundle.archive) != item.source_context.expected_bytes
            ):
                raise RecipeLibraryError(
                    "recipe_library.digest_mismatch",
                    "recipe library source bundle digest does not match",
                )
            hydrated = replace(item, source_bundle=bundle.archive)
            self._hydrated_bundles[item.source_context.content_sha256] = bundle.archive
            self._hydrated_items[item.uri] = hydrated
            return hydrated

    def _current_revision(self) -> str:
        value = self._get_json(f"/repos/{self._repository}/commits/{self._ref}")
        commit = value.get("sha")
        if not isinstance(commit, str) or not _SHA.fullmatch(commit):
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                "recipe library commit is invalid",
            )
        return commit

    def _item(self, commit: str, source_path: str) -> RecipeLibraryItem:
        encoded = self._get_json(
            f"/repos/{self._repository}/contents/{quote(source_path, safe='/')}"
            f"?ref={quote(commit, safe='')}"
        )
        document = self._decode_document(encoded, "recipe library recipe")
        return self._item_from_document(commit, source_path, document)

    @staticmethod
    def _decode_document(encoded: dict[str, Any], label: str) -> dict[str, object]:
        decoded = RecipeLibraryClient._decode_base64(encoded, label)
        return RecipeLibraryClient._parse_document(decoded, label)

    def _decode_contents_document(
        self,
        encoded: dict[str, Any],
        label: str,
        *,
        max_content_bytes: int,
    ) -> dict[str, object]:
        decoded = self._decode_contents_payload(
            encoded,
            label,
            max_content_bytes=max_content_bytes,
        )
        return self._parse_document(decoded, label)

    def _decode_contents_payload(
        self,
        encoded: dict[str, Any],
        label: str,
        *,
        max_content_bytes: int,
    ) -> bytes:
        encoding = encoded.get("encoding")
        if encoding == "base64":
            content = self._decode_base64(encoded, label)
            blob_sha, expected_size = self._contents_file_metadata(
                encoded,
                label,
                max_content_bytes=max_content_bytes,
            )
            self._verify_blob_identity(content, blob_sha, expected_size, label)
            return content

        inline_content = encoded.get("content")
        if encoding != "none" or inline_content not in (None, ""):
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                f"{label} content is invalid",
            )
        blob_sha, expected_size = self._contents_file_metadata(
            encoded,
            label,
            max_content_bytes=max_content_bytes,
        )

        blob = self._get_json(
            f"/repos/{self._repository}/git/blobs/{blob_sha}",
            max_response_bytes=_MAX_BLOB_RESPONSE_BYTES,
        )
        returned_sha = blob.get("sha")
        returned_size = blob.get("size")
        if (
            returned_sha != blob_sha
            or not isinstance(returned_size, int)
            or isinstance(returned_size, bool)
            or returned_size != expected_size
        ):
            raise RecipeLibraryError(
                "recipe_library.digest_mismatch",
                f"{label} blob metadata does not match",
            )
        content = self._decode_base64(blob, f"{label} blob")
        self._verify_blob_identity(content, blob_sha, expected_size, label)
        return content

    @staticmethod
    def _contents_file_metadata(
        encoded: dict[str, Any],
        label: str,
        *,
        max_content_bytes: int,
    ) -> tuple[str, int]:
        blob_sha = encoded.get("sha")
        expected_size = encoded.get("size")
        if (
            encoded.get("type") != "file"
            or not isinstance(blob_sha, str)
            or _SHA.fullmatch(blob_sha) is None
            or not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 1
        ):
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                f"{label} metadata is invalid",
            )
        if expected_size > max_content_bytes:
            raise RecipeLibraryError(
                "recipe_library.response_too_large",
                f"{label} content exceeds its size limit",
            )
        return blob_sha, expected_size

    @staticmethod
    def _verify_blob_identity(
        content: bytes,
        blob_sha: str,
        expected_size: int,
        label: str,
    ) -> None:
        git_identity = hashlib.sha1(
            f"blob {len(content)}\0".encode() + content,
            usedforsecurity=False,
        ).hexdigest()
        if len(content) != expected_size or git_identity != blob_sha:
            raise RecipeLibraryError(
                "recipe_library.digest_mismatch",
                f"{label} blob digest does not match",
            )

    @staticmethod
    def _parse_document(decoded: bytes, label: str) -> dict[str, object]:
        try:
            value = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                f"{label} content is invalid",
            ) from error
        if not isinstance(value, dict):
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                f"{label} must be an object",
            )
        return value

    @staticmethod
    def _decode_base64(encoded: dict[str, Any], label: str) -> bytes:
        if encoded.get("encoding") != "base64" or not isinstance(
            encoded.get("content"), str
        ):
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                f"{label} content is invalid",
            )
        try:
            return base64.b64decode("".join(encoded["content"].split()), validate=True)
        except binascii.Error as error:
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                f"{label} content is invalid",
            ) from error

    def _item_from_document(
        self,
        commit: str,
        source_path: str,
        value: dict[str, object],
        *,
        dependencies: tuple[dict[str, object], ...] = (),
        source_context: RecipeLibrarySourceContext | None = None,
    ) -> RecipeLibraryItem:
        try:
            document = dict(parse_recipe_json(json.dumps(value).encode()))
        except (RecipeContractError, UnicodeDecodeError) as error:
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                "recipe library recipe content is invalid",
            ) from error
        try:
            validate_recipe(document)
        except RecipeContractError as error:
            raise RecipeLibraryError(
                "recipe_library.schema_incompatible",
                f"recipe library recipe is incompatible at {error.path}",
            ) from error
        identity = document.get("identity")
        metadata = document.get("metadata")
        if not isinstance(identity, dict) or not isinstance(metadata, dict):
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                "recipe library recipe metadata is incomplete",
            )
        publisher = identity.get("publisher")
        slug = identity.get("slug")
        title = metadata.get("title")
        description = metadata.get("description")
        tags = metadata.get("tags", [])
        if (
            not isinstance(publisher, str)
            or not _SLUG.fullmatch(publisher)
            or not isinstance(slug, str)
            or not _SLUG.fullmatch(slug)
            or not isinstance(title, str)
            or not title
            or not isinstance(description, str)
            or not description
            or not isinstance(tags, list)
            or any(not isinstance(tag, str) for tag in tags)
            or source_path != f"recipes/{slug}.json"
        ):
            raise RecipeLibraryError(
                "recipe_library.identity_invalid",
                "recipe library recipe identity or metadata is invalid",
            )
        digest = recipe_content_sha256(document)
        return RecipeLibraryItem(
            library_commit=commit,
            source_path=source_path,
            publisher=publisher,
            slug=slug,
            title=title,
            description=description,
            tags=tuple(tags),
            content_sha256=digest,
            uri=f"vonk://catalog/{publisher}/{slug}@sha256:{digest}",
            document=document,
            dependencies=dependencies,
            source_context=source_context,
        )

    def _get_json(
        self,
        path: str,
        *,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
        accept: str | None = None,
    ) -> dict[str, Any]:
        try:
            headers = {"Accept": accept} if accept is not None else None
            with self._client.stream("GET", path, headers=headers) as response:
                if 300 <= response.status_code < 400:
                    raise RecipeLibraryError(
                        "recipe_library.redirect_forbidden",
                        "recipe library redirects are not followed",
                    )
                if response.status_code == 404:
                    raise RecipeLibraryError(
                        "recipe_library.not_found",
                        "recipe library entry was not found",
                    )
                if response.status_code != 200:
                    raise RecipeLibraryError(
                        "recipe_library.unavailable",
                        "recipe library request failed",
                    )
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > max_response_bytes:
                        raise RecipeLibraryError(
                            "recipe_library.response_too_large",
                            "recipe library response exceeds its size limit",
                        )
        except RecipeLibraryError:
            raise
        except (httpx.HTTPError, OSError) as error:
            raise RecipeLibraryError(
                "recipe_library.unavailable",
                "recipe library is unavailable",
            ) from error
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                "recipe library response is invalid JSON",
            ) from error
        if not isinstance(value, dict):
            raise RecipeLibraryError(
                "recipe_library.response_invalid",
                "recipe library response must be an object",
            )
        return value


__all__ = [
    "RecipeLibraryClient",
    "RecipeLibraryError",
    "RecipeLibraryItem",
    "RecipeLibrarySnapshot",
    "RecipeLibrarySourceContext",
    "RecipeLibrarySourceFile",
]
