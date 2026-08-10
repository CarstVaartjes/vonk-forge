"""Canonical identity documents for mutable development Compose cohorts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

DEVELOPMENT_IMAGE_IDENTITY_PATH = Path(
    "/usr/local/share/vonk-forge/development-image-identity.json"
)
DEVELOPMENT_SOURCE_REPOSITORY = "https://github.com/CarstVaartjes/vonk-forge"
DEVELOPMENT_CHANNEL = "development"
DEVELOPMENT_PLATFORM_VERSION = "0.1.0"
DEVELOPMENT_DATABASE_REVISION = "0020_recipe_catalog_bridge"
DEVELOPMENT_PROTOCOL_MINIMUM = 1
DEVELOPMENT_PROTOCOL_MAXIMUM = 2
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
_GENERATION_ID = re.compile(r"dev-[0-9a-f]{24}\Z")
_NONCE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_JSON_BYTES = 16 * 1024


class DevelopmentCohortError(ValueError):
    """A development image or cohort identity cannot be trusted."""


def canonical_json(value: object) -> bytes:
    """Return the canonical ASCII JSON encoding used for public identities."""
    try:
        return (
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            + "\n"
        ).encode("ascii")
    except (TypeError, UnicodeEncodeError) as error:
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
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
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


def _read_stable_regular_file(path: Path, *, label: str) -> bytes:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as error:
        raise DevelopmentCohortError(f"development {label} identity source is unsafe") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 0 < before.st_size <= _MAX_JSON_BYTES
        ):
            raise DevelopmentCohortError(
                f"development {label} identity source is unsafe"
            )
        content = bytearray()
        while len(content) <= _MAX_JSON_BYTES:
            chunk = os.read(
                descriptor,
                min(4096, _MAX_JSON_BYTES + 1 - len(content)),
            )
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        if len(content) != before.st_size or (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_uid,
            before.st_gid,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_gid,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
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
        generation_hash = _generation_hash(
            common, self.api_identity_digest, self.worker_identity_digest
        )
        if (
            _DIGEST.fullmatch(self.release_digest) is None
            or _DIGEST.fullmatch(self.api_identity_digest) is None
            or _DIGEST.fullmatch(self.worker_identity_digest) is None
            or self.release_digest != _release_digest(common)
            or self.api_image
            != f"development.invalid/vonk-forge-api@{self.api_identity_digest}"
            or self.worker_image
            != f"development.invalid/vonk-forge-worker@{self.worker_identity_digest}"
            or _IMAGE.fullmatch(self.api_image) is None
            or _IMAGE.fullmatch(self.worker_image) is None
            or self.generation_id != "dev-" + generation_hash[:24]
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
        release_digest=_release_digest(common),
        api_identity_digest=api_digest,
        worker_identity_digest=worker_digest,
        api_image=f"development.invalid/vonk-forge-api@{api_digest}",
        worker_image=f"development.invalid/vonk-forge-worker@{worker_digest}",
        generation_id="dev-" + generation_hash[:24],
        start_nonce=_start_nonce(generation_hash),
    )
