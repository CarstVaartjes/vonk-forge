"""Canonical identity documents for mutable development Compose cohorts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

DEVELOPMENT_IMAGE_IDENTITY_PATH = Path(
    "/usr/local/share/vonk-forge/development-image-identity.json"
)
DEVELOPMENT_SOURCE_REPOSITORY = "https://github.com/CarstVaartjes/vonk-forge"
DEVELOPMENT_CHANNEL = "development"
DEVELOPMENT_PLATFORM_VERSION = "0.1.0"
DEVELOPMENT_DATABASE_REVISION = "0021_browser_authentication"
DEVELOPMENT_PROTOCOL_MINIMUM = 1
DEVELOPMENT_PROTOCOL_MAXIMUM = 3
DEVELOPMENT_SCHEMA_VERSION = 1

_IDENTITY_FIELDS = frozenset(
    {
        "schema_version",
        "source_repository",
        "source_commit",
        "channel",
        "platform_version",
        "build_digest",
        "database_revision",
        "protocol_minimum",
        "protocol_maximum",
        "image_role",
    }
)
_SELECTED_FIELDS = frozenset(
    {
        "schema_version",
        "source_repository",
        "source_commit",
        "channel",
        "platform_version",
        "build_digest",
        "database_revision",
        "protocol_minimum",
        "protocol_maximum",
        "release_digest",
        "api_identity_digest",
        "worker_identity_digest",
        "api_image",
        "worker_image",
        "generation_id",
        "start_nonce",
    }
)
_COHORT_DIGEST_FIELDS = (
    "schema_version",
    "source_repository",
    "source_commit",
    "channel",
    "platform_version",
    "database_revision",
    "protocol_minimum",
    "protocol_maximum",
)
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IMAGE = re.compile(r"development\.invalid/vonk-forge-(api|worker)@sha256:[0-9a-f]{64}\Z")
_SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_REVISION = re.compile(r"[0-9]{4}_[a-z0-9_]+\Z")
_GENERATION_ID = re.compile(r"gen-[0-9a-f]{24}\Z")
_NONCE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_JSON_BYTES = 16 * 1024
_MAX_COHORT_FILE_BYTES = 64 * 1024
_COHORT_ROLES = frozenset({"api", "worker"})
_COHORT_LIFECYCLE_FILES = frozenset({"api.json", "worker.json", "selected.json"})
_COHORT_TEMPORARY_FILE = re.compile(
    r"\.(api|worker|selected)\.json\.[0-9a-f]{32}\.tmp\Z"
)
_COHORT_UID = 10001
_COHORT_GID = 10001
_DIRECTORY_MODE = 0o700
_PUBLISHED_MODE = 0o444


class DevelopmentCohortError(ValueError):
    """A development image or cohort identity cannot be trusted."""


def canonical_json(value: object) -> bytes:
    """Return the canonical ASCII JSON encoding used for public identities."""
    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("ascii")
    except (TypeError, UnicodeEncodeError, ValueError) as error:
        raise DevelopmentCohortError("development identity is not canonical JSON") from error


def _load_canonical_object(raw: bytes, *, label: str) -> dict[str, object]:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= _MAX_JSON_BYTES:
        raise DevelopmentCohortError(f"development {label} identity size is invalid")
    if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        raise DevelopmentCohortError(f"development {label} identity is not canonical")

    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        seen: set[str] = set()
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in seen:
                raise DevelopmentCohortError(
                    f"development {label} identity contains duplicate field"
                )
            seen.add(key)
            result[key] = value
        return result

    try:
        document = json.loads(raw, object_pairs_hook=no_duplicates)
    except DevelopmentCohortError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
        raise DevelopmentCohortError(
            f"development {label} identity is malformed"
        ) from error
    if not isinstance(document, dict):
        raise DevelopmentCohortError(f"development {label} identity must be an object")
    if canonical_json(document) != raw:
        raise DevelopmentCohortError(f"development {label} identity is not canonical")
    return document


def _require_exact_fields(
    document: dict[str, object], fields: frozenset[str], *, label: str
) -> None:
    present = set(document)
    missing = fields - present
    unknown = present - fields
    if missing:
        raise DevelopmentCohortError(
            f"development {label} identity is missing required fields"
        )
    if unknown:
        raise DevelopmentCohortError(
            f"development {label} identity contains unknown fields"
        )


def _as_str(document: dict[str, object], key: str, *, label: str) -> str:
    value = document[key]
    if not isinstance(value, str):
        raise DevelopmentCohortError(f"development {label} identity field is invalid")
    return value


def _as_int(document: dict[str, object], key: str, *, label: str) -> int:
    value = document[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise DevelopmentCohortError(f"development {label} identity field is invalid")
    return value


def _validate_identity_values(
    *,
    schema_version: int,
    source_repository: str,
    source_commit: str,
    channel: str,
    platform_version: str,
    build_digest: str,
    database_revision: str,
    protocol_minimum: int,
    protocol_maximum: int,
    image_role: str,
) -> None:
    if (
        schema_version != DEVELOPMENT_SCHEMA_VERSION
        or source_repository != DEVELOPMENT_SOURCE_REPOSITORY
        or _COMMIT.fullmatch(source_commit) is None
        or channel != DEVELOPMENT_CHANNEL
        or platform_version != DEVELOPMENT_PLATFORM_VERSION
        or _SEMVER.fullmatch(platform_version) is None
        or _DIGEST.fullmatch(build_digest) is None
        or database_revision != DEVELOPMENT_DATABASE_REVISION
        or _REVISION.fullmatch(database_revision) is None
        or protocol_minimum != DEVELOPMENT_PROTOCOL_MINIMUM
        or protocol_maximum != DEVELOPMENT_PROTOCOL_MAXIMUM
        or image_role not in {"api", "worker"}
    ):
        raise DevelopmentCohortError("development image identity is invalid")


@dataclass(frozen=True)
class DevelopmentImageIdentity:
    schema_version: int
    source_repository: str
    source_commit: str
    channel: str
    platform_version: str
    build_digest: str
    database_revision: str
    protocol_minimum: int
    protocol_maximum: int
    image_role: str

    def __post_init__(self) -> None:
        _validate_identity_values(
            schema_version=self.schema_version,
            source_repository=self.source_repository,
            source_commit=self.source_commit,
            channel=self.channel,
            platform_version=self.platform_version,
            build_digest=self.build_digest,
            database_revision=self.database_revision,
            protocol_minimum=self.protocol_minimum,
            protocol_maximum=self.protocol_maximum,
            image_role=self.image_role,
        )
        if self.build_digest != _cohort_build_digest(_common_identity_document(self)):
            raise DevelopmentCohortError(
                "development image identity build digest is invalid"
            )

    @classmethod
    def from_bytes(
        cls, raw: bytes, *, expected_role: str
    ) -> DevelopmentImageIdentity:
        document = _load_canonical_object(raw, label="image")
        _require_exact_fields(document, _IDENTITY_FIELDS, label="image")
        identity = cls(
            schema_version=_as_int(document, "schema_version", label="image"),
            source_repository=_as_str(document, "source_repository", label="image"),
            source_commit=_as_str(document, "source_commit", label="image"),
            channel=_as_str(document, "channel", label="image"),
            platform_version=_as_str(document, "platform_version", label="image"),
            build_digest=_as_str(document, "build_digest", label="image"),
            database_revision=_as_str(document, "database_revision", label="image"),
            protocol_minimum=_as_int(document, "protocol_minimum", label="image"),
            protocol_maximum=_as_int(document, "protocol_maximum", label="image"),
            image_role=_as_str(document, "image_role", label="image"),
        )
        if identity.image_role != expected_role:
            raise DevelopmentCohortError("development image identity role mismatch")
        return identity

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_repository": self.source_repository,
            "source_commit": self.source_commit,
            "channel": self.channel,
            "platform_version": self.platform_version,
            "build_digest": self.build_digest,
            "database_revision": self.database_revision,
            "protocol_minimum": self.protocol_minimum,
            "protocol_maximum": self.protocol_maximum,
            "image_role": self.image_role,
        }

    def to_bytes(self) -> bytes:
        return canonical_json(self.to_document())

    @property
    def identity_digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.to_bytes()).hexdigest()


def _cohort_build_digest(document: dict[str, object]) -> str:
    role_independent = {field: document[field] for field in _COHORT_DIGEST_FIELDS}
    return "sha256:" + hashlib.sha256(canonical_json(role_independent)).hexdigest()


def _common_identity_document(identity: DevelopmentImageIdentity) -> dict[str, object]:
    return {
        "schema_version": identity.schema_version,
        "source_repository": identity.source_repository,
        "source_commit": identity.source_commit,
        "channel": identity.channel,
        "platform_version": identity.platform_version,
        "database_revision": identity.database_revision,
        "protocol_minimum": identity.protocol_minimum,
        "protocol_maximum": identity.protocol_maximum,
        "build_digest": identity.build_digest,
    }


def _identity_from_common(
    common: dict[str, object], *, image_role: str
) -> DevelopmentImageIdentity:
    return DevelopmentImageIdentity(
        schema_version=_as_int(common, "schema_version", label="selected"),
        source_repository=_as_str(common, "source_repository", label="selected"),
        source_commit=_as_str(common, "source_commit", label="selected"),
        channel=_as_str(common, "channel", label="selected"),
        platform_version=_as_str(common, "platform_version", label="selected"),
        build_digest=_as_str(common, "build_digest", label="selected"),
        database_revision=_as_str(common, "database_revision", label="selected"),
        protocol_minimum=_as_int(common, "protocol_minimum", label="selected"),
        protocol_maximum=_as_int(common, "protocol_maximum", label="selected"),
        image_role=image_role,
    )


def _validate_common_document(document: dict[str, object], *, label: str) -> None:
    _validate_identity_values(
        schema_version=_as_int(document, "schema_version", label=label),
        source_repository=_as_str(document, "source_repository", label=label),
        source_commit=_as_str(document, "source_commit", label=label),
        channel=_as_str(document, "channel", label=label),
        platform_version=_as_str(document, "platform_version", label=label),
        build_digest=_as_str(document, "build_digest", label=label),
        database_revision=_as_str(document, "database_revision", label=label),
        protocol_minimum=_as_int(document, "protocol_minimum", label=label),
        protocol_maximum=_as_int(document, "protocol_maximum", label=label),
        image_role="api",
    )
    if _cohort_build_digest(document) != document["build_digest"]:
        raise DevelopmentCohortError(f"development {label} build digest is invalid")


def _release_digest(common: dict[str, object]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(common)).hexdigest()


def _generation_id(release_digest: str) -> str:
    return "gen-" + release_digest.removeprefix("sha256:")[:24]


def _generation_hash(
    common: dict[str, object], api_identity_digest: str, worker_identity_digest: str
) -> str:
    seed = {
        "api_identity_digest": api_identity_digest,
        "common": common,
        "worker_identity_digest": worker_identity_digest,
    }
    return hashlib.sha256(canonical_json(seed)).hexdigest()


def _start_nonce(generation_hash: str) -> str:
    return hashlib.sha256(
        f"vonk-forge:development-start:{generation_hash}".encode("ascii")
    ).hexdigest()


def build_identity(*, role: str, source_commit: str) -> DevelopmentImageIdentity:
    document: dict[str, object] = {
        "schema_version": DEVELOPMENT_SCHEMA_VERSION,
        "source_repository": DEVELOPMENT_SOURCE_REPOSITORY,
        "source_commit": source_commit,
        "channel": DEVELOPMENT_CHANNEL,
        "platform_version": DEVELOPMENT_PLATFORM_VERSION,
        "database_revision": DEVELOPMENT_DATABASE_REVISION,
        "protocol_minimum": DEVELOPMENT_PROTOCOL_MINIMUM,
        "protocol_maximum": DEVELOPMENT_PROTOCOL_MAXIMUM,
    }
    document["build_digest"] = _cohort_build_digest(document)
    document["image_role"] = role
    return DevelopmentImageIdentity(
        schema_version=_as_int(document, "schema_version", label="image"),
        source_repository=_as_str(document, "source_repository", label="image"),
        source_commit=_as_str(document, "source_commit", label="image"),
        channel=_as_str(document, "channel", label="image"),
        platform_version=_as_str(document, "platform_version", label="image"),
        build_digest=_as_str(document, "build_digest", label="image"),
        database_revision=_as_str(document, "database_revision", label="image"),
        protocol_minimum=_as_int(document, "protocol_minimum", label="image"),
        protocol_maximum=_as_int(document, "protocol_maximum", label="image"),
        image_role=_as_str(document, "image_role", label="image"),
    )


def _normalized_absolute_path(path: Path, *, label: str) -> str:
    value = os.fspath(path)
    if not isinstance(value, str) or not os.path.isabs(value):
        raise DevelopmentCohortError(f"development {label} path must be absolute")
    if os.path.normpath(value) != value:
        raise DevelopmentCohortError(f"development {label} path must be normalized")
    return value


def _open_absolute_directory(path: Path, *, label: str) -> int:
    value = _normalized_absolute_path(path, label=label)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = -1
    try:
        descriptor = os.open("/", flags)
        for component in Path(value).parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise DevelopmentCohortError(
            f"development {label} directory is unsafe"
        ) from error


def _open_parent(path: Path, *, label: str) -> tuple[int, str]:
    value = _normalized_absolute_path(path, label=label)
    parent, name = os.path.split(value)
    if not name or name in {".", ".."}:
        raise DevelopmentCohortError(f"development {label} path is unsafe")
    return _open_absolute_directory(Path(parent), label=label), name


def _stable_file_fingerprint(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_stable_regular_file_at(
    directory_descriptor: int, name: str, *, label: str
) -> bytes:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory_descriptor,
        )
    except OSError as error:
        raise DevelopmentCohortError(f"development {label} identity source is unsafe") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 0 < before.st_size <= _MAX_COHORT_FILE_BYTES
        ):
            raise DevelopmentCohortError(
                f"development {label} identity source is unsafe"
            )
        content = bytearray()
        while len(content) <= _MAX_COHORT_FILE_BYTES:
            chunk = os.read(
                descriptor,
                min(4096, _MAX_COHORT_FILE_BYTES + 1 - len(content)),
            )
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        if len(content) != before.st_size or _stable_file_fingerprint(
            before
        ) != _stable_file_fingerprint(after):
            raise DevelopmentCohortError(
                f"development {label} identity changed while read"
            )
        return bytes(content)
    except OSError as error:
        raise DevelopmentCohortError(
            f"development {label} identity cannot be read"
        ) from error
    finally:
        os.close(descriptor)


def _read_stable_regular_file(path: Path, *, label: str) -> bytes:
    directory_descriptor, name = _open_parent(path, label=label)
    try:
        return _read_stable_regular_file_at(
            directory_descriptor,
            name,
            label=label,
        )
    finally:
        os.close(directory_descriptor)


def read_identity(path: Path, *, expected_role: str) -> DevelopmentImageIdentity:
    return DevelopmentImageIdentity.from_bytes(
        _read_stable_regular_file(Path(path), label="image"),
        expected_role=expected_role,
    )


@dataclass(frozen=True)
class DevelopmentCohort:
    api: DevelopmentImageIdentity
    worker: DevelopmentImageIdentity

    def __post_init__(self) -> None:
        if self.api.image_role != "api" or self.worker.image_role != "worker":
            raise DevelopmentCohortError("development cohort roles are invalid")
        common = _common_identity_document(self.api)
        if _common_identity_document(self.worker) != common:
            raise DevelopmentCohortError("development cohort metadata mismatch")
        try:
            _validate_common_document(common, label="cohort")
            _validate_identity_values(**self.api.to_document())
            _validate_identity_values(**self.worker.to_document())
        except DevelopmentCohortError as error:
            raise DevelopmentCohortError(
                "development cohort identity is invalid"
            ) from error

    def common_document(self) -> dict[str, object]:
        return _common_identity_document(self.api)


@dataclass(frozen=True)
class SelectedDevelopmentCohort:
    schema_version: int
    source_repository: str
    source_commit: str
    channel: str
    platform_version: str
    database_revision: str
    protocol_minimum: int
    protocol_maximum: int
    build_digest: str
    release_digest: str
    api_identity_digest: str
    worker_identity_digest: str
    api_image: str
    worker_image: str
    generation_id: str
    start_nonce: str

    def __post_init__(self) -> None:
        common = self.common_document()
        try:
            _validate_common_document(common, label="selected")
        except DevelopmentCohortError as error:
            raise DevelopmentCohortError(
                "development selected cohort metadata is invalid"
            ) from error
        expected_api_digest = _identity_from_common(
            common, image_role="api"
        ).identity_digest
        expected_worker_digest = _identity_from_common(
            common, image_role="worker"
        ).identity_digest
        if (
            self.api_identity_digest != expected_api_digest
            or self.worker_identity_digest != expected_worker_digest
        ):
            raise DevelopmentCohortError(
                "development selected cohort identity digest is invalid"
            )
        generation_hash = _generation_hash(
            common, expected_api_digest, expected_worker_digest
        )
        expected_release_digest = _release_digest(common)
        if (
            _DIGEST.fullmatch(self.release_digest) is None
            or _DIGEST.fullmatch(self.api_identity_digest) is None
            or _DIGEST.fullmatch(self.worker_identity_digest) is None
            or self.release_digest != expected_release_digest
            or self.api_image
            != f"development.invalid/vonk-forge-api@{self.api_identity_digest}"
            or self.worker_image
            != f"development.invalid/vonk-forge-worker@{self.worker_identity_digest}"
            or _IMAGE.fullmatch(self.api_image) is None
            or _IMAGE.fullmatch(self.worker_image) is None
            or self.generation_id != _generation_id(expected_release_digest)
            or _GENERATION_ID.fullmatch(self.generation_id) is None
            or self.start_nonce != _start_nonce(generation_hash)
            or _NONCE.fullmatch(self.start_nonce) is None
        ):
            raise DevelopmentCohortError("development selected cohort is invalid")

    @classmethod
    def from_bytes(cls, raw: bytes) -> SelectedDevelopmentCohort:
        document = _load_canonical_object(raw, label="selected")
        _require_exact_fields(document, _SELECTED_FIELDS, label="selected")
        return cls(
            schema_version=_as_int(document, "schema_version", label="selected"),
            source_repository=_as_str(document, "source_repository", label="selected"),
            source_commit=_as_str(document, "source_commit", label="selected"),
            channel=_as_str(document, "channel", label="selected"),
            platform_version=_as_str(document, "platform_version", label="selected"),
            database_revision=_as_str(document, "database_revision", label="selected"),
            protocol_minimum=_as_int(document, "protocol_minimum", label="selected"),
            protocol_maximum=_as_int(document, "protocol_maximum", label="selected"),
            build_digest=_as_str(document, "build_digest", label="selected"),
            release_digest=_as_str(document, "release_digest", label="selected"),
            api_identity_digest=_as_str(
                document, "api_identity_digest", label="selected"
            ),
            worker_identity_digest=_as_str(
                document, "worker_identity_digest", label="selected"
            ),
            api_image=_as_str(document, "api_image", label="selected"),
            worker_image=_as_str(document, "worker_image", label="selected"),
            generation_id=_as_str(document, "generation_id", label="selected"),
            start_nonce=_as_str(document, "start_nonce", label="selected"),
        )

    def common_document(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_repository": self.source_repository,
            "source_commit": self.source_commit,
            "channel": self.channel,
            "platform_version": self.platform_version,
            "database_revision": self.database_revision,
            "protocol_minimum": self.protocol_minimum,
            "protocol_maximum": self.protocol_maximum,
            "build_digest": self.build_digest,
        }

    def to_document(self) -> dict[str, object]:
        return {
            **self.common_document(),
            "release_digest": self.release_digest,
            "api_identity_digest": self.api_identity_digest,
            "worker_identity_digest": self.worker_identity_digest,
            "api_image": self.api_image,
            "worker_image": self.worker_image,
            "generation_id": self.generation_id,
            "start_nonce": self.start_nonce,
        }

    def to_bytes(self) -> bytes:
        return canonical_json(self.to_document())


def verify_cohort(
    identities: list[DevelopmentImageIdentity] | tuple[DevelopmentImageIdentity, ...],
) -> SelectedDevelopmentCohort:
    roles: dict[str, DevelopmentImageIdentity] = {}
    for identity in identities:
        if not isinstance(identity, DevelopmentImageIdentity):
            raise DevelopmentCohortError("development cohort roles are invalid")
        role = identity.image_role
        if role not in {"api", "worker"} or role in roles:
            raise DevelopmentCohortError("development cohort roles are invalid")
        roles[role] = identity
    if set(roles) != {"api", "worker"}:
        raise DevelopmentCohortError("development cohort roles are invalid")
    cohort = DevelopmentCohort(api=roles["api"], worker=roles["worker"])
    common = cohort.common_document()
    api_digest = cohort.api.identity_digest
    worker_digest = cohort.worker.identity_digest
    generation_hash = _generation_hash(common, api_digest, worker_digest)
    release_digest = _release_digest(common)
    return SelectedDevelopmentCohort(
        schema_version=cohort.api.schema_version,
        source_repository=cohort.api.source_repository,
        source_commit=cohort.api.source_commit,
        channel=cohort.api.channel,
        platform_version=cohort.api.platform_version,
        database_revision=cohort.api.database_revision,
        protocol_minimum=cohort.api.protocol_minimum,
        protocol_maximum=cohort.api.protocol_maximum,
        build_digest=cohort.api.build_digest,
        release_digest=release_digest,
        api_identity_digest=api_digest,
        worker_identity_digest=worker_digest,
        api_image=f"development.invalid/vonk-forge-api@{api_digest}",
        worker_image=f"development.invalid/vonk-forge-worker@{worker_digest}",
        generation_id=_generation_id(release_digest),
        start_nonce=_start_nonce(generation_hash),
    )


def _cohort_root_descriptor(root: Path) -> int:
    return _open_absolute_directory(Path(root), label="cohort")


def _validate_role(role: str) -> str:
    if role not in _COHORT_ROLES:
        raise DevelopmentCohortError("development cohort role is invalid")
    return role


def _entry_metadata(directory_descriptor: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise DevelopmentCohortError("development cohort entry is unsafe") from error


def reset_cohort_root(path: Path) -> None:
    """Clear a dedicated cohort volume and give it to the unprivileged jobs."""
    if os.geteuid() != 0:
        raise DevelopmentCohortError("development cohort reset requires root")
    root = Path(path)
    _normalized_absolute_path(root, label="cohort")
    descriptor = _cohort_root_descriptor(root)
    try:
        names = os.listdir(descriptor)
        metadata: dict[str, os.stat_result] = {}
        for name in names:
            if (
                name not in _COHORT_LIFECYCLE_FILES
                and _COHORT_TEMPORARY_FILE.fullmatch(name) is None
            ):
                raise DevelopmentCohortError(
                    "development cohort volume contains unsafe entries"
                )
            entry = _entry_metadata(descriptor, name)
            if entry is None or not stat.S_ISREG(entry.st_mode) or entry.st_nlink != 1:
                raise DevelopmentCohortError(
                    "development cohort volume contains unsafe entries"
                )
            metadata[name] = entry

        for name, expected in metadata.items():
            current = _entry_metadata(descriptor, name)
            if current is None or _stable_file_fingerprint(
                current
            ) != _stable_file_fingerprint(expected):
                raise DevelopmentCohortError(
                    "development cohort volume changed during reset"
                )
            os.unlink(name, dir_fd=descriptor)
        os.fchown(descriptor, _COHORT_UID, _COHORT_GID)
        os.fchmod(descriptor, _DIRECTORY_MODE)
        os.fsync(descriptor)
    except DevelopmentCohortError:
        raise
    except OSError as error:
        raise DevelopmentCohortError("development cohort reset failed") from error
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, content: bytes) -> None:
    written = 0
    while written < len(content):
        count = os.write(descriptor, content[written:])
        if count <= 0:
            raise OSError("short cohort file write")
        written += count


def _unlink_if_same(
    directory_descriptor: int,
    name: str,
    expected: os.stat_result,
) -> None:
    current = _entry_metadata(directory_descriptor, name)
    if current is not None and _stable_file_fingerprint(
        current
    ) == _stable_file_fingerprint(expected):
        os.unlink(name, dir_fd=directory_descriptor)


def _publish_new_file(
    directory_descriptor: int,
    name: str,
    content: bytes,
    *,
    label: str,
) -> None:
    if not 0 < len(content) <= _MAX_COHORT_FILE_BYTES:
        raise DevelopmentCohortError(f"development {label} size is invalid")
    temporary_name = f".{name}.{secrets.token_hex(16)}.tmp"
    reservation_descriptor = -1
    temporary_descriptor = -1
    reservation: os.stat_result | None = None
    try:
        reservation_descriptor = os.open(
            name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | os.O_CLOEXEC,
            0,
            dir_fd=directory_descriptor,
        )
        reservation = os.fstat(reservation_descriptor)
        os.close(reservation_descriptor)
        reservation_descriptor = -1

        temporary_descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | os.O_CLOEXEC,
            0o600,
            dir_fd=directory_descriptor,
        )
        _write_all(temporary_descriptor, content)
        os.fchmod(temporary_descriptor, _PUBLISHED_MODE)
        os.fsync(temporary_descriptor)
        temporary_metadata = os.fstat(temporary_descriptor)
        if (
            not stat.S_ISREG(temporary_metadata.st_mode)
            or temporary_metadata.st_nlink != 1
            or temporary_metadata.st_size != len(content)
            or stat.S_IMODE(temporary_metadata.st_mode) != _PUBLISHED_MODE
        ):
            raise DevelopmentCohortError(
                f"development {label} temporary file is unsafe"
            )
        os.close(temporary_descriptor)
        temporary_descriptor = -1

        current_reservation = _entry_metadata(directory_descriptor, name)
        if (
            reservation is None
            or current_reservation is None
            or _stable_file_fingerprint(current_reservation)
            != _stable_file_fingerprint(reservation)
        ):
            raise DevelopmentCohortError(
                f"development {label} destination changed"
            )
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        os.fsync(directory_descriptor)
        published = _entry_metadata(directory_descriptor, name)
        if (
            published is None
            or not stat.S_ISREG(published.st_mode)
            or published.st_nlink != 1
            or published.st_size != len(content)
            or stat.S_IMODE(published.st_mode) != _PUBLISHED_MODE
        ):
            raise DevelopmentCohortError(
                f"development {label} publication is unsafe"
            )
    except FileExistsError as error:
        raise DevelopmentCohortError(
            f"development {label} destination already exists"
        ) from error
    except DevelopmentCohortError:
        raise
    except OSError as error:
        raise DevelopmentCohortError(f"development {label} cannot be written") from error
    finally:
        if reservation_descriptor >= 0:
            os.close(reservation_descriptor)
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        temporary = _entry_metadata(directory_descriptor, temporary_name)
        if temporary is not None and stat.S_ISREG(temporary.st_mode):
            _unlink_if_same(directory_descriptor, temporary_name, temporary)
        if reservation is not None:
            _unlink_if_same(directory_descriptor, name, reservation)


def report_identity(root: Path, role: str) -> DevelopmentImageIdentity:
    """Publish this image's fixed embedded identity into the cohort volume."""
    expected_role = _validate_role(role)
    identity = read_identity(
        DEVELOPMENT_IMAGE_IDENTITY_PATH,
        expected_role=expected_role,
    )
    descriptor = _cohort_root_descriptor(Path(root))
    try:
        _publish_new_file(
            descriptor,
            f"{expected_role}.json",
            identity.to_bytes(),
            label=f"{expected_role} report",
        )
    finally:
        os.close(descriptor)
    return identity


def select_cohort(root: Path) -> SelectedDevelopmentCohort:
    """Verify the exact API/worker report set and publish its selection."""
    descriptor = _cohort_root_descriptor(Path(root))
    try:
        if set(os.listdir(descriptor)) != {"api.json", "worker.json"}:
            raise DevelopmentCohortError(
                "development cohort requires exactly api and worker reports"
            )
        identities = [
            DevelopmentImageIdentity.from_bytes(
                _read_stable_regular_file_at(
                    descriptor,
                    f"{role}.json",
                    label=f"{role} report",
                ),
                expected_role=role,
            )
            for role in ("api", "worker")
        ]
        selected = verify_cohort(identities)
        if set(os.listdir(descriptor)) != {"api.json", "worker.json"}:
            raise DevelopmentCohortError(
                "development cohort changed during verification"
            )
        _publish_new_file(
            descriptor,
            "selected.json",
            selected.to_bytes(),
            label="selected cohort",
        )
        return selected
    finally:
        os.close(descriptor)


def require_selected_cohort(
    path: Path, role: str
) -> SelectedDevelopmentCohort:
    """Require a selected cohort to contain this image's embedded identity."""
    expected_role = _validate_role(role)
    selected = SelectedDevelopmentCohort.from_bytes(
        _read_stable_regular_file(Path(path), label="selected cohort")
    )
    current = read_identity(
        DEVELOPMENT_IMAGE_IDENTITY_PATH,
        expected_role=expected_role,
    )
    expected = _identity_from_common(
        selected.common_document(),
        image_role=expected_role,
    )
    expected_digest = (
        selected.api_identity_digest
        if expected_role == "api"
        else selected.worker_identity_digest
    )
    if current != expected or current.identity_digest != expected_digest:
        raise DevelopmentCohortError(
            "development selected cohort does not match the current image"
        )
    return selected


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify mutable development image cohorts"
    )
    commands = parser.add_subparsers(dest="command_name", required=True)

    identity = commands.add_parser("build-identity")
    identity.add_argument("--role", choices=sorted(_COHORT_ROLES), required=True)
    identity.add_argument("--source-commit", required=True)

    reset = commands.add_parser("reset")
    reset.add_argument("root", nargs="?", type=Path, default=Path("/cohort"))

    report = commands.add_parser("report")
    report.add_argument("root", nargs="?", type=Path, default=Path("/cohort"))
    report.add_argument("--role", choices=sorted(_COHORT_ROLES), required=True)

    verify = commands.add_parser("verify")
    verify.add_argument("root", nargs="?", type=Path, default=Path("/cohort"))

    run = commands.add_parser("run-selected")
    run.add_argument("--role", choices=sorted(_COHORT_ROLES), required=True)
    run.add_argument("program", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command_name == "build-identity":
        sys.stdout.buffer.write(
            build_identity(
                role=arguments.role,
                source_commit=arguments.source_commit,
            ).to_bytes()
        )
        sys.stdout.buffer.flush()
        return 0
    if arguments.command_name == "reset":
        reset_cohort_root(arguments.root)
        return 0
    if arguments.command_name == "report":
        report_identity(arguments.root, arguments.role)
        return 0
    if arguments.command_name == "verify":
        select_cohort(arguments.root)
        return 0
    if arguments.command_name == "run-selected":
        program = list(arguments.program)
        if program[:1] == ["--"]:
            program = program[1:]
        if not program:
            raise DevelopmentCohortError(
                "development selected cohort command is required"
            )
        selected_path = os.environ.get("VONK_DEV_SELECTED_COHORT_FILE")
        if not selected_path:
            raise DevelopmentCohortError(
                "VONK_DEV_SELECTED_COHORT_FILE is required"
            )
        require_selected_cohort(Path(selected_path), arguments.role)
        os.execvp(program[0], program)
        raise AssertionError("os.execvp returned unexpectedly")
    raise AssertionError("unreachable development cohort command")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DevelopmentCohortError as error:
        raise SystemExit(str(error)) from error
