"""Durable, content-addressed model artifacts stored on the Controller NAS.

This module is deliberately independent from Spark presence.  The database
stores immutable set identities and checkpoints; the NAS root stores the
actual verified bytes.  A cache entry is only complete when every artifact in
its manifest has an on-disk object with the expected length and SHA-256.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import threading
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .catalog_contract import CatalogKind
from .models import (
    CatalogEntity,
    CatalogEntityRevision,
    FleetProfile,
    LocalRecipeRevision,
    ModelCacheArtifact,
    ModelCacheOperation,
    ModelCacheSet,
    ModelCacheSetArtifact,
    RecipeInstallation,
    RecipeRevision,
    RecipeRun,
)

SCHEMA_VERSION = 2
SOURCE_POLICY = "nas-first"
_DIGEST_LENGTH = 64
_MAX_ARTIFACTS = 128
_MAX_MANIFEST_BYTES = 1_048_576
_CHUNK_BYTES = 1024 * 1024
_MAX_HTTP_REDIRECTS = 0
_USE_MANIFEST_BYTES = object()


class ModelCacheError(RuntimeError):
    """Base error carrying a stable API code and bounded operator detail."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail[:512]
        super().__init__(self.detail)


class ModelCacheConflict(ModelCacheError):
    pass


class ModelCacheNotFound(ModelCacheError):
    pass


class ModelCacheResolutionError(ModelCacheError):
    pass


class ModelCacheStorageError(ModelCacheError):
    pass


@dataclass(frozen=True, slots=True)
class ArtifactSpec:
    """One exact downloadable artifact in a resolved set."""

    key: str
    artifact_id: str
    path: str
    kind: str
    repository: str | None
    source: str
    revision: str | None
    sha256: str
    expected_bytes: int
    roles: tuple[str, ...]
    model_version_sha256: str | None = None

    def identity(self) -> dict[str, object]:
        return {
            "key": self.key,
            "id": self.artifact_id,
            "path": self.path,
            "kind": self.kind,
            "repository": self.repository,
            "source": self.source,
            "revision": self.revision,
            "sha256": self.sha256,
            "download_bytes": self.expected_bytes,
            "roles": list(self.roles),
            "model_version_sha256": self.model_version_sha256,
        }

    @classmethod
    def from_manifest(cls, value: Mapping[str, object]) -> ArtifactSpec:
        try:
            roles = value["roles"]
            if not isinstance(roles, list) or not all(
                isinstance(role, str) and role for role in roles
            ):
                raise TypeError
            result = cls(
                key=str(value["key"]),
                artifact_id=str(value["id"]),
                path=str(value["path"]),
                kind=str(value["kind"]),
                repository=(
                    None
                    if value.get("repository") is None
                    else str(value["repository"])
                ),
                source=str(value["source"]),
                revision=(
                    None
                    if value.get("revision") is None
                    else str(value["revision"])
                ),
                sha256=str(value["sha256"]),
                expected_bytes=int(value["download_bytes"]),
                roles=tuple(roles),
                model_version_sha256=(
                    None
                    if value.get("model_version_sha256") is None
                    else str(value["model_version_sha256"])
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ModelCacheResolutionError(
                "model_cache.manifest_invalid", "cache manifest artifact is invalid"
            ) from error
        _validate_artifact(result)
        return result


@dataclass(frozen=True, slots=True)
class ArtifactSetManifest:
    model_version_sha256: str | None
    recipe_revision_sha256: str | None
    model_versions: tuple[str, ...]
    artifacts: tuple[ArtifactSpec, ...]
    model_version_ref: Mapping[str, object] | None = None

    def document(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "source_policy": SOURCE_POLICY,
            "model_version_sha256": self.model_version_sha256,
            "recipe_revision_sha256": self.recipe_revision_sha256,
            "model_version_ref": (
                None
                if self.model_version_ref is None
                else dict(self.model_version_ref)
            ),
            "model_versions": list(self.model_versions),
            "artifacts": [item.identity() for item in self.artifacts],
        }

    @property
    def digest(self) -> str:
        return _sha256_json(self.document())

    @property
    def expected_bytes(self) -> int:
        return sum(
            value.expected_bytes
            for _digest, value in _unique_artifacts(self.artifacts).items()
        )

    @classmethod
    def from_document(cls, value: Mapping[str, object]) -> ArtifactSetManifest:
        if value.get("schema_version") != SCHEMA_VERSION:
            raise ModelCacheResolutionError(
                "model_cache.schema_unsupported", "cache manifest schema is unsupported"
            )
        raw_artifacts = value.get("artifacts")
        raw_versions = value.get("model_versions", [])
        if not isinstance(raw_artifacts, list) or not isinstance(raw_versions, list):
            raise ModelCacheResolutionError(
                "model_cache.manifest_invalid", "cache manifest shape is invalid"
            )
        artifacts = tuple(
            ArtifactSpec.from_manifest(item)
            for item in raw_artifacts
            if isinstance(item, Mapping)
        )
        if len(artifacts) != len(raw_artifacts):
            raise ModelCacheResolutionError(
                "model_cache.manifest_invalid", "cache manifest artifacts are invalid"
            )
        result = cls(
            model_version_sha256=_optional_digest(value.get("model_version_sha256")),
            recipe_revision_sha256=_optional_digest(
                value.get("recipe_revision_sha256")
            ),
            model_versions=tuple(str(item) for item in raw_versions),
            artifacts=tuple(sorted(artifacts, key=lambda item: item.key)),
            model_version_ref=(
                value.get("model_version_ref")
                if isinstance(value.get("model_version_ref"), Mapping)
                else None
            ),
        )
        _validate_manifest(result)
        return result


@dataclass(frozen=True, slots=True)
class StorageSummary:
    total_bytes: int
    free_bytes: int
    reserve_bytes: int
    available_bytes: int
    unique_used_bytes: int
    in_flight_bytes: int
    protected_bytes: int
    reclaimable_bytes: int

    def document(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "total_bytes": self.total_bytes,
            "free_bytes": self.free_bytes,
            "reserve_bytes": self.reserve_bytes,
            "available_bytes": self.available_bytes,
            "unique_used_bytes": self.unique_used_bytes,
            "in_flight_bytes": self.in_flight_bytes,
            "protected_bytes": self.protected_bytes,
            "reclaimable_bytes": self.reclaimable_bytes,
        }


@dataclass(frozen=True, slots=True)
class CacheOperationView:
    id: str
    request_key: str
    kind: str
    state: str
    attempt: int
    artifact_set_sha256: str | None
    plan_digest: str | None
    progress: Mapping[str, object]
    result: Mapping[str, object] | None
    last_error: str | None
    created_at: str
    updated_at: str
    completed_at: str | None


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _optional_digest(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) != _DIGEST_LENGTH:
        raise ModelCacheResolutionError(
            "model_cache.digest_invalid", "cache identity digest is invalid"
        )
    try:
        int(value, 16)
    except ValueError as error:
        raise ModelCacheResolutionError(
            "model_cache.digest_invalid", "cache identity digest is invalid"
        ) from error
    if value != value.lower():
        raise ModelCacheResolutionError(
            "model_cache.digest_invalid", "cache identity digest is invalid"
        )
    return value


def _validate_artifact(value: ArtifactSpec) -> None:
    if (
        not value.key
        or len(value.key) > 256
        or not value.key[0].isalpha()
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_.:-" for character in value.key)
        or not value.artifact_id
        or len(value.artifact_id) > 256
        or re.fullmatch(r"[a-z][a-z0-9_.:-]{0,255}", value.artifact_id) is None
        or not value.path
        or len(value.path) > 512
        or value.path.startswith("/")
        or "\\" in value.path
        or "\x00" in value.path
        or any(part in {"", ".", ".."} for part in value.path.split("/"))
        or value.kind not in {"huggingface.file", "http.file", "file"}
        or len(value.sha256) != _DIGEST_LENGTH
        or value.sha256 != value.sha256.lower()
        or not _is_hex(value.sha256)
        or not isinstance(value.expected_bytes, int)
        or isinstance(value.expected_bytes, bool)
        or value.expected_bytes <= 0
        or not value.roles
        or len(value.roles) > 32
        or any(not isinstance(role, str) for role in value.roles)
        or len(set(value.roles)) != len(value.roles)
        or any(not role or len(role) > 64 for role in value.roles)
    ):
        raise ModelCacheResolutionError(
            "model_cache.artifact_invalid", "cache artifact identity is invalid"
        )
    if value.kind != "file" and value.revision is None:
        raise ModelCacheResolutionError(
            "model_cache.revision_missing",
            "remote cache artifacts require an immutable revision",
        )
    _validate_source(value.source)


def _validate_manifest(value: ArtifactSetManifest) -> None:
    if len(value.artifacts) < 1 or len(value.artifacts) > _MAX_ARTIFACTS:
        raise ModelCacheResolutionError(
            "model_cache.artifact_count", "cache artifact set count is invalid"
        )
    keys = [item.key for item in value.artifacts]
    if len(keys) != len(set(keys)):
        raise ModelCacheResolutionError(
            "model_cache.artifact_duplicate", "cache artifact keys must be unique"
        )
    _unique_artifacts(value.artifacts)
    if any(not isinstance(item, str) or not _is_hex(item) for item in value.model_versions):
        raise ModelCacheResolutionError(
            "model_cache.model_versions_invalid", "cache model dependency pins are invalid"
        )
    encoded = json.dumps(value.document(), sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > _MAX_MANIFEST_BYTES:
        raise ModelCacheResolutionError(
            "model_cache.manifest_too_large", "cache manifest exceeds the size limit"
        )


def _unique_artifacts(values: Sequence[ArtifactSpec]) -> dict[str, ArtifactSpec]:
    result: dict[str, ArtifactSpec] = {}
    for item in values:
        existing = result.get(item.sha256)
        if existing is not None and existing.expected_bytes != item.expected_bytes:
            raise ModelCacheResolutionError(
                "model_cache.digest_size_conflict",
                "one artifact digest has conflicting sizes",
            )
        result.setdefault(item.sha256, item)
    return result


def _is_hex(value: str) -> bool:
    if not value:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _validate_source(source: str) -> None:
    try:
        parsed = urlsplit(source)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise ModelCacheResolutionError(
            "model_cache.source_invalid", "cache source URL is invalid"
        ) from error
    if parsed.scheme in {"http", "https"}:
        if (
            not hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
            or parsed.query
            or port is not None
        ):
            raise ModelCacheResolutionError(
                "model_cache.source_invalid", "cache source URL is invalid"
            )
        return
    if parsed.scheme == "file":
        if parsed.netloc not in {"", "localhost"} or not parsed.path.startswith("/"):
            raise ModelCacheResolutionError(
                "model_cache.source_invalid", "cache file source is invalid"
            )
        return
    raise ModelCacheResolutionError(
        "model_cache.source_invalid", "cache source must use HTTPS, HTTP or file"
    )


def _source_for_catalog_artifact(
    artifact: Mapping[str, object],
) -> tuple[str, str | None]:
    kind = artifact.get("kind")
    repository = artifact.get("repository")
    path = artifact.get("path")
    revision = artifact.get("revision")
    if not isinstance(repository, str) or not repository:
        raise ModelCacheResolutionError(
            "model_cache.source_invalid", "catalog artifact repository is invalid"
        )
    if not isinstance(path, str) or not path:
        raise ModelCacheResolutionError(
            "model_cache.artifact_invalid", "catalog artifact path is invalid"
        )
    if kind == "huggingface.file":
        from urllib.parse import quote

        # Catalog repositories are immutable HTTPS URLs.  Parse and bind the
        # repository path to the one trusted Hugging Face authority before
        # constructing the file URL; accepting the raw URL here would permit
        # catalog metadata to redirect the Controller to another host.
        try:
            parsed = urlsplit(repository)
            hostname = parsed.hostname
            port = parsed.port
        except (TypeError, ValueError) as error:
            raise ModelCacheResolutionError(
                "model_cache.source_invalid",
                "catalog Hugging Face repository is invalid",
            ) from error
        if parsed.scheme:
            if (
                parsed.scheme != "https"
                or hostname is None
                or hostname.lower().rstrip(".") != "huggingface.co"
                or port is not None
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or not parsed.path.startswith("/")
            ):
                raise ModelCacheResolutionError(
                    "model_cache.source_invalid",
                    "catalog Hugging Face repository must use canonical HTTPS",
                )
            repository_path = parsed.path.strip("/")
        else:
            # Older catalog fixtures store the repository as the immutable
            # Hugging Face ``namespace/name`` identifier.  It is safe to
            # normalize that bounded identifier to the canonical authority;
            # arbitrary URI repositories still take the guarded path above.
            repository_path = repository
        if not _valid_repository(repository_path):
            raise ModelCacheResolutionError(
                "model_cache.source_invalid",
                "catalog Hugging Face repository is invalid",
            )
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40,64}", revision):
            raise ModelCacheResolutionError(
                "model_cache.revision_invalid",
                "catalog artifact revision is not immutable",
            )
        source = (
            f"https://huggingface.co/{repository_path}/resolve/{revision}/"
            f"{quote(path, safe='/')}"
        )
    elif kind == "http.file":
        source = repository
    else:
        raise ModelCacheResolutionError(
            "model_cache.source_unsupported",
            "catalog artifact source cannot be downloaded by the NAS cache",
        )
    return source, str(revision) if revision is not None else None


def _valid_repository(value: str) -> bool:
    return (
        bool(
            re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}/"
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
                value,
            )
        )
    )


def _datetime(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else _datetime(value).astimezone(UTC).isoformat()


def _parse_iso(value: str) -> datetime:
    try:
        return _datetime(datetime.fromisoformat(value))
    except (TypeError, ValueError) as error:
        raise ModelCacheConflict(
            "model_cache.cursor_invalid", "cache cursor boundary is invalid"
        ) from error


class ModelCacheService:
    """Resolve, download, verify, repair and evict NAS model artifact sets."""

    def __init__(
        self,
        sessions: Session | sessionmaker[Session],
        root: Path,
        *,
        reserve_bytes: int = 10 * 1024**3,
        clock: callable | None = None,
        http_client: httpx.Client | None = None,
        fixture_sources: bool = False,
        trusted_source_hosts: Sequence[str] = ("huggingface.co",),
    ) -> None:
        if not isinstance(root, Path):
            root = Path(root)
        if not root.is_absolute() or any(part in {"", ".", ".."} for part in root.parts):
            raise ValueError("model cache root must be an absolute normalized path")
        if root.is_symlink():
            raise ValueError("model cache root must not be a symlink")
        if not isinstance(reserve_bytes, int) or isinstance(reserve_bytes, bool) or reserve_bytes < 0:
            raise ValueError("model cache reserve must be a non-negative integer")
        root.mkdir(parents=True, exist_ok=True, mode=0o750)
        for child in ("objects", "partials", "quarantine", "manifests"):
            directory = root / child
            if directory.is_symlink():
                raise ValueError("model cache storage directory must not be a symlink")
            directory.mkdir(mode=0o750, exist_ok=True)
        self._sessions = sessions
        self._root = root
        self._reserve_bytes = reserve_bytes
        self._clock = clock or (lambda: datetime.now(UTC))
        self._http = http_client
        # Local file and caller-supplied HTTP sources are useful for isolated
        # fixture tests, but are never enabled by the production constructor.
        # Production manifests are resolved from trusted catalog rows only.
        self._fixture_sources = fixture_sources
        self._trusted_source_hosts = frozenset(
            host.strip().lower().rstrip(".")
            for host in trusted_source_hosts
            if isinstance(host, str) and host.strip()
        )
        self._lock = threading.RLock()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def reserve_bytes(self) -> int:
        return self._reserve_bytes

    @contextmanager
    def _session(self, *, write: bool = False) -> Iterator[Session]:
        if isinstance(self._sessions, Session):
            yield self._sessions
            if write:
                self._sessions.flush()
            return
        if write:
            with self._sessions.begin() as session:
                yield session
        else:
            with self._sessions() as session:
                yield session

    def resolve_artifact_set(
        self,
        *,
        model_version_sha256: str | None = None,
        recipe_revision_sha256: str | None = None,
        recipe_revision_id: str | None = None,
        artifacts: Sequence[Mapping[str, object]] | None = None,
    ) -> ArtifactSetManifest:
        model_digest = _optional_digest(model_version_sha256)
        recipe_digest = _optional_digest(recipe_revision_sha256)
        if recipe_digest is not None and recipe_revision_id is not None:
            raise ModelCacheResolutionError(
                "model_cache.recipe_identity_ambiguous",
                "recipe revision digest and ID cannot both be supplied",
            )
        if artifacts is not None:
            if not self._fixture_sources:
                raise ModelCacheResolutionError(
                    "model_cache.fixture_sources_forbidden",
                    "caller-supplied artifact sources are only available to fixture services",
                )
            specs = tuple(
                self._artifact_from_input(value, model_version_sha256=model_digest)
                for value in artifacts
            )
            manifest = ArtifactSetManifest(
                model_version_sha256=model_digest,
                recipe_revision_sha256=recipe_digest,
                model_versions=(() if model_digest is None else (model_digest,)),
                artifacts=tuple(sorted(specs, key=lambda item: item.key)),
            )
            _validate_manifest(manifest)
            return manifest

        if model_digest is None and recipe_digest is None and recipe_revision_id is None:
            raise ModelCacheResolutionError(
                "model_cache.pin_required",
                "an exact model or recipe revision is required",
            )
        with self._session() as session:
            recipe_document: Mapping[str, object] | None = None
            if recipe_digest is not None or recipe_revision_id is not None:
                recipe_document, _resolved_recipe_id, resolved_recipe_digest = self._recipe_document(
                    session, recipe_digest, recipe_revision_id
                )
                if recipe_digest is not None and resolved_recipe_digest != recipe_digest:
                    raise ModelCacheResolutionError(
                        "model_cache.recipe_revision_missing",
                        "exact recipe revision is not resolved",
                    )
                recipe_digest = resolved_recipe_digest
                reference = recipe_document.get("model")
                candidate = (
                    reference.get("content_sha256")
                    if isinstance(reference, Mapping)
                    else None
                )
                if not isinstance(candidate, str):
                    raise ModelCacheResolutionError(
                        "model_cache.recipe_model_missing",
                        "recipe does not bind an exact model revision",
                    )
                if model_digest is not None and candidate != model_digest:
                    raise ModelCacheConflict(
                        "model_cache.pin_mismatch",
                        "recipe and requested model revisions do not match",
                    )
                model_digest = candidate
            if model_digest is None:
                raise ModelCacheResolutionError(
                    "model_cache.pin_required",
                    "an exact model revision is required after recipe resolution",
                )
            model_rows: dict[str, CatalogEntityRevision] = {}
            self._collect_model_versions(session, model_digest, model_rows)
            if recipe_document is not None:
                raw_dependencies = recipe_document.get("dependencies", ())
                if not isinstance(raw_dependencies, list):
                    raise ModelCacheResolutionError(
                        "model_cache.dependencies_invalid",
                        "recipe model dependencies are invalid",
                    )
                for reference in raw_dependencies:
                    if not isinstance(reference, Mapping) or not isinstance(
                        reference.get("content_sha256"), str
                    ):
                        raise ModelCacheResolutionError(
                            "model_cache.dependencies_invalid",
                            "recipe model dependency pin is invalid",
                        )
                    self._collect_model_versions(
                        session, reference["content_sha256"], model_rows
                    )
            specs: list[ArtifactSpec] = []
            model_ref: Mapping[str, object] | None = None
            for digest, row in sorted(model_rows.items()):
                if digest == model_digest:
                    model_ref = {
                        "kind": "model-version",
                        "publisher": row.entity.publisher,
                        "slug": row.entity.slug,
                        "content_sha256": digest,
                    }
                raw_artifacts = row.document.get("artifacts")
                if not isinstance(raw_artifacts, list):
                    raise ModelCacheResolutionError(
                        "model_cache.artifacts_missing",
                        "model revision does not declare its complete artifact inventory",
                    )
                for raw in raw_artifacts:
                    if not isinstance(raw, Mapping):
                        raise ModelCacheResolutionError(
                            "model_cache.artifact_invalid", "model artifact metadata is invalid"
                        )
                    specs.append(
                        self._artifact_from_catalog(
                            raw,
                            model_version_sha256=digest,
                        )
                    )
            manifest = ArtifactSetManifest(
                model_version_sha256=model_digest,
                recipe_revision_sha256=recipe_digest,
                model_versions=tuple(sorted(model_rows)),
                artifacts=tuple(sorted(specs, key=lambda item: item.key)),
                model_version_ref=model_ref,
            )
        _validate_manifest(manifest)
        return manifest

    def _recipe_document(
        self,
        session: Session,
        digest: str | None,
        revision_id: str | None,
    ) -> tuple[Mapping[str, object], str, str]:
        local = None
        if revision_id is not None:
            local = session.get(LocalRecipeRevision, revision_id)
            if local is None:
                greenfield = session.get(RecipeRevision, revision_id)
                if greenfield is not None:
                    return greenfield.content, greenfield.revision_id, greenfield.content_digest
        elif digest is not None:
            local = session.scalar(
                select(LocalRecipeRevision).where(
                    LocalRecipeRevision.content_sha256 == digest,
                    LocalRecipeRevision.lifecycle == "resolved",
                )
            )
            if local is None:
                greenfield = session.scalar(
                    select(RecipeRevision).where(RecipeRevision.content_digest == digest)
                )
                if greenfield is not None:
                    return greenfield.content, greenfield.revision_id, greenfield.content_digest
        if local is None or local.lifecycle != "resolved" or local.content_sha256 is None:
            raise ModelCacheResolutionError(
                "model_cache.recipe_revision_missing",
                "exact recipe revision is not resolved",
            )
        return local.document, local.id, local.content_sha256

    def _collect_model_versions(
        self,
        session: Session,
        digest: str,
        rows: dict[str, CatalogEntityRevision],
    ) -> None:
        if not isinstance(digest, str) or not _is_hex(digest) or len(digest) != 64:
            raise ModelCacheResolutionError(
                "model_cache.model_pin_invalid", "model dependency pin is invalid"
            )
        if digest in rows:
            return
        row = session.scalar(
            select(CatalogEntityRevision)
            .join(CatalogEntity)
            .where(
                CatalogEntity.kind == CatalogKind.MODEL_VERSION.value,
                CatalogEntityRevision.content_sha256 == digest,
                CatalogEntityRevision.lifecycle == "resolved",
            )
        )
        if row is None:
            raise ModelCacheResolutionError(
                "model_cache.model_revision_missing",
                "exact model revision is not resolved",
            )
        rows[digest] = row
        dependencies = row.document.get("dependencies", ())
        if not isinstance(dependencies, list):
            raise ModelCacheResolutionError(
                "model_cache.dependencies_invalid", "model dependencies are invalid"
            )
        if len(rows) > _MAX_ARTIFACTS:
            raise ModelCacheResolutionError(
                "model_cache.dependency_count", "model dependency set is too large"
            )
        for reference in dependencies:
            if not isinstance(reference, Mapping) or not isinstance(
                reference.get("content_sha256"), str
            ):
                raise ModelCacheResolutionError(
                    "model_cache.dependencies_invalid", "model dependency pin is invalid"
                )
            self._collect_model_versions(session, reference["content_sha256"], rows)

    def _artifact_from_catalog(
        self,
        value: Mapping[str, object],
        *,
        model_version_sha256: str,
    ) -> ArtifactSpec:
        raw_id = value.get("id")
        raw_path = value.get("path")
        raw_kind = value.get("kind")
        raw_digest = value.get("sha256")
        raw_bytes = value.get("download_bytes")
        roles = value.get("roles")
        if not isinstance(raw_id, str) or not isinstance(raw_path, str) or not isinstance(raw_kind, str):
            raise ModelCacheResolutionError(
                "model_cache.artifact_invalid", "catalog artifact identity is incomplete"
            )
        if not isinstance(raw_digest, str) or not isinstance(raw_bytes, int) or not isinstance(roles, list):
            raise ModelCacheResolutionError(
                "model_cache.artifact_invalid", "catalog artifact integrity metadata is incomplete"
            )
        if raw_kind != "huggingface.file" and not self._fixture_sources:
            raise ModelCacheResolutionError(
                "model_cache.source_untrusted",
                "production cache downloads require a trusted Hugging Face artifact reference",
            )
        source, revision = _source_for_catalog_artifact(value)
        spec = ArtifactSpec(
            key=f"artifact-{model_version_sha256[:12]}-{raw_id}",
            artifact_id=raw_id,
            path=raw_path,
            kind=raw_kind,
            repository=(
                None if value.get("repository") is None else str(value["repository"])
            ),
            source=source,
            revision=revision,
            sha256=raw_digest,
            expected_bytes=raw_bytes,
            roles=tuple(str(role) for role in roles),
            model_version_sha256=model_version_sha256,
        )
        _validate_artifact(spec)
        return spec

    def _artifact_from_input(
        self,
        value: Mapping[str, object],
        *,
        model_version_sha256: str | None,
    ) -> ArtifactSpec:
        try:
            artifact_id = str(value["id"])
            path = str(value["path"])
            kind = str(value["kind"])
            source_value = value.get("source") or value.get("source_uri")
            repository = value.get("repository")
            if (
                source_value is None
                and kind in {"http.file", "file"}
                and isinstance(repository, str)
            ):
                source_value = repository
            source = str(source_value)
            revision = (
                None if value.get("revision") is None else str(value["revision"])
            )
            raw_roles = value["roles"]
            if not isinstance(raw_roles, list):
                raise TypeError
            digest = str(value["sha256"])
            expected_bytes = int(value["download_bytes"])
        except (KeyError, TypeError, ValueError) as error:
            raise ModelCacheResolutionError(
                "model_cache.artifact_invalid", "cache artifact input is invalid"
            ) from error
        spec = ArtifactSpec(
            key=(
                f"artifact-{model_version_sha256[:12]}-{artifact_id}"
                if model_version_sha256 is not None
                else f"artifact-input-{artifact_id}"
            ),
            artifact_id=artifact_id,
            path=path,
            kind=kind,
            repository=(None if repository is None else str(repository)),
            source=source,
            revision=revision,
            sha256=digest,
            expected_bytes=expected_bytes,
            roles=tuple(str(role) for role in raw_roles),
            model_version_sha256=model_version_sha256,
        )
        _validate_artifact(spec)
        return spec

    def start_download(
        self,
        *,
        actor: str,
        request_key: str,
        plan_digest: str,
        artifact_set_sha256: str | None = None,
        model_version_sha256: str | None = None,
        recipe_revision_sha256: str | None = None,
        recipe_revision_id: str | None = None,
        artifacts: Sequence[Mapping[str, object]] | None = None,
        interrupt_after_bytes: int | None = None,
    ) -> CacheOperationView:
        request_key = _request_key(request_key)
        requested_plan = _optional_digest(plan_digest)
        if requested_plan is None:
            raise ModelCacheConflict("model_cache.plan_invalid", "download plan digest is invalid")
        manifest = self._resolve_requested_manifest(
            artifact_set_sha256=artifact_set_sha256,
            model_version_sha256=model_version_sha256,
            recipe_revision_sha256=recipe_revision_sha256,
            recipe_revision_id=recipe_revision_id,
            artifacts=artifacts,
        )
        set_digest = manifest.digest
        # A retry of the same idempotency key must return the original
        # operation even when its partial checkpoint has changed the current
        # preview's remaining-byte estimate.
        with self._lock, self._session() as session:
            existing = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == request_key
                )
            )
            if existing is not None:
                if (
                    existing.kind != "download"
                    or existing.payload.get("artifact_set_sha256") != set_digest
                    or existing.plan_digest != requested_plan
                ):
                    raise ModelCacheConflict(
                        "model_cache.request_key_reused",
                        "request key was already used for another cache operation",
                    )
                return self.get_operation(existing.id)
        preview = self._download_preview_for_manifest(manifest)
        if preview["plan_digest"] != requested_plan:
            raise ModelCacheConflict("model_cache.stale_plan", "download preview is stale")
        if preview["blockers"]:
            raise ModelCacheConflict(
                "model_cache.download_blocked",
                "; ".join(str(item) for item in preview["blockers"]),
            )
        transfer = preview.get("_transfer")
        if not isinstance(transfer, Mapping):
            transfer = self._transfer_state_for_manifest(manifest, force=False)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "source_policy": SOURCE_POLICY,
            "artifact_set_sha256": set_digest,
            "manifest": manifest.document(),
            "plan_digest": requested_plan,
            "transfer": dict(transfer),
        }
        with self._lock, self._session(write=True) as session:
            existing = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == request_key
                )
            )
            if existing is not None:
                if (
                    existing.kind != "download"
                    or existing.payload.get("artifact_set_sha256") != set_digest
                    or existing.plan_digest != requested_plan
                ):
                    raise ModelCacheConflict(
                        "model_cache.request_key_reused",
                        "request key was already used for another cache operation",
                    )
                operation_id = existing.id
            else:
                self._ensure_set(session, manifest)
                now = self._clock()
                operation = ModelCacheOperation(
                    request_key=request_key,
                    schema_version=SCHEMA_VERSION,
                    kind="download",
                    state="queued",
                    attempt=1,
                    artifact_set_sha256=set_digest,
                    plan_digest=requested_plan,
                    payload=payload,
                    progress=self._progress(
                        manifest,
                        phase="queued",
                        expected_bytes=int(transfer["total_bytes"]),
                    ),
                    actor=actor,
                    created_at=now,
                    updated_at=now,
                )
                session.add(operation)
                session.flush()
                operation_id = operation.id
        if interrupt_after_bytes is not None:
            self._run_download(
                operation_id,
                force=False,
                interrupt_after_bytes=interrupt_after_bytes,
            )
        return self.get_operation(operation_id)

    def _resolve_requested_manifest(
        self,
        *,
        artifact_set_sha256: str | None,
        model_version_sha256: str | None,
        recipe_revision_sha256: str | None,
        recipe_revision_id: str | None,
        artifacts: Sequence[Mapping[str, object]] | None,
    ) -> ArtifactSetManifest:
        requested_set = _optional_digest(artifact_set_sha256)
        if requested_set is not None and artifacts is None and (
            model_version_sha256 is None
            and recipe_revision_sha256 is None
            and recipe_revision_id is None
        ):
            return self._manifest_for_set(requested_set)
        manifest = self.resolve_artifact_set(
            model_version_sha256=model_version_sha256,
            recipe_revision_sha256=recipe_revision_sha256,
            recipe_revision_id=recipe_revision_id,
            artifacts=artifacts,
        )
        if requested_set is not None and manifest.digest != requested_set:
            raise ModelCacheConflict(
                "model_cache.pin_mismatch",
                "requested artifact-set identity does not match the resolved pins",
            )
        return manifest

    def download_preview(
        self,
        *,
        artifact_set_sha256: str | None = None,
        model_version_sha256: str | None = None,
        recipe_revision_sha256: str | None = None,
        recipe_revision_id: str | None = None,
        artifacts: Sequence[Mapping[str, object]] | None = None,
    ) -> dict[str, object]:
        manifest = self._resolve_requested_manifest(
            artifact_set_sha256=artifact_set_sha256,
            model_version_sha256=model_version_sha256,
            recipe_revision_sha256=recipe_revision_sha256,
            recipe_revision_id=recipe_revision_id,
            artifacts=artifacts,
        )
        return self._download_preview_for_manifest(manifest)

    def _download_preview_for_manifest(
        self, manifest: ArtifactSetManifest
    ) -> dict[str, object]:
        already_cached = 0
        for spec in _unique_artifacts(manifest.artifacts).values():
            if self._object_is_verified(spec):
                already_cached += spec.expected_bytes
        transfer = self._transfer_state_for_manifest(manifest, force=False)
        new_bytes = int(transfer["total_bytes"])
        storage = self.storage_summary()
        blockers = []
        if new_bytes > storage.available_bytes:
            blockers.append("insufficient-reserved-storage")
        plan = {
            "schema_version": SCHEMA_VERSION,
            "kind": "download",
            "artifact_set_sha256": manifest.digest,
            "manifest": manifest.document(),
            "already_cached_bytes": already_cached,
            "new_bytes": new_bytes,
            "source_policy": SOURCE_POLICY,
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "artifact_set_sha256": manifest.digest,
            "plan_digest": _sha256_json(plan),
            "source_policy": SOURCE_POLICY,
            "artifact_count": len(manifest.artifacts),
            "expected_bytes": manifest.expected_bytes,
            "already_cached_bytes": already_cached,
            "new_bytes": new_bytes,
            "blockers": blockers,
            "warnings": [],
            "_manifest": manifest,
            "_transfer": transfer,
        }

    def _partial_bytes(self, set_digest: str, spec: ArtifactSpec) -> int:
        """Return only a bounded, reusable partial checkpoint length."""
        partial = self._partial_path(set_digest, spec.sha256)
        try:
            if not partial.is_file() or partial.is_symlink():
                return 0
            size = partial.stat().st_size
        except OSError:
            return 0
        if size < 0 or size > spec.expected_bytes:
            return 0
        if size == spec.expected_bytes and not self._verify_file(partial, spec):
            return 0
        return size

    def _transfer_state_for_manifest(
        self,
        manifest: ArtifactSetManifest,
        *,
        force: bool,
    ) -> dict[str, object]:
        """Create the immutable planned transfer and per-object baselines."""
        artifacts: dict[str, dict[str, int]] = {}
        total_bytes = 0
        for digest, spec in _unique_artifacts(manifest.artifacts).items():
            baseline = (
                0
                if force
                else (
                    spec.expected_bytes
                    if self._object_is_verified(spec)
                    else self._partial_bytes(manifest.digest, spec)
                )
            )
            remaining = max(0, spec.expected_bytes - baseline)
            artifacts[digest] = {
                "baseline_bytes": baseline,
                "received_bytes": 0,
            }
            total_bytes += remaining
        return {
            "schema_version": SCHEMA_VERSION,
            "total_bytes": total_bytes,
            "artifacts": artifacts,
        }

    @staticmethod
    def _transfer_totals(payload: Mapping[str, object]) -> tuple[int | None, int]:
        raw_transfer = payload.get("transfer")
        if not isinstance(raw_transfer, Mapping):
            return None, 0
        raw_total = raw_transfer.get("total_bytes")
        total = raw_total if type(raw_total) is int and raw_total >= 0 else None
        raw_artifacts = raw_transfer.get("artifacts")
        if not isinstance(raw_artifacts, Mapping):
            return total, 0
        received = 0
        for raw_entry in raw_artifacts.values():
            if not isinstance(raw_entry, Mapping):
                continue
            value = raw_entry.get("received_bytes")
            if type(value) is int and value >= 0:
                received += value
        return total, received

    def _ensure_transfer_state(
        self,
        operation_id: str,
        manifest: ArtifactSetManifest,
        *,
        force: bool,
    ) -> dict[str, object]:
        """Persist a transfer ledger when resuming an older schema-2 row."""
        with self._session(write=True) as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None:
                raise ModelCacheNotFound(
                    "model_cache.operation_missing", "cache operation was not found"
                )
            raw = operation.payload.get("transfer")
            if isinstance(raw, Mapping):
                return dict(raw)
            transfer = self._transfer_state_for_manifest(manifest, force=force)
            operation.payload = dict(operation.payload) | {"transfer": transfer}
            return transfer

    def _operation_transfer_snapshot(
        self, operation_id: str
    ) -> tuple[int | None, int]:
        with self._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None:
                raise ModelCacheNotFound(
                    "model_cache.operation_missing", "cache operation was not found"
                )
            return self._transfer_totals(operation.payload)

    def _ensure_set(
        self,
        session: Session,
        manifest: ArtifactSetManifest,
    ) -> ModelCacheSet:
        set_digest = manifest.digest
        row = session.get(ModelCacheSet, set_digest)
        now = self._clock()
        if row is None:
            row = ModelCacheSet(
                artifact_set_sha256=set_digest,
                schema_version=SCHEMA_VERSION,
                model_version_sha256=manifest.model_version_sha256,
                recipe_revision_sha256=manifest.recipe_revision_sha256,
                manifest=manifest.document(),
                expected_bytes=manifest.expected_bytes,
                verified_bytes=0,
                state="incomplete",
                protected=False,
                protected_reasons=[],
                created_at=now,
                updated_at=now,
                verified_at=None,
                last_accessed_at=now,
                last_error=None,
            )
            session.add(row)
            session.flush()
        elif row.manifest != manifest.document():
            raise ModelCacheConflict(
                "model_cache.identity_conflict",
                "artifact-set digest resolves to different immutable content",
            )
        for spec in manifest.artifacts:
            artifact = session.get(ModelCacheArtifact, spec.sha256)
            if artifact is None:
                artifact = ModelCacheArtifact(
                    sha256=spec.sha256,
                    identity=spec.identity(),
                    storage_key=self._object_key(spec.sha256),
                    expected_bytes=spec.expected_bytes,
                    actual_bytes=0,
                    state="missing",
                    verified_at=None,
                    updated_at=now,
                )
                session.add(artifact)
                session.flush()
            elif artifact.expected_bytes != spec.expected_bytes:
                raise ModelCacheConflict(
                    "model_cache.digest_size_conflict",
                    "artifact digest is already bound to a different size",
                )
            membership = session.get(
                ModelCacheSetArtifact,
                {"artifact_set_sha256": set_digest, "artifact_key": spec.key},
            )
            if membership is None:
                session.add(
                    ModelCacheSetArtifact(
                        artifact_set_sha256=set_digest,
                        artifact_key=spec.key,
                        artifact_sha256=spec.sha256,
                        path=spec.path,
                        roles=list(spec.roles),
                    )
                )
        return row

    def _progress(
        self,
        manifest: ArtifactSetManifest,
        *,
        phase: str,
        completed_artifacts: int = 0,
        downloaded_bytes: int = 0,
        expected_bytes: int | None | object = _USE_MANIFEST_BYTES,
        current_artifact_key: str | None = None,
    ) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "phase": phase,
            "completed_artifacts": completed_artifacts,
            "total_artifacts": len(manifest.artifacts),
            "downloaded_bytes": downloaded_bytes,
            "expected_bytes": (
                manifest.expected_bytes
                if expected_bytes is _USE_MANIFEST_BYTES
                else expected_bytes
            ),
            "current_artifact_key": current_artifact_key,
        }

    def _run_download(
        self,
        operation_id: str,
        *,
        force: bool,
        interrupt_after_bytes: int | None = None,
    ) -> None:
        with self._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None:
                raise ModelCacheNotFound("model_cache.operation_missing", "cache operation was not found")
            operation_payload = dict(operation.payload)
            operation_set_digest = operation.artifact_set_sha256
            manifest = ArtifactSetManifest.from_document(
                operation_payload.get("manifest", {})
                if isinstance(operation_payload, Mapping)
                else {}
            )
            set_digest = operation_set_digest
        if set_digest is None:
            raise ModelCacheConflict("model_cache.set_missing", "cache operation has no artifact set")
        transfer = self._ensure_transfer_state(
            operation_id, manifest, force=force
        )
        planned_total = transfer.get("total_bytes")
        planned_total = (
            planned_total
            if type(planned_total) is int and planned_total >= 0
            else None
        )
        self._set_operation_state(operation_id, "running")
        completed = 0
        processed_digests: set[str] = set()
        try:
            with self._session(write=True) as session:
                self._ensure_set(
                    session,
                    manifest,
                )
            for spec in manifest.artifacts:
                self._set_operation_progress(
                    operation_id,
                    manifest,
                    phase="verifying" if force else "downloading",
                    completed_artifacts=completed,
                    downloaded_bytes=self._operation_transfer_snapshot(operation_id)[1],
                    expected_bytes=planned_total,
                    current_artifact_key=spec.key,
                )
                if spec.sha256 in processed_digests:
                    # Multiple logical entries may intentionally point at the
                    # same immutable object.  Count the entry for coverage,
                    # but never transfer or count its bytes twice.
                    completed += 1
                    continue
                if not force and self._object_is_verified(spec):
                    self._mark_artifact_verified(spec, set_digest)
                else:
                    # A forced repair always transfers a fresh payload.  The
                    # existing object is kept in place until the fresh part
                    # has passed both size and digest verification.
                    self._download_artifact(
                        spec,
                        set_digest,
                        operation_id=operation_id,
                        completed_artifacts=completed,
                        force=force,
                        interrupt_after_bytes=interrupt_after_bytes,
                    )
                processed_digests.add(spec.sha256)
                completed += 1
                # The transfer ledger records only bytes received from the
                # source during this operation.  Reused verified objects and
                # pre-existing partial checkpoints never contribute here.
            self._set_operation_progress(
                operation_id,
                manifest,
                phase="completed",
                completed_artifacts=completed,
                downloaded_bytes=self._operation_transfer_snapshot(operation_id)[1],
                expected_bytes=planned_total,
                current_artifact_key=None,
            )
            with self._session(write=True) as session:
                row = session.get(ModelCacheSet, set_digest)
                if row is not None:
                    row.state = "cached"
                    row.verified_bytes = manifest.expected_bytes
                    row.verified_at = self._clock()
                    row.updated_at = self._clock()
                    row.last_accessed_at = row.updated_at
                    row.last_error = None
            self._set_operation_state(
                operation_id,
                "succeeded",
                result={
                    "schema_version": SCHEMA_VERSION,
                    "artifact_set_sha256": set_digest,
                    "coverage": "complete",
                },
            )
        except InterruptedError as error:
            self._finish_partial(operation_id, set_digest, manifest, str(error) or "download interrupted")
        except (ModelCacheError, OSError, httpx.HTTPError, ValueError) as error:
            self._finish_failed(operation_id, set_digest, manifest, error)

    def _download_artifact(
        self,
        spec: ArtifactSpec,
        set_digest: str,
        *,
        operation_id: str,
        completed_artifacts: int,
        force: bool,
        interrupt_after_bytes: int | None,
    ) -> None:
        part = self._partial_path(set_digest, spec.sha256)
        part.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        if part.is_symlink():
            part.unlink(missing_ok=True)
        if force:
            # Repair has an independent transfer budget and must not inherit
            # bytes from a stale partial produced by another operation.
            part.unlink(missing_ok=True)
        offset = part.stat().st_size if part.exists() else 0
        if offset > spec.expected_bytes:
            part.unlink(missing_ok=True)
            offset = 0
        received = offset
        if offset == spec.expected_bytes and self._verify_file(part, spec):
            self._publish_object(spec, part)
            self._mark_artifact_verified(spec, set_digest)
            return
        if offset == spec.expected_bytes:
            part.unlink(missing_ok=True)
            received = 0
            offset = 0
        stream, effective_offset, close = self._open_source(spec, offset)
        if effective_offset != offset:
            received = effective_offset
        try:
            mode = "ab" if effective_offset else "wb"
            with part.open(mode) as output:
                while True:
                    if hasattr(stream, "read"):
                        chunk = stream.read(_CHUNK_BYTES)
                    else:
                        chunk = next(stream, b"")
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        chunk = bytes(chunk)
                    received += len(chunk)
                    if received > spec.expected_bytes:
                        raise ModelCacheStorageError(
                            "model_cache.source_size_mismatch",
                            "source returned more bytes than the immutable artifact pin",
                        )
                    output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                    if interrupt_after_bytes is not None and received >= interrupt_after_bytes:
                        self._checkpoint_artifact(
                            spec,
                            operation_id=operation_id,
                            set_digest=set_digest,
                            actual_bytes=received,
                            state="partial",
                            completed_artifacts=completed_artifacts,
                        )
                        raise InterruptedError("download interrupted at a durable checkpoint")
                    self._checkpoint_artifact(
                        spec,
                        operation_id=operation_id,
                        set_digest=set_digest,
                        actual_bytes=received,
                        state="partial",
                        completed_artifacts=completed_artifacts,
                    )
        finally:
            close()
        if received != spec.expected_bytes:
            self._checkpoint_artifact(
                spec,
                operation_id=operation_id,
                set_digest=set_digest,
                actual_bytes=received,
                state="partial",
            )
            raise ModelCacheStorageError(
                "model_cache.source_truncated",
                "source ended before the immutable artifact size",
            )
        if not self._verify_file(part, spec):
            self._checkpoint_artifact(
                spec,
                operation_id=operation_id,
                set_digest=set_digest,
                actual_bytes=received,
                state="corrupt",
            )
            raise ModelCacheStorageError(
                "model_cache.digest_mismatch",
                "downloaded artifact failed SHA-256 verification",
            )
        self._publish_object(spec, part)
        self._mark_artifact_verified(spec, set_digest)

    def _open_source(
        self, spec: ArtifactSpec, offset: int
    ) -> tuple[object, int, callable]:
        try:
            parsed = urlsplit(spec.source)
            hostname = parsed.hostname
            port = parsed.port
        except (TypeError, ValueError) as error:
            raise ModelCacheStorageError(
                "model_cache.source_invalid", "cache source URL is invalid"
            ) from error
        if parsed.scheme == "file":
            if not self._fixture_sources:
                raise ModelCacheStorageError(
                    "model_cache.source_untrusted",
                    "production cache downloads cannot read file sources",
                )
            path = Path(unquote(parsed.path))
            if path.is_symlink() or not path.is_file():
                raise ModelCacheStorageError(
                    "model_cache.source_unavailable", "cache file source is unavailable"
                )
            handle = path.open("rb")
            size = path.stat().st_size
            if offset > size:
                handle.close()
                raise ModelCacheStorageError(
                    "model_cache.source_size_mismatch", "cache file source is shorter than its checkpoint"
                )
            handle.seek(offset)
            return handle, offset, handle.close
        if not self._fixture_sources and (
            parsed.scheme != "https"
            or hostname is None
            or port is not None
            or hostname.lower().rstrip(".")
            not in self._trusted_source_hosts
            or _is_private_host(hostname)
        ):
            raise ModelCacheStorageError(
                "model_cache.source_untrusted",
                "production cache downloads require a trusted HTTPS artifact host",
            )
        client = self._http
        owns_client = client is None
        if client is None:
            client = httpx.Client(
                follow_redirects=False,
                timeout=httpx.Timeout(30.0),
                trust_env=False,
            )
        elif not self._fixture_sources and getattr(client, "follow_redirects", False):
            raise ModelCacheStorageError(
                "model_cache.redirect_forbidden",
                "production cache HTTP clients must not follow redirects",
            )
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        response = client.stream("GET", spec.source, headers=headers)
        response.__enter__()
        if response.status_code in {301, 302, 303, 307, 308}:
            response.close()
            if owns_client:
                client.close()
            raise ModelCacheStorageError(
                "model_cache.redirect_forbidden", "cache source redirects are not followed"
            )
        if response.status_code not in {200, 206}:
            status_code = response.status_code
            response.close()
            if owns_client:
                client.close()
            raise ModelCacheStorageError(
                "model_cache.source_unavailable",
                f"cache source request failed with status {status_code}",
            )
        effective_offset = offset
        if offset and response.status_code == 200:
            # The server ignored the range request; restart safely rather than
            # appending a complete payload to a checkpoint.
            response.close()
            response = client.stream("GET", spec.source)
            response.__enter__()
            if response.status_code != 200:
                status_code = response.status_code
                response.close()
                if owns_client:
                    client.close()
                raise ModelCacheStorageError(
                    "model_cache.source_unavailable",
                    f"cache source restart failed with status {status_code}",
                )
            effective_offset = 0
        if response.status_code == 206:
            content_range = response.headers.get("content-range", "")
            if not content_range.startswith(f"bytes {effective_offset}-"):
                response.close()
                if owns_client:
                    client.close()
                raise ModelCacheStorageError(
                    "model_cache.range_invalid", "cache source returned an invalid byte range"
                )
        return (
            response.iter_bytes(),
            effective_offset,
            lambda: (response.close(), client.close() if owns_client else None),
        )

    def _verify_file(self, path: Path, spec: ArtifactSpec) -> bool:
        if path.is_symlink() or not path.is_file():
            return False
        try:
            if path.stat().st_size != spec.expected_bytes:
                return False
            digest = hashlib.sha256()
            with path.open("rb") as source:
                while chunk := source.read(_CHUNK_BYTES):
                    digest.update(chunk)
            return digest.hexdigest() == spec.sha256
        except OSError:
            return False

    def _object_is_verified(self, spec: ArtifactSpec) -> bool:
        return self._verify_file(self._object_path(spec.sha256), spec)

    def _publish_object(self, spec: ArtifactSpec, part: Path) -> None:
        if not self._verify_file(part, spec):
            raise ModelCacheStorageError(
                "model_cache.digest_mismatch", "cache artifact failed verification"
            )
        target = self._object_path(spec.sha256)
        target.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        quarantined: Path | None = None
        if target.exists() or target.is_symlink():
            quarantined = self._quarantine_path(spec.sha256)
            os.replace(target, quarantined)
        try:
            os.replace(part, target)
            _fsync_directory(target.parent)
        except Exception:
            if quarantined is not None and (quarantined.exists() or quarantined.is_symlink()):
                os.replace(quarantined, target)
            raise
        if quarantined is not None:
            quarantined.unlink(missing_ok=True)

    def _checkpoint_artifact(
        self,
        spec: ArtifactSpec,
        *,
        operation_id: str,
        set_digest: str,
        actual_bytes: int,
        state: str,
        completed_artifacts: int = 0,
    ) -> None:
        now = self._clock()
        with self._session(write=True) as session:
            artifact = session.get(ModelCacheArtifact, spec.sha256)
            if artifact is not None:
                artifact.actual_bytes = actual_bytes
                artifact.state = state
                artifact.updated_at = now
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is not None:
                manifest = ArtifactSetManifest.from_document(operation.payload["manifest"])
                payload = dict(operation.payload)
                raw_transfer = payload.get("transfer")
                transfer = dict(raw_transfer) if isinstance(raw_transfer, Mapping) else {}
                raw_artifacts = transfer.get("artifacts")
                artifacts = dict(raw_artifacts) if isinstance(raw_artifacts, Mapping) else {}
                raw_entry = artifacts.get(spec.sha256)
                entry = dict(raw_entry) if isinstance(raw_entry, Mapping) else {}
                baseline = entry.get("baseline_bytes")
                baseline = baseline if type(baseline) is int and baseline >= 0 else 0
                previous_received = entry.get("received_bytes")
                previous_received = (
                    previous_received
                    if type(previous_received) is int and previous_received >= 0
                    else 0
                )
                entry["baseline_bytes"] = baseline
                entry["received_bytes"] = max(
                    previous_received, max(0, actual_bytes - baseline)
                )
                artifacts[spec.sha256] = entry
                transfer["schema_version"] = SCHEMA_VERSION
                transfer["artifacts"] = artifacts
                total = transfer.get("total_bytes")
                total = total if type(total) is int and total >= 0 else None
                received = sum(
                    value.get("received_bytes", 0)
                    for value in artifacts.values()
                    if isinstance(value, Mapping)
                    and type(value.get("received_bytes")) is int
                    and value.get("received_bytes", 0) >= 0
                )
                payload["transfer"] = transfer
                operation.payload = payload
                old_progress = operation.progress if isinstance(operation.progress, Mapping) else {}
                old_downloaded = old_progress.get("downloaded_bytes")
                old_downloaded = old_downloaded if type(old_downloaded) is int and old_downloaded >= 0 else 0
                old_completed = old_progress.get("completed_artifacts")
                old_completed = old_completed if type(old_completed) is int and old_completed >= 0 else 0
                operation.state = "running" if state != "partial" else "partial"
                operation.progress = self._progress(
                    manifest,
                    phase="downloading" if state == "partial" else "verifying",
                    completed_artifacts=max(old_completed, completed_artifacts),
                    downloaded_bytes=max(old_downloaded, received),
                    expected_bytes=total,
                    current_artifact_key=spec.key,
                )
                operation.current_artifact_key = spec.key
                operation.updated_at = now
            row = session.get(ModelCacheSet, set_digest)
            if row is not None:
                row.state = "downloading" if state == "partial" else "verifying"
                row.verified_bytes = self._verified_bytes(session, set_digest)
                row.updated_at = now

    def _mark_artifact_verified(self, spec: ArtifactSpec, set_digest: str) -> None:
        now = self._clock()
        with self._session(write=True) as session:
            artifact = session.get(ModelCacheArtifact, spec.sha256)
            if artifact is not None:
                artifact.actual_bytes = spec.expected_bytes
                artifact.state = "verified"
                artifact.verified_at = now
                artifact.updated_at = now
            row = session.get(ModelCacheSet, set_digest)
            if row is not None:
                row.verified_bytes = self._verified_bytes(session, set_digest)
                row.updated_at = now

    def _verified_bytes(self, session: Session, set_digest: str) -> int:
        rows = session.scalars(
            select(ModelCacheSetArtifact).where(
                ModelCacheSetArtifact.artifact_set_sha256 == set_digest
            )
        )
        total = 0
        seen: set[str] = set()
        for membership in rows:
            if membership.artifact_sha256 in seen:
                continue
            seen.add(membership.artifact_sha256)
            artifact = session.get(ModelCacheArtifact, membership.artifact_sha256)
            if artifact is not None and artifact.state == "verified":
                total += artifact.expected_bytes
        return total

    def _finish_partial(
        self,
        operation_id: str,
        set_digest: str,
        manifest: ArtifactSetManifest,
        detail: str,
    ) -> None:
        now = self._clock()
        with self._session(write=True) as session:
            row = session.get(ModelCacheSet, set_digest)
            if row is not None:
                row.state = "incomplete"
                row.verified_bytes = self._verified_bytes(session, set_digest)
                row.updated_at = now
                row.last_error = detail[:512]
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is not None:
                operation.state = "partial"
                operation.last_error = detail[:512]
                _total, received = self._transfer_totals(operation.payload)
                operation.progress = self._progress(
                    manifest,
                    phase="downloading",
                    completed_artifacts=(
                        operation.progress.get("completed_artifacts", 0)
                        if isinstance(operation.progress, Mapping)
                        else 0
                    ),
                    downloaded_bytes=received,
                    expected_bytes=_total,
                    current_artifact_key=operation.current_artifact_key,
                )
                operation.updated_at = now

    def _finish_failed(
        self,
        operation_id: str,
        set_digest: str,
        manifest: ArtifactSetManifest,
        error: BaseException,
    ) -> None:
        detail = (
            error.detail
            if isinstance(error, ModelCacheError)
            else f"{type(error).__name__}: {str(error)[:400]}"
        )
        now = self._clock()
        with self._session(write=True) as session:
            row = session.get(ModelCacheSet, set_digest)
            if row is not None:
                row.verified_bytes = self._verified_bytes(session, set_digest)
                all_valid = all(
                    self._object_is_verified(ArtifactSpec.from_manifest(item))
                    for item in manifest.document()["artifacts"]
                )
                row.state = "cached" if all_valid else "needs-repair"
                row.updated_at = now
                row.last_error = detail[:512]
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is not None:
                operation.state = "failed"
                operation.last_error = detail[:512]
                operation.updated_at = now
                operation.completed_at = now

    def _set_operation_state(
        self,
        operation_id: str,
        state: str,
        *,
        result: Mapping[str, object] | None = None,
    ) -> None:
        now = self._clock()
        with self._session(write=True) as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None:
                raise ModelCacheNotFound("model_cache.operation_missing", "cache operation was not found")
            if state == "running":
                if operation.state in {"partial", "failed"}:
                    operation.attempt = int(operation.attempt) + 1
                else:
                    operation.attempt = max(1, int(operation.attempt))
            operation.state = state
            operation.updated_at = now
            if result is not None:
                operation.payload = dict(operation.payload) | {"result": dict(result)}
            if state in {"succeeded", "failed", "cancelled"}:
                operation.completed_at = now

    def _set_operation_progress(
        self,
        operation_id: str,
        manifest: ArtifactSetManifest,
        *,
        phase: str,
        completed_artifacts: int,
        downloaded_bytes: int,
        current_artifact_key: str | None,
        expected_bytes: int | None = None,
    ) -> None:
        now = self._clock()
        with self._session(write=True) as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None:
                raise ModelCacheNotFound("model_cache.operation_missing", "cache operation was not found")
            old_progress = operation.progress if isinstance(operation.progress, Mapping) else {}
            old_downloaded = old_progress.get("downloaded_bytes")
            old_downloaded = old_downloaded if type(old_downloaded) is int and old_downloaded >= 0 else 0
            old_completed = old_progress.get("completed_artifacts")
            old_completed = old_completed if type(old_completed) is int and old_completed >= 0 else 0
            operation.progress = self._progress(
                manifest,
                phase=phase,
                completed_artifacts=max(old_completed, completed_artifacts),
                downloaded_bytes=max(old_downloaded, downloaded_bytes),
                expected_bytes=expected_bytes,
                current_artifact_key=current_artifact_key,
            )
            operation.current_artifact_key = current_artifact_key
            operation.state = "running"
            operation.updated_at = now

    def get_operation(self, operation_id: str) -> CacheOperationView:
        with self._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None:
                raise ModelCacheNotFound("model_cache.operation_missing", "cache operation was not found")
            return self._operation_view(operation)

    def list_operations(self, *, limit: int = 100) -> tuple[CacheOperationView, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("cache operation limit is invalid")
        with self._session() as session:
            rows = session.scalars(
                select(ModelCacheOperation)
                .order_by(ModelCacheOperation.created_at.desc(), ModelCacheOperation.id.desc())
                .limit(limit)
            )
            return tuple(self._operation_view(row) for row in rows)

    def operations_page(
        self,
        *,
        limit: int = 100,
        boundary: tuple[str, str] | None = None,
    ) -> dict[str, object]:
        """Return a stable created-at/id ordered page and raw next boundary."""
        if not 1 <= limit <= 100:
            raise ValueError("cache operation limit is invalid")
        with self._session() as session:
            rows = list(
                session.scalars(
                    select(ModelCacheOperation).order_by(
                        ModelCacheOperation.created_at.desc(),
                        ModelCacheOperation.id.desc(),
                    )
                )
            )
        total = len(rows)
        start = 0
        if boundary is not None:
            boundary_time = _parse_iso(boundary[0])
            for index, row in enumerate(rows):
                if (
                    _datetime(row.created_at) == boundary_time
                    and row.id == boundary[1]
                ):
                    start = index + 1
                    break
            else:
                raise ModelCacheConflict(
                    "model_cache.cursor_invalid", "operation cursor boundary is stale"
                )
        page = rows[start : start + limit]
        next_boundary = None
        if start + limit < total and page:
            last = page[-1]
            next_boundary = (_iso(last.created_at) or "", last.id)
        return {
            "schema_version": SCHEMA_VERSION,
            "operations": tuple(self._operation_view(row) for row in page),
            "total": total,
            "_next_boundary": next_boundary,
        }

    @staticmethod
    def _operation_view(operation: ModelCacheOperation) -> CacheOperationView:
        result = operation.payload.get("result") if isinstance(operation.payload, Mapping) else None
        return CacheOperationView(
            id=operation.id,
            request_key=operation.request_key,
            kind=operation.kind,
            state=operation.state,
            attempt=int(operation.attempt),
            artifact_set_sha256=operation.artifact_set_sha256,
            plan_digest=operation.plan_digest,
            progress=dict(operation.progress),
            result=(dict(result) if isinstance(result, Mapping) else None),
            last_error=operation.last_error,
            created_at=_iso(operation.created_at) or "",
            updated_at=_iso(operation.updated_at) or "",
            completed_at=_iso(operation.completed_at),
        )

    def resume_operations(self, *, limit: int = 16) -> int:
        """Return durable cache work for the Controller worker to resume.

        Startup must not perform network or disk transfers inline.  The
        worker calls :meth:`run_pending` after it has claimed its process
        loop, so an API restart only discovers outstanding work here.
        """
        if not 1 <= limit <= 100:
            raise ValueError("cache operation limit is invalid")
        with self._session() as session:
            count = int(
                session.scalar(
                    select(func.count())
                    .select_from(ModelCacheOperation)
                    .where(
                        ModelCacheOperation.kind.in_(
                            ["download", "repair", "evict"]
                        )
                    )
                    .where(
                        ModelCacheOperation.state.in_(
                            ["queued", "running", "partial"]
                        )
                    )
                )
            )
        return min(count, limit)

    def run_pending(self, *, limit: int = 1) -> int:
        """Process a bounded batch of durable cache operations in the worker."""
        if not 1 <= limit <= 16:
            raise ValueError("cache worker batch limit is invalid")
        with self._lock:
            with self._session() as session:
                rows = list(
                    session.scalars(
                        select(ModelCacheOperation)
                        .where(
                            ModelCacheOperation.kind.in_(
                                ["download", "repair", "evict"]
                            )
                        )
                        .where(
                            ModelCacheOperation.state.in_(
                                ["queued", "running", "partial"]
                            )
                        )
                        .order_by(
                            ModelCacheOperation.updated_at,
                            ModelCacheOperation.id,
                        )
                        .limit(limit)
                    )
                )
            for row in rows:
                if row.kind == "evict":
                    self._run_eviction(row.id)
                else:
                    self._run_download(row.id, force=row.kind == "repair")
            return len(rows)

    def repair_preview(self, artifact_set_sha256: str) -> dict[str, object]:
        digest = _optional_digest(artifact_set_sha256)
        assert digest is not None
        entry = self.get_entry(digest)
        plan = {
            "schema_version": SCHEMA_VERSION,
            "kind": "repair",
            "artifact_set_sha256": digest,
            "artifacts": [item["sha256"] for item in entry["artifacts"]],
            "source_policy": SOURCE_POLICY,
        }
        plan_digest = _sha256_json(plan)
        return {
            "schema_version": SCHEMA_VERSION,
            "artifact_set_sha256": digest,
            "plan_digest": plan_digest,
            "source_policy": SOURCE_POLICY,
            "artifact_count": len(entry["artifacts"]),
            "current_state": entry["state"],
            "expected_bytes": entry["expected_bytes"],
            "verified_bytes": entry["verified_bytes"],
        }

    def start_repair(
        self,
        *,
        actor: str,
        request_key: str,
        artifact_set_sha256: str,
        plan_digest: str,
    ) -> CacheOperationView:
        digest = _optional_digest(artifact_set_sha256)
        requested_plan = _optional_digest(plan_digest)
        assert digest is not None and requested_plan is not None
        preview = self.repair_preview(digest)
        if preview["plan_digest"] != requested_plan:
            raise ModelCacheConflict("model_cache.stale_plan", "repair preview is stale")
        request_key = _request_key(request_key)
        manifest = self._manifest_for_set(digest)
        transfer = self._transfer_state_for_manifest(manifest, force=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "source_policy": SOURCE_POLICY,
            "artifact_set_sha256": digest,
            "manifest": manifest.document(),
            "plan_digest": requested_plan,
            "transfer": transfer,
        }
        with self._lock, self._session(write=True) as session:
            existing = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == request_key
                )
            )
            if existing is not None:
                if existing.kind != "repair" or existing.plan_digest != requested_plan:
                    raise ModelCacheConflict(
                        "model_cache.request_key_reused",
                        "request key was already used for another cache operation",
                    )
                operation_id = existing.id
            else:
                operation = ModelCacheOperation(
                    request_key=request_key,
                    schema_version=SCHEMA_VERSION,
                    kind="repair",
                    state="queued",
                    attempt=1,
                    artifact_set_sha256=digest,
                    plan_digest=requested_plan,
                    payload=payload,
                    progress=self._progress(
                        manifest,
                        phase="queued",
                        expected_bytes=int(transfer["total_bytes"]),
                    ),
                    actor=actor,
                    created_at=self._clock(),
                    updated_at=self._clock(),
                )
                session.add(operation)
                session.flush()
                operation_id = operation.id
        return self.get_operation(operation_id)

    def _manifest_for_set(self, digest: str) -> ArtifactSetManifest:
        with self._session() as session:
            row = session.get(ModelCacheSet, digest)
            if row is None:
                raise ModelCacheNotFound("model_cache.entry_missing", "cache entry was not found")
            manifest = ArtifactSetManifest.from_document(row.manifest)
            if manifest.digest != digest:
                raise ModelCacheConflict(
                    "model_cache.identity_conflict",
                    "persisted cache manifest does not match its artifact-set identity",
                )
            return manifest

    def manifest_for_artifact_set(self, artifact_set_sha256: str) -> ArtifactSetManifest:
        """Return the persisted immutable manifest for an exact artifact set.

        Consumers that prepare or distribute a model use this boundary rather
        than rebuilding an identity from display metadata.  The persisted
        manifest is re-hashed before it is returned, so a database row with a
        mismatched primary key cannot become a trusted source descriptor.
        """
        digest = _optional_digest(artifact_set_sha256)
        assert digest is not None
        return self._manifest_for_set(digest)

    def preparation_evidence(self, artifact_set_sha256: str) -> dict[str, object]:
        """Project exact model preparation evidence for run/profile adapters.

        The cache owns Controller-side model bytes only.  ``targets`` stays
        empty because target readiness is established by the distribution
        worker after agent-authenticated transfer and verification.
        """
        digest = _optional_digest(artifact_set_sha256)
        assert digest is not None
        entry = self.get_entry(digest)
        manifest = self.manifest_for_artifact_set(digest)
        if manifest.model_version_sha256 is None:
            raise ModelCacheResolutionError(
                "model_cache.model_pin_missing",
                "preparation evidence requires an exact primary model revision",
            )
        dependencies = sorted(
            value
            for value in manifest.model_versions
            if value != manifest.model_version_sha256
        )
        expected_bytes = int(entry["expected_bytes"])
        verified_bytes = int(entry["verified_bytes"])
        complete = entry["coverage"] == "complete"
        state = str(entry["state"])
        controller_state = {
            "cached": "ready",
            "incomplete": "preparing",
            "downloading": "preparing",
            "verifying": "verifying",
            "needs-repair": "failed",
            "failed": "failed",
        }.get(state, "unknown")
        reason = None
        if controller_state in {"failed", "unknown"}:
            reason = str(entry.get("last_error") or "model cache is not complete")
        return {
            "artifact_set_sha256": digest,
            "model_version_sha256": manifest.model_version_sha256,
            "recipe_revision_sha256": manifest.recipe_revision_sha256,
            "artifact_count": len(manifest.artifacts),
            "artifact_set_bytes": expected_bytes,
            "dependency_model_version_sha256": dependencies,
            "completeness": "complete" if complete else "incomplete",
            "controller": {
                "state": controller_state,
                "expected_bytes": expected_bytes,
                "verified_bytes": verified_bytes,
                "missing_bytes": max(0, expected_bytes - verified_bytes),
                "verified_sha256": digest if complete else None,
                "verified_at": entry.get("verified_at"),
                "source": "nas-cache",
                "reason": reason,
            },
            "targets": [],
        }

    def activity_operations(
        self,
        *,
        after: tuple[datetime, str] | None = None,
        limit: int = 101,
        state: str | None = None,
        node_id: str | None = None,
    ) -> dict[str, object]:
        """Return cache operations for the global Activity provider seam.

        Cache work is Controller/NAS scoped and therefore has no Spark node
        IDs.  A node filter consequently returns an empty page while still
        reporting the unfiltered-by-cursor total for the requested state.
        ``after`` is the already authenticated global activity boundary.
        """
        if not 1 <= limit <= 101:
            raise ValueError("operation provider page limit is invalid")
        if state is not None and (not isinstance(state, str) or not state.strip()):
            raise ValueError("operation state filter is invalid")
        if node_id is not None:
            return {"operations": (), "total": 0, "_next_boundary": None}
        allowed_states = {
            "queued",
            "running",
            "partial",
            "succeeded",
            "failed",
            "cancelled",
        }
        if state is not None and state not in allowed_states:
            return {"operations": (), "total": 0, "_next_boundary": None}
        with self._session() as session:
            filters = []
            if state is not None:
                filters.append(ModelCacheOperation.state == state)
            if after is not None:
                boundary_time, boundary_id = after
                boundary_time = _datetime(boundary_time)
                filters.append(
                    (ModelCacheOperation.created_at < boundary_time)
                    | (
                        (ModelCacheOperation.created_at == boundary_time)
                        & (ModelCacheOperation.id < boundary_id)
                    )
                )
            rows = list(
                session.scalars(
                    select(ModelCacheOperation)
                    .where(*filters)
                    .order_by(
                        ModelCacheOperation.created_at.desc(),
                        ModelCacheOperation.id.desc(),
                    )
                    # Fetch one sentinel row so the provider can expose a
                    # stable boundary instead of silently truncating pages.
                    .limit(limit + 1)
                )
            )
            has_more = len(rows) > limit
            rows = rows[:limit]
            total_filters = []
            if state is not None:
                total_filters.append(ModelCacheOperation.state == state)
            total = int(
                session.scalar(
                    select(func.count())
                    .select_from(ModelCacheOperation)
                    .where(*total_filters)
                )
                or 0
            )
        next_boundary = None
        if has_more and rows:
            last = rows[-1]
            next_boundary = (_iso(last.created_at) or "", last.id)
        return {
            "operations": tuple(self._operation_view(row) for row in rows),
            "total": total,
            "_next_boundary": next_boundary,
        }

    def verified_artifact_file(
        self,
        artifact_set_sha256: str,
        artifact_sha256: str,
        artifact_path: str,
    ) -> tuple[Path, int, str]:
        """Return one immutable object only after complete-set verification.

        This is the Controller-to-agent serving seam.  The caller receives a
        content-addressed path and must stream it from the returned file
        descriptor/path; no caller-controlled filesystem path is accepted.
        """
        set_digest = _optional_digest(artifact_set_sha256)
        object_digest = _optional_digest(artifact_sha256)
        if set_digest is None or object_digest is None or not _valid_relative_path(artifact_path):
            raise ModelCacheNotFound(
                "model_cache.artifact_missing", "verified cache artifact was not found"
            )
        manifest = self._manifest_for_set(set_digest)
        if not all(self._object_is_verified(spec) for spec in manifest.artifacts):
            raise ModelCacheConflict(
                "model_cache.coverage_incomplete",
                "cache artifact set is not completely verified",
            )
        spec = next(
            (
                value
                for value in manifest.artifacts
                if value.sha256 == object_digest and value.path == artifact_path
            ),
            None,
        )
        if spec is None:
            raise ModelCacheNotFound(
                "model_cache.artifact_missing", "verified cache artifact was not found"
            )
        path = self._object_path(spec.sha256)
        if path.is_symlink() or not path.is_file() or not self._verify_file(path, spec):
            raise ModelCacheConflict(
                "model_cache.artifact_unverified",
                "cache artifact is no longer verified",
            )
        return path, spec.expected_bytes, spec.sha256

    def resolve_verified_artifact_set(
        self, artifact_set_sha256: str
    ) -> tuple[dict[str, object], ...]:
        """Describe every verified object in a complete immutable set.

        The distribution worker consumes these descriptors to copy one
        content-addressed object to one or more agents.  It never receives a
        source URL or a caller-controlled path from this adapter.
        """
        digest = _optional_digest(artifact_set_sha256)
        if digest is None:
            raise ModelCacheNotFound(
                "model_cache.entry_missing", "cache entry was not found"
            )
        manifest = self._manifest_for_set(digest)
        descriptors = []
        for spec in manifest.artifacts:
            path, size, object_digest = self.verified_artifact_file(
                digest, spec.sha256, spec.path
            )
            descriptors.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "artifact_set_sha256": digest,
                    "artifact_key": spec.key,
                    "path": spec.path,
                    "sha256": object_digest,
                    "bytes": size,
                    "storage_key": self._object_key(object_digest),
                    "file": path,
                    "roles": list(spec.roles),
                }
            )
        return tuple(descriptors)

    def read_verified_artifact(
        self,
        artifact_set_sha256: str,
        artifact_sha256: str,
        artifact_path: str,
        *,
        offset: int = 0,
        maximum_bytes: int = 8 * 1024 * 1024,
    ) -> bytes:
        """Read a bounded range from a complete verified cache set."""
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or not isinstance(maximum_bytes, int)
            or isinstance(maximum_bytes, bool)
            or not 0 < maximum_bytes <= 8 * 1024 * 1024
        ):
            raise ValueError("verified artifact read bounds are invalid")
        path, size, _digest = self.verified_artifact_file(
            artifact_set_sha256, artifact_sha256, artifact_path
        )
        if offset >= size:
            return b""
        with path.open("rb") as source:
            source.seek(offset)
            return source.read(min(maximum_bytes, size - offset))

    def get_entry(self, artifact_set_sha256: str) -> dict[str, object]:
        digest = _optional_digest(artifact_set_sha256)
        assert digest is not None
        self.reconcile_storage()
        with self._session(write=True) as session:
            row = session.get(ModelCacheSet, digest)
            if row is None:
                raise ModelCacheNotFound("model_cache.entry_missing", "cache entry was not found")
            self._refresh_protection(session, row)
            manifest = ArtifactSetManifest.from_document(row.manifest)
            artifacts = []
            unique_bytes = 0
            seen: set[str] = set()
            for spec in manifest.artifacts:
                cache_artifact = session.get(ModelCacheArtifact, spec.sha256)
                state = "missing"
                actual = 0
                if cache_artifact is not None:
                    state = cache_artifact.state
                    actual = cache_artifact.actual_bytes
                    if spec.sha256 not in seen and state == "verified":
                        unique_bytes += spec.expected_bytes
                        seen.add(spec.sha256)
                artifacts.append(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "key": spec.key,
                        "id": spec.artifact_id,
                        "path": spec.path,
                        "sha256": spec.sha256,
                        "expected_bytes": spec.expected_bytes,
                        "actual_bytes": actual,
                        "roles": list(spec.roles),
                        "state": state,
                        "source": spec.source,
                    }
                )
            update = self._update_flags(session, row, manifest)
            return {
                "schema_version": SCHEMA_VERSION,
                "artifact_set_sha256": digest,
                "model_version_sha256": row.model_version_sha256,
                "recipe_revision_sha256": row.recipe_revision_sha256,
                "state": row.state,
                "coverage": "complete" if row.state == "cached" else "incomplete",
                "expected_bytes": row.expected_bytes,
                "verified_bytes": row.verified_bytes,
                "unique_bytes": unique_bytes,
                "artifacts": artifacts,
                "protected": bool(row.protected),
                "protected_reasons": list(row.protected_reasons or ()),
                "update_available": update[0],
                "recipe_update_available": update[1],
                "created_at": _iso(row.created_at) or "",
                "updated_at": _iso(row.updated_at) or "",
                "verified_at": _iso(row.verified_at),
                "last_error": row.last_error,
            }

    def inventory(
        self,
        *,
        limit: int = 100,
        boundary: tuple[str, str] | None = None,
    ) -> dict[str, object]:
        if not 1 <= limit <= 100:
            raise ValueError("cache entry limit is invalid")
        self.reconcile_storage()
        with self._session() as session:
            rows = list(
                session.scalars(
                    select(ModelCacheSet)
                    .order_by(ModelCacheSet.updated_at.desc(), ModelCacheSet.artifact_set_sha256.desc())
                )
            )
        total = len(rows)
        start = 0
        if boundary is not None:
            boundary_time = _parse_iso(boundary[0])
            for index, row in enumerate(rows):
                if (
                    _datetime(row.updated_at) == boundary_time
                    and row.artifact_set_sha256 == boundary[1]
                ):
                    start = index + 1
                    break
            else:
                raise ModelCacheConflict(
                    "model_cache.cursor_invalid", "cache inventory cursor boundary is stale"
                )
        page = rows[start : start + limit]
        entries = [self.get_entry(row.artifact_set_sha256) for row in page]
        next_boundary = None
        if start + limit < total and page:
            last = page[-1]
            next_boundary = (_iso(last.updated_at) or "", last.artifact_set_sha256)
        return {
            "schema_version": SCHEMA_VERSION,
            "source_policy": SOURCE_POLICY,
            "entries": entries,
            "storage": self.storage_summary().document(),
            "total": total,
            "_next_boundary": next_boundary,
        }

    def reconcile_storage(self) -> dict[str, object]:
        with self._lock:
            with self._session(write=True) as session:
                rows = list(session.scalars(select(ModelCacheArtifact)))
                for artifact in rows:
                    path = self._object_path(artifact.sha256)
                    actual = path.stat().st_size if path.exists() and not path.is_symlink() else 0
                    if (
                        actual == artifact.expected_bytes
                        and path.is_file()
                        and not path.is_symlink()
                        and self._verify_digest(path, artifact.sha256)
                    ):
                        artifact.state = "verified"
                        artifact.actual_bytes = actual
                    else:
                        artifact.state = "missing" if actual == 0 else "corrupt"
                        artifact.actual_bytes = min(actual, artifact.expected_bytes)
                    artifact.updated_at = self._clock()
                sets = list(session.scalars(select(ModelCacheSet)))
                for row in sets:
                    manifest = ArtifactSetManifest.from_document(row.manifest)
                    verified = self._verified_bytes(session, row.artifact_set_sha256)
                    row.verified_bytes = verified
                    if row.state not in {"downloading", "verifying"}:
                        row.state = "cached" if verified == manifest.expected_bytes else "needs-repair"
                    row.updated_at = self._clock()
                    self._refresh_protection(session, row)
            return self.storage_summary().document()

    def _verify_digest(self, path: Path, digest: str) -> bool:
        try:
            hasher = hashlib.sha256()
            with path.open("rb") as source:
                while chunk := source.read(_CHUNK_BYTES):
                    hasher.update(chunk)
            return hasher.hexdigest() == digest
        except OSError:
            return False

    def _refresh_protection(self, session: Session, row: ModelCacheSet) -> None:
        # Protection is a projection of durable references. Recompute it from
        # those references so removal of the last reference makes a set
        # evictable without retaining an old flag.
        reasons: set[str] = set()
        if row.model_version_sha256 is not None:
            installation = session.scalar(
                select(RecipeInstallation.id).where(
                    RecipeInstallation.model_version_sha256 == row.model_version_sha256,
                    RecipeInstallation.state.in_(["planned", "installing", "installed", "partial"]),
                )
            )
            if installation is not None:
                reasons.add("recipe-installation")
            run = session.scalar(
                select(RecipeRun.id)
                .join(RecipeInstallation, RecipeRun.installation_id == RecipeInstallation.id)
                .where(
                    RecipeInstallation.model_version_sha256 == row.model_version_sha256,
                    RecipeRun.state.in_(["planned", "starting", "running"]),
                )
            )
            if run is not None:
                reasons.add("running-model")
        for profile in session.scalars(select(FleetProfile)):
            if _contains_digest(
                profile.assignments,
                row.model_version_sha256,
                row.recipe_revision_sha256,
            ) or self._profile_references_cache(session, profile, row):
                reasons.add("saved-profile")
        row.protected = bool(reasons)
        row.protected_reasons = sorted(reasons)

    @staticmethod
    def _profile_references_cache(
        session: Session,
        profile: FleetProfile,
        row: ModelCacheSet,
    ) -> bool:
        """Resolve profile recipe IDs before deciding a cache set is evictable."""
        assignments = profile.assignments
        if not isinstance(assignments, list):
            return False
        for assignment in assignments:
            if not isinstance(assignment, Mapping):
                continue
            revision_id = assignment.get("recipe_revision_id")
            if not isinstance(revision_id, str) or not revision_id:
                continue
            revision = session.get(LocalRecipeRevision, revision_id)
            if revision is None or revision.lifecycle != "resolved":
                continue
            if (
                row.recipe_revision_sha256 is not None
                and revision.content_sha256 == row.recipe_revision_sha256
            ):
                return True
            if row.model_version_sha256 is None:
                continue
            document = revision.document
            model = document.get("model") if isinstance(document, Mapping) else None
            if (
                isinstance(model, Mapping)
                and model.get("content_sha256") == row.model_version_sha256
            ):
                return True
            dependencies = (
                document.get("dependencies", ())
                if isinstance(document, Mapping)
                else ()
            )
            if isinstance(dependencies, list) and any(
                isinstance(reference, Mapping)
                and reference.get("content_sha256") == row.model_version_sha256
                for reference in dependencies
            ):
                return True
        return False

    def _update_flags(
        self, session: Session, row: ModelCacheSet, manifest: ArtifactSetManifest
    ) -> tuple[bool, bool]:
        model_update = False
        recipe_update = False
        ref = manifest.model_version_ref
        if isinstance(ref, Mapping):
            entity = session.scalar(
                select(CatalogEntity).where(
                    CatalogEntity.kind == CatalogKind.MODEL_VERSION.value,
                    CatalogEntity.publisher == ref.get("publisher"),
                    CatalogEntity.slug == ref.get("slug"),
                )
            )
            if entity is not None:
                latest = session.scalar(
                    select(CatalogEntityRevision)
                    .where(
                        CatalogEntityRevision.entity_id == entity.id,
                        CatalogEntityRevision.lifecycle == "resolved",
                    )
                    .order_by(CatalogEntityRevision.revision_number.desc())
                )
                model_update = bool(
                    latest is not None
                    and latest.content_sha256 is not None
                    and latest.content_sha256 != row.model_version_sha256
                )
        if row.recipe_revision_sha256 is not None:
            latest_recipe = self._latest_recipe_digest(
                session, row.recipe_revision_sha256
            )
            recipe_update = latest_recipe is not None and latest_recipe != row.recipe_revision_sha256
        return model_update, recipe_update

    @staticmethod
    def _latest_recipe_digest(session: Session, digest: str) -> str | None:
        local = session.scalar(
            select(LocalRecipeRevision).where(
                LocalRecipeRevision.content_sha256 == digest,
                LocalRecipeRevision.lifecycle == "resolved",
            )
        )
        if local is not None:
            latest = session.scalar(
                select(LocalRecipeRevision)
                .where(
                    LocalRecipeRevision.recipe_id == local.recipe_id,
                    LocalRecipeRevision.lifecycle == "resolved",
                )
                .order_by(LocalRecipeRevision.revision_number.desc())
            )
            return None if latest is None else latest.content_sha256
        greenfield = session.scalar(
            select(RecipeRevision).where(RecipeRevision.content_digest == digest)
        )
        if greenfield is None:
            return None
        latest_greenfield = session.scalar(
            select(RecipeRevision)
            .where(RecipeRevision.recipe_id == greenfield.recipe_id)
            .order_by(RecipeRevision.revision_number.desc())
        )
        return None if latest_greenfield is None else latest_greenfield.content_digest

    def discover_updates(
        self,
        *,
        artifact_set_sha256: str | None = None,
        limit: int = 100,
        boundary: tuple[str, str] | None = None,
    ) -> dict[str, object]:
        """Return a bounded, deterministic update page.

        Update discovery is metadata only: it never changes the immutable
        model pin or the active profile/run reference.  The optional exact
        set filter is used by the CLI and keeps a large NAS inventory from
        becoming an unbounded response.
        """
        if not 1 <= limit <= 100:
            raise ValueError("cache update limit is invalid")
        requested_set = _optional_digest(artifact_set_sha256)
        self.reconcile_storage()
        with self._session() as session:
            query = select(ModelCacheSet).order_by(
                ModelCacheSet.updated_at.desc(),
                ModelCacheSet.artifact_set_sha256.desc(),
            )
            if requested_set is not None:
                query = query.where(ModelCacheSet.artifact_set_sha256 == requested_set)
            rows = list(session.scalars(query))
            total = len(rows)
            start = 0
            if boundary is not None:
                boundary_time = _parse_iso(boundary[0])
                for index, row in enumerate(rows):
                    if (
                        _datetime(row.updated_at) == boundary_time
                        and row.artifact_set_sha256 == boundary[1]
                    ):
                        start = index + 1
                        break
                else:
                    raise ModelCacheConflict(
                        "model_cache.cursor_invalid",
                        "cache update cursor boundary is stale",
                    )
            page = rows[start : start + limit]
            result = []
            for row in page:
                manifest = ArtifactSetManifest.from_document(row.manifest)
                model_update, recipe_update = self._update_flags(session, row, manifest)
                latest_model = None
                latest_recipe = None
                if model_update and isinstance(manifest.model_version_ref, Mapping):
                    latest = session.scalar(
                        select(CatalogEntityRevision)
                        .join(CatalogEntity)
                        .where(
                            CatalogEntity.kind == CatalogKind.MODEL_VERSION.value,
                            CatalogEntity.publisher == manifest.model_version_ref.get("publisher"),
                            CatalogEntity.slug == manifest.model_version_ref.get("slug"),
                            CatalogEntityRevision.lifecycle == "resolved",
                        )
                        .order_by(CatalogEntityRevision.revision_number.desc())
                    )
                    latest_model = latest.content_sha256 if latest is not None else None
                if recipe_update and row.recipe_revision_sha256 is not None:
                    latest_recipe = self._latest_recipe_digest(
                        session, row.recipe_revision_sha256
                    )
                result.append(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "artifact_set_sha256": row.artifact_set_sha256,
                        "model_version_sha256": row.model_version_sha256,
                        "latest_model_version_sha256": latest_model,
                        "recipe_revision_sha256": row.recipe_revision_sha256,
                        "latest_recipe_revision_sha256": latest_recipe,
                        "model_update_available": model_update,
                        "recipe_update_available": recipe_update,
                        "updated_at": _iso(row.updated_at),
                    }
                )
            next_boundary = None
            if start + limit < total and page:
                last = page[-1]
                next_boundary = (_iso(last.updated_at) or "", last.artifact_set_sha256)
            return {
                "schema_version": SCHEMA_VERSION,
                "source_policy": SOURCE_POLICY,
                "updates": tuple(result),
                "total": total,
                "_next_boundary": next_boundary,
            }

    def storage_summary(self) -> StorageSummary:
        usage = shutil.disk_usage(self._root)
        object_bytes: dict[str, int] = {}
        objects = self._root / "objects"
        for path in objects.glob("*/*"):
            if (
                path.is_file()
                and not path.is_symlink()
                and len(path.name) == _DIGEST_LENGTH
                and path.name == path.name.lower()
                and _is_hex(path.name)
                and path.parent.name == path.name[:2]
            ):
                try:
                    object_bytes[path.name] = path.stat().st_size
                except OSError:
                    continue
        unique_used = sum(object_bytes.values())
        with self._session(write=True) as session:
            sets = list(session.scalars(select(ModelCacheSet)))
            memberships = list(session.scalars(select(ModelCacheSetArtifact)))
            operations = list(
                session.scalars(
                    select(ModelCacheOperation).where(
                        ModelCacheOperation.state.in_(["queued", "running", "partial"])
                    )
                )
            )
            for row in sets:
                self._refresh_protection(session, row)
            protected_sets = {row.artifact_set_sha256 for row in sets if row.protected}
            protected_artifacts = {
                item.artifact_sha256
                for item in memberships
                if item.artifact_set_sha256 in protected_sets
            }
            in_flight_artifacts: set[str] = set()
            for operation in operations:
                manifest = operation.payload.get("manifest") if isinstance(operation.payload, Mapping) else None
                if isinstance(manifest, Mapping):
                    try:
                        in_flight_artifacts.update(
                            item.sha256
                            for item in ArtifactSetManifest.from_document(manifest).artifacts
                        )
                    except ModelCacheError:
                        continue
            artifact_rows = {
                row.sha256: row for row in session.scalars(select(ModelCacheArtifact))
            }
            protected_bytes = sum(
                object_bytes.get(digest, 0)
                for digest in protected_artifacts
            )
            # Any on-disk object that is not protected can be reclaimed.  This
            # includes orphaned files left by an interrupted atomic publish;
            # reporting physical bytes keeps capacity decisions honest.
            reclaimable_bytes = sum(
                size
                for digest, size in object_bytes.items()
                if digest not in protected_artifacts
            )
            partial_bytes: dict[str, int] = {}
            partial_root = self._root / "partials"
            for partial in partial_root.glob("*/*.part"):
                if (
                    partial.is_file()
                    and not partial.is_symlink()
                    and len(partial.stem) == _DIGEST_LENGTH
                    and _is_hex(partial.stem)
                ):
                    try:
                        partial_bytes[partial.stem] = max(
                            partial_bytes.get(partial.stem, 0), partial.stat().st_size
                        )
                    except OSError:
                        continue
            in_flight_bytes = sum(
                max(
                    0,
                    artifact_rows[digest].expected_bytes
                    - max(
                        object_bytes.get(digest, 0),
                        partial_bytes.get(digest, 0),
                        int(artifact_rows[digest].actual_bytes),
                    ),
                )
                for digest in in_flight_artifacts
                if digest in artifact_rows
            )
        available = max(0, usage.free - self._reserve_bytes)
        return StorageSummary(
            total_bytes=usage.total,
            free_bytes=usage.free,
            reserve_bytes=self._reserve_bytes,
            available_bytes=available,
            unique_used_bytes=unique_used,
            in_flight_bytes=in_flight_bytes,
            protected_bytes=protected_bytes,
            reclaimable_bytes=reclaimable_bytes,
        )

    def eviction_preview(
        self,
        *,
        target_bytes: int,
    ) -> dict[str, object]:
        if not isinstance(target_bytes, int) or isinstance(target_bytes, bool) or target_bytes <= 0:
            raise ValueError("eviction target must be positive")
        self.reconcile_storage()
        before = self.storage_summary()
        with self._session(write=True) as session:
            rows = list(session.scalars(select(ModelCacheSet).order_by(ModelCacheSet.last_accessed_at)))
            memberships = list(session.scalars(select(ModelCacheSetArtifact)))
            by_set: dict[str, list[ModelCacheSetArtifact]] = {}
            for membership in memberships:
                by_set.setdefault(membership.artifact_set_sha256, []).append(membership)
            object_paths = {
                artifact.sha256: self._object_path(artifact.sha256)
                for artifact in session.scalars(select(ModelCacheArtifact))
            }
            selected: list[dict[str, object]] = []
            protected_entries: list[dict[str, object]] = []
            selected_sets: set[str] = set()
            for row in rows:
                self._refresh_protection(session, row)
                entry = {
                    "schema_version": SCHEMA_VERSION,
                    "artifact_set_sha256": row.artifact_set_sha256,
                    "reclaimable_bytes": 0,
                    "protected": bool(row.protected),
                    "protected_reasons": list(row.protected_reasons or ()),
                    "last_accessed_at": _iso(row.last_accessed_at) or "",
                }
                if row.protected:
                    protected_entries.append(entry)
                elif row.state != "cached":
                    continue
                else:
                    selected.append(entry)
            # A shared object is reclaimable only when every referencing set
            # is in the selected removal plan.  Build the plan incrementally
            # in LRU order and report actual object bytes, not declared sizes.
            candidates = [entry for entry in selected]
            chosen: list[dict[str, object]] = []
            chosen_objects: set[str] = set()
            previous_bytes = 0
            for entry in candidates:
                selected_sets.add(str(entry["artifact_set_sha256"]))
                chosen.append(entry)
                for membership in by_set.get(str(entry["artifact_set_sha256"]), ()):
                    digest = membership.artifact_sha256
                    references = {
                        item.artifact_set_sha256
                        for item in memberships
                        if item.artifact_sha256 == digest
                    }
                    if references <= selected_sets:
                        chosen_objects.add(digest)
                cumulative_bytes = sum(
                    object_paths[digest].stat().st_size
                    for digest in chosen_objects
                    if digest in object_paths and object_paths[digest].is_file()
                )
                entry["reclaimable_bytes"] = max(0, cumulative_bytes - previous_bytes)
                previous_bytes = cumulative_bytes
                if cumulative_bytes >= target_bytes:
                    break
            selected_bytes = sum(
                object_paths[digest].stat().st_size
                for digest in chosen_objects
                if digest in object_paths and object_paths[digest].is_file()
            )
            # Include protected rows in the review so the operator sees why
            # the target cannot be met; apply never permits deleting them.
            blockers: list[str] = []
            if selected_bytes < target_bytes:
                if protected_entries:
                    blockers.append("protected entries require separate reference removal")
                else:
                    blockers.append("target-exceeds-reclaimable-bytes")
            plan = {
                "schema_version": SCHEMA_VERSION,
                "kind": "evict",
                "target_bytes": target_bytes,
                "selected": [entry["artifact_set_sha256"] for entry in chosen],
                "selected_objects": sorted(chosen_objects),
            }
            plan_digest = _sha256_json(plan)
            after = StorageSummary(
                total_bytes=before.total_bytes,
                free_bytes=before.free_bytes + selected_bytes,
                reserve_bytes=before.reserve_bytes,
                available_bytes=before.available_bytes + selected_bytes,
                unique_used_bytes=max(0, before.unique_used_bytes - selected_bytes),
                in_flight_bytes=before.in_flight_bytes,
                protected_bytes=before.protected_bytes,
                reclaimable_bytes=max(0, before.reclaimable_bytes - selected_bytes),
            )
            return {
                "schema_version": SCHEMA_VERSION,
                "plan_digest": plan_digest,
                "target_bytes": target_bytes,
                "selected": chosen,
                "protected_entries": protected_entries,
                "reclaimable_bytes": before.reclaimable_bytes,
                "selected_bytes": selected_bytes,
                "storage_before": before.document(),
                "storage_after": after.document(),
                "blockers": blockers,
                "_selected_objects": sorted(chosen_objects),
            }

    def evict(
        self,
        *,
        actor: str,
        request_key: str,
        plan_digest: str,
        target_bytes: int,
    ) -> CacheOperationView:
        request_key = _request_key(request_key)
        requested_plan = _optional_digest(plan_digest)
        assert requested_plan is not None
        preview = self.eviction_preview(target_bytes=target_bytes)
        if preview["plan_digest"] != requested_plan:
            raise ModelCacheConflict("model_cache.stale_plan", "eviction preview is stale")
        if preview["blockers"]:
            raise ModelCacheConflict(
                "model_cache.eviction_blocked",
                "; ".join(str(item) for item in preview["blockers"]),
            )
        selected = [str(item["artifact_set_sha256"]) for item in preview["selected"]]
        if any(bool(item["protected"]) for item in preview["selected"]):
            raise ModelCacheConflict(
                "model_cache.protected_reference",
                "protected cache content must be unreferenced before removal",
            )
        payload = {
            "schema_version": SCHEMA_VERSION,
            "source_policy": SOURCE_POLICY,
            "target_bytes": target_bytes,
            "selected": selected,
            "selected_objects": list(
                preview.get("_selected_objects", ())
            ),
            "before_unique_used_bytes": self.storage_summary().unique_used_bytes,
        }
        with self._lock, self._session(write=True) as session:
            existing = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == request_key
                )
            )
            if existing is not None:
                if existing.kind != "evict" or existing.plan_digest != requested_plan:
                    raise ModelCacheConflict(
                        "model_cache.request_key_reused",
                        "request key was already used for another cache operation",
                    )
                operation_id = existing.id
            else:
                operation = ModelCacheOperation(
                    request_key=request_key,
                    schema_version=SCHEMA_VERSION,
                    kind="evict",
                    state="queued",
                    attempt=1,
                    artifact_set_sha256=None,
                    plan_digest=requested_plan,
                    payload=payload,
                    progress={
                        "schema_version": SCHEMA_VERSION,
                        "phase": "queued",
                        "completed_artifacts": 0,
                        "total_artifacts": len(selected),
                        "downloaded_bytes": 0,
                        "expected_bytes": int(preview["selected_bytes"]),
                        "current_artifact_key": None,
                    },
                    actor=actor,
                    created_at=self._clock(),
                    updated_at=self._clock(),
                )
                session.add(operation)
                session.flush()
                operation_id = operation.id
        return self.get_operation(operation_id)

    def _run_eviction(self, operation_id: str) -> None:
        with self._session() as session:
            operation = session.get(ModelCacheOperation, operation_id)
            if operation is None:
                raise ModelCacheNotFound("model_cache.operation_missing", "cache operation was not found")
            selected = tuple(str(item) for item in operation.payload.get("selected", ()))
            before_unique = int(operation.payload.get("before_unique_used_bytes", 0))
        self._set_operation_state(operation_id, "running")
        try:
            with self._session(write=True) as session:
                for index, digest in enumerate(selected, start=1):
                    row = session.get(ModelCacheSet, digest)
                    if row is None:
                        continue
                    self._refresh_protection(session, row)
                    if row.protected:
                        raise ModelCacheConflict(
                            "model_cache.protected_reference",
                            "protected cache content changed before eviction",
                        )
                    session.query(ModelCacheSetArtifact).filter(
                        ModelCacheSetArtifact.artifact_set_sha256 == digest
                    ).delete(synchronize_session=False)
                    session.delete(row)
                    operation = session.get(ModelCacheOperation, operation_id)
                    if operation is not None:
                        operation.progress = {
                            "schema_version": SCHEMA_VERSION,
                            "phase": "reclaiming",
                            "completed_artifacts": index,
                            "total_artifacts": len(selected),
                            "downloaded_bytes": 0,
                            "expected_bytes": int(operation.progress.get("expected_bytes", 0)),
                            "current_artifact_key": None,
                        }
                session.flush()
                referenced = {
                    item.artifact_sha256
                    for item in session.scalars(select(ModelCacheSetArtifact))
                }
                for artifact in list(session.scalars(select(ModelCacheArtifact))):
                    if artifact.sha256 in referenced:
                        continue
                    path = self._object_path(artifact.sha256)
                    if path.exists() or path.is_symlink():
                        path.unlink(missing_ok=True)
                    session.delete(artifact)
            self._set_operation_state(
                operation_id,
                "succeeded",
                result={
                    "schema_version": SCHEMA_VERSION,
                    "removed_entries": list(selected),
                    "reclaimed_bytes": max(
                        0,
                        before_unique - self.storage_summary().unique_used_bytes,
                    ),
                },
            )
        except (ModelCacheError, OSError, ValueError) as error:
            now = self._clock()
            with self._session(write=True) as session:
                operation = session.get(ModelCacheOperation, operation_id)
                if operation is not None:
                    operation.state = "failed"
                    operation.last_error = (
                        error.detail if isinstance(error, ModelCacheError) else str(error)
                    )[:512]
                    operation.updated_at = now
                    operation.completed_at = now

    def _refresh_entry_state(self, session: Session, set_digest: str) -> None:
        row = session.get(ModelCacheSet, set_digest)
        if row is None:
            return
        manifest = ArtifactSetManifest.from_document(row.manifest)
        row.verified_bytes = self._verified_bytes(session, set_digest)
        if row.state not in {"downloading", "verifying"}:
            row.state = "cached" if row.verified_bytes == manifest.expected_bytes else "needs-repair"

    def _object_key(self, digest: str) -> str:
        return f"objects/{digest[:2]}/{digest}"

    def _object_path(self, digest: str) -> Path:
        return self._root / "objects" / digest[:2] / digest

    def _partial_path(self, set_digest: str, digest: str) -> Path:
        return self._root / "partials" / set_digest / f"{digest}.part"

    def _quarantine_path(self, digest: str) -> Path:
        return self._root / "quarantine" / f"{digest}.{uuid.uuid4().hex}.quarantine"


def _request_key(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError) as error:
        raise ModelCacheConflict("model_cache.request_key_invalid", "request key is invalid") from error


def _is_private_host(value: str) -> bool:
    normalized = value.lower().rstrip(".")
    if normalized in {"localhost", "localhost.localdomain", "ip6-localhost"}:
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _valid_relative_path(value: str) -> bool:
    return bool(
        value
        and len(value) <= 512
        and not value.startswith("/")
        and "\\" not in value
        and "\x00" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _contains_digest(value: object, model_digest: str | None, recipe_digest: str | None) -> bool:
    if model_digest is None and recipe_digest is None:
        return False
    if isinstance(value, Mapping):
        return any(_contains_digest(child, model_digest, recipe_digest) for child in value.values()) or (
            (model_digest is not None and value.get("model_version_sha256") == model_digest)
            or (recipe_digest is not None and value.get("recipe_revision_sha256") == recipe_digest)
            or (model_digest is not None and value.get("model_version") == model_digest)
        )
    if isinstance(value, list):
        return any(_contains_digest(child, model_digest, recipe_digest) for child in value)
    return False


def _eviction_plan_objects(preview: Mapping[str, object], root: Path) -> tuple[str, ...]:
    # The preview exposes selected bytes and entries; the apply operation
    # revalidates membership and computes object reachability again.  This
    # helper intentionally returns no filesystem-derived authority.
    del root
    values = preview.get("selected_objects")
    if isinstance(values, list):
        return tuple(str(value) for value in values)
    return ()


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "SCHEMA_VERSION",
    "SOURCE_POLICY",
    "ArtifactSetManifest",
    "ArtifactSpec",
    "CacheOperationView",
    "ModelCacheConflict",
    "ModelCacheError",
    "ModelCacheNotFound",
    "ModelCacheResolutionError",
    "ModelCacheService",
    "ModelCacheStorageError",
    "StorageSummary",
]
