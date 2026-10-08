"""Artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from pydantic import ValidationError
from vonk_agent_protocol import ModelCacheCode, canonical_message
from vonk_forge_contracts.model import ModelReference

from ..bounded_json import require_integer, require_mapping
from ..model_cache_contract import (
    CacheIdentity,
    CacheIdentityArtifact,
    CacheManifest,
    CacheManifestArtifact,
    CacheManifestArtifactPart,
)
from ..strict_json import serialize_json_value
from .constants import (
    _DIGEST_LENGTH,
    _EMPTY_SHA256,
    _MAX_ARTIFACT_PARTS,
    _MAX_ARTIFACTS,
    _MAX_MANIFEST_BYTES,
    _WEIGHT_ROLES,
    SCHEMA_VERSION,
    SOURCE_POLICY,
)
from .errors import ModelCacheResolutionInvalid, ModelCacheResolutionRefused
from .source_helpers import _valid_relative_path


@dataclass(frozen=True, slots=True)
class ArtifactPart:
    """One source-published piece of a file the source hosts only split.

    Hugging Face caps a file at 50 GB, so a larger file exists there only as
    ``name.part00``, ``name.part01``, ...; the pieces are joined by byte
    concatenation, in order, into the one file the artifact names.
    """

    path: str
    source: str
    sha256: str
    expected_bytes: int


@dataclass(frozen=True, slots=True)
class ArtifactSpec:
    """One exact downloadable artifact in a resolved set.

    ``sha256`` and ``expected_bytes`` always describe the whole installed file.
    ``parts`` is set only when the source publishes the file split; the parts
    are a transport detail of one ingest, never cache objects of their own, so
    reuse (``cache_identity``) is by the whole file's bytes alone.
    """

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
    model_content_sha256: str | None = None
    parts: tuple[ArtifactPart, ...] | None = None
    # Set only on the transient per-part view of a split file (``part_spec``):
    # transfer progress of a part is accounted on the whole file's ledger entry,
    # offset by the bytes of the parts already appended.
    ledger_sha256: str | None = None
    ledger_base: int = 0

    def part_spec(self, index: int) -> ArtifactSpec:
        """The transient single-file view that downloads part ``index``."""

        assert self.parts is not None
        part = self.parts[index]
        return replace(
            self,
            path=part.path,
            source=part.source,
            sha256=part.sha256,
            expected_bytes=part.expected_bytes,
            parts=None,
            ledger_sha256=self.sha256,
            ledger_base=sum(item.expected_bytes for item in self.parts[:index]),
        )

    def contract(self) -> CacheManifestArtifact:
        return CacheManifestArtifact(
            key=self.key,
            id=self.artifact_id,
            path=self.path,
            kind=self.kind,
            repository=self.repository,
            source=self.source,
            revision=self.revision,
            sha256=self.sha256,
            download_bytes=self.expected_bytes,
            roles=list(self.roles),
            model_content_sha256=self.model_content_sha256,
            parts=None
            if self.parts is None
            else [
                CacheManifestArtifactPart(
                    path=part.path,
                    source=part.source,
                    sha256=part.sha256,
                    download_bytes=part.expected_bytes,
                )
                for part in self.parts
            ],
        )

    def cache_identity(self) -> CacheIdentityArtifact:
        """Return only immutable bytes/source identity for cache reuse.

        Model/recipe content digests, roles, and mount selectors are
        provenance or runtime execution facts.  They must remain visible in
        the manifest but cannot make the same selected file bytes download a
        second time.
        """
        return CacheIdentityArtifact(
            key=self.key,
            id=self.artifact_id,
            path=self.path,
            kind=self.kind,
            repository=self.repository,
            source=self.source,
            revision=self.revision,
            sha256=self.sha256,
            download_bytes=self.expected_bytes,
        )

    @classmethod
    def from_manifest(cls, value: object) -> ArtifactSpec:
        try:
            wire = CacheManifestArtifact.model_validate_json(canonical_message(value))
        except ValidationError as error:
            raise ModelCacheResolutionInvalid(
                ModelCacheCode.MANIFEST_INVALID, "cache manifest artifact is invalid"
            ) from error
        return cls._from_wire(wire)

    @classmethod
    def _from_wire(cls, wire: CacheManifestArtifact) -> ArtifactSpec:
        result = cls(
            key=wire.key,
            artifact_id=wire.id,
            path=wire.path,
            kind=wire.kind,
            repository=wire.repository,
            source=wire.source,
            revision=wire.revision,
            sha256=wire.sha256,
            expected_bytes=wire.download_bytes,
            roles=tuple(wire.roles),
            model_content_sha256=wire.model_content_sha256,
            parts=(
                None
                if wire.parts is None
                else tuple(
                    ArtifactPart(
                        path=part.path,
                        source=part.source,
                        sha256=part.sha256,
                        expected_bytes=part.download_bytes,
                    )
                    for part in wire.parts
                )
            ),
        )
        _validate_artifact(result)
        return result


@dataclass(frozen=True, slots=True)
class ArtifactSetManifest:
    model_content_sha256: str | None
    recipe_revision_sha256: str | None
    model_content_digests: tuple[str, ...]
    artifacts: tuple[ArtifactSpec, ...]
    model_definition_ref: ModelReference | None = None

    def contract(self) -> CacheManifest:
        return CacheManifest(
            schema_version=SCHEMA_VERSION,
            source_policy=SOURCE_POLICY,
            model_content_sha256=self.model_content_sha256,
            recipe_revision_sha256=self.recipe_revision_sha256,
            model_definition_ref=self.model_definition_ref,
            model_content_digests=list(self.model_content_digests),
            artifacts=[item.contract() for item in self.artifacts],
        )

    def document(self) -> dict[str, object]:
        """The manifest as the JSON document stored and hashed."""

        return serialize_json_value(self.contract())

    @property
    def digest(self) -> str:
        return _sha256_json(serialize_json_value(self.identity()))

    def identity(self) -> CacheIdentity:
        """Return the reusable identity, separate from requested provenance."""
        return CacheIdentity(
            schema_version=SCHEMA_VERSION,
            source_policy=SOURCE_POLICY,
            artifacts=[item.cache_identity() for item in self.artifacts],
        )

    @property
    def expected_bytes(self) -> int:
        return sum(
            value.expected_bytes
            for _digest, value in _unique_artifacts(self.artifacts).items()
        )

    @classmethod
    def from_document(cls, value: object) -> ArtifactSetManifest:
        failure: Exception | None = None
        wire: CacheManifest | None = None
        try:
            document = require_mapping(value, "cache manifest must be a JSON object")
            wire = CacheManifest.model_validate_json(canonical_message(document))
        except (TypeError, ValueError, ValidationError) as error:
            failure = error
        if wire is None or _has_python_tuple(value):
            code = (
                ModelCacheCode.SCHEMA_UNSUPPORTED
                if isinstance(failure, ValidationError)
                and any(
                    issue.get("loc") == ("schema_version",)
                    for issue in failure.errors()
                )
                else ModelCacheCode.MANIFEST_INVALID
            )
            detail = (
                "cache manifest schema is unsupported"
                if code == ModelCacheCode.SCHEMA_UNSUPPORTED
                else "cache manifest shape is invalid"
            )
            raise ModelCacheResolutionInvalid(code, detail) from failure
        return cls.from_contract(wire)

    @classmethod
    def from_contract(cls, wire: CacheManifest) -> ArtifactSetManifest:
        """Build the validated storage identity from the canonical manifest."""

        result = cls(
            model_content_sha256=_optional_digest(wire.model_content_sha256),
            recipe_revision_sha256=_optional_digest(wire.recipe_revision_sha256),
            model_content_digests=tuple(wire.model_content_digests),
            artifacts=tuple(
                sorted(
                    (ArtifactSpec._from_wire(item) for item in wire.artifacts),
                    key=lambda item: item.key,
                )
            ),
            model_definition_ref=wire.model_definition_ref,
        )
        _validate_manifest(result)
        return result


def _has_python_tuple(value: object) -> bool:
    """Whether a manifest document holds a Python-only tuple, which would be
    normalized into a JSON array and so must be refused."""

    if isinstance(value, tuple):
        return True
    if isinstance(value, Mapping):
        return any(_has_python_tuple(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_python_tuple(item) for item in value)
    return False


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_digest(value: object) -> bool:
    """Whether ``value`` is a lowercase hex digest (no raise: a damaged one is skipped)."""

    return (
        isinstance(value, str)
        and len(value) == _DIGEST_LENGTH
        and value == value.lower()
        and _is_hex(value)
    )


def _optional_digest(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) != _DIGEST_LENGTH:
        raise ModelCacheResolutionRefused(
            ModelCacheCode.DIGEST_INVALID, "cache identity digest is invalid"
        )
    try:
        int(value, 16)
    except ValueError as error:
        raise ModelCacheResolutionRefused(
            ModelCacheCode.DIGEST_INVALID, "cache identity digest is invalid"
        ) from error
    if value != value.lower():
        raise ModelCacheResolutionRefused(
            ModelCacheCode.DIGEST_INVALID, "cache identity digest is invalid"
        )
    return value


def _validate_artifact(value: ArtifactSpec) -> None:
    from .catalog_helpers import _github_release_asset_binding, _validate_source

    if (
        not value.key
        or len(value.key) > 256
        or not value.key[0].isalpha()
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_.:-"
            for character in value.key
        )
        or not value.artifact_id
        or len(value.artifact_id) > 256
        or re.fullmatch(r"[a-z][a-z0-9_.:-]{0,255}", value.artifact_id) is None
        or not value.path
        or len(value.path) > 512
        or value.path.startswith("/")
        or "\\" in value.path
        or "\x00" in value.path
        or any(part in {"", ".", ".."} for part in value.path.split("/"))
        or value.kind
        not in {
            "huggingface.file",
            "github-release.asset",
            "http.file",
            "file",
        }
        or len(value.sha256) != _DIGEST_LENGTH
        or value.sha256 != value.sha256.lower()
        or not _is_hex(value.sha256)
        or not isinstance(value.expected_bytes, int)
        or isinstance(value.expected_bytes, bool)
        or value.expected_bytes < 0
        or (value.expected_bytes == 0 and value.sha256 != _EMPTY_SHA256)
        or not value.roles
        or len(value.roles) > 32
        or any(not isinstance(role, str) for role in value.roles)
        or len(set(value.roles)) != len(value.roles)
        or any(not role or len(role) > 64 for role in value.roles)
    ):
        raise ModelCacheResolutionRefused(
            ModelCacheCode.ARTIFACT_INVALID, "cache artifact identity is invalid"
        )
    if value.expected_bytes == 0 and any(
        role.lower() in _WEIGHT_ROLES for role in value.roles
    ):
        raise ModelCacheResolutionRefused(
            ModelCacheCode.ARTIFACT_INVALID,
            "only verified empty support artifacts may have zero bytes",
        )
    if value.kind != "file" and value.revision is None:
        raise ModelCacheResolutionRefused(
            ModelCacheCode.REVISION_MISSING,
            "remote cache artifacts require an immutable revision",
        )
    _validate_source(value.source)
    if value.kind == "github-release.asset":
        _github_release_asset_binding(value)
    if value.parts is not None:
        _validate_parts(value)


def _validate_parts(value: ArtifactSpec) -> None:
    from .catalog_helpers import _validate_source

    parts = value.parts
    assert parts is not None
    if (
        value.kind != "huggingface.file"
        or not 2 <= len(parts) <= _MAX_ARTIFACT_PARTS
        or len({part.path for part in parts}) != len(parts)
        or value.path in {part.path for part in parts}
        or sum(part.expected_bytes for part in parts) != value.expected_bytes
    ):
        raise ModelCacheResolutionRefused(
            ModelCacheCode.ARTIFACT_INVALID, "cache artifact parts are invalid"
        )
    for part in parts:
        if (
            not _valid_relative_path(part.path)
            or len(part.sha256) != _DIGEST_LENGTH
            or part.sha256 != part.sha256.lower()
            or not _is_hex(part.sha256)
            or not isinstance(part.expected_bytes, int)
            or isinstance(part.expected_bytes, bool)
            or part.expected_bytes < 1
        ):
            raise ModelCacheResolutionRefused(
                ModelCacheCode.ARTIFACT_INVALID, "cache artifact part is invalid"
            )
        _validate_source(part.source)


def _validate_manifest(value: ArtifactSetManifest) -> None:
    if len(value.artifacts) < 1 or len(value.artifacts) > _MAX_ARTIFACTS:
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.ARTIFACT_COUNT, "cache artifact set count is invalid"
        )
    keys = [item.key for item in value.artifacts]
    if len(keys) != len(set(keys)):
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.ARTIFACT_DUPLICATE, "cache artifact keys must be unique"
        )
    _unique_artifacts(value.artifacts)
    if any(
        not isinstance(item, str) or not _is_hex(item)
        for item in value.model_content_digests
    ):
        raise ModelCacheResolutionRefused(
            ModelCacheCode.MODEL_CONTENT_DIGESTS_INVALID,
            "cache model dependency pins are invalid",
        )
    encoded = json.dumps(
        value.document(), sort_keys=True, separators=(",", ":")
    ).encode()
    if len(encoded) > _MAX_MANIFEST_BYTES:
        raise ModelCacheResolutionInvalid(
            ModelCacheCode.MANIFEST_TOO_LARGE, "cache manifest exceeds the size limit"
        )


def _part_from_input(raw: object) -> ArtifactPart:
    value = require_mapping(raw, "artifact part")
    return ArtifactPart(
        path=str(value["path"]),
        source=str(value["source"]),
        sha256=str(value["sha256"]),
        expected_bytes=require_integer(value["download_bytes"], "part download bytes"),
    )


def _split_transient_bytes(
    manifest: ArtifactSetManifest, cached: frozenset[str] | None
) -> int:
    """Extra disk a split file needs beyond its own bytes while it is assembled."""

    return max(
        (
            max(part.expected_bytes for part in spec.parts)
            for digest, spec in _unique_artifacts(manifest.artifacts).items()
            if spec.parts is not None and (cached is None or digest not in cached)
        ),
        default=0,
    )


def _unique_artifacts(values: Sequence[ArtifactSpec]) -> dict[str, ArtifactSpec]:
    result: dict[str, ArtifactSpec] = {}
    for item in values:
        existing = result.get(item.sha256)
        if existing is not None and existing.expected_bytes != item.expected_bytes:
            raise ModelCacheResolutionRefused(
                ModelCacheCode.DIGEST_SIZE_CONFLICT,
                "one artifact digest has conflicting sizes",
            )
        result.setdefault(item.sha256, item)
    return result


def _composed_artifacts(values: Sequence[ArtifactSpec]) -> tuple[ArtifactSpec, ...]:
    """Name every catalog selection while retaining shared physical objects.

    Ordinary selections keep their existing keys and cache identities. Two
    models can select the same file ID and bytes, so a colliding key names the
    immutable source identity instead. Repeated identical sources receive
    distinct occurrence keys: all model/file provenance stays in the manifest,
    while its reusable digest remains independent of that provenance. Transfers
    and storage continue to own one object per SHA, not one per selection.
    """
    by_key: dict[str, list[ArtifactSpec]] = {}
    for spec in values:
        by_key.setdefault(spec.key, []).append(spec)
    result: list[ArtifactSpec] = []
    for group in by_key.values():
        if len(group) == 1:
            result.extend(group)
            continue
        by_identity: dict[str, list[ArtifactSpec]] = {}
        for spec in group:
            identity = _sha256_json(serialize_json_value(spec.cache_identity()))
            by_identity.setdefault(identity, []).append(spec)
        for identity, selections in by_identity.items():
            for index, spec in enumerate(selections):
                key = f"artifact-{identity}"
                if len(selections) > 1:
                    key += f":{index}"
                result.append(replace(spec, key=key))
    return tuple(result)


def _is_hex(value: str) -> bool:
    if not value:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True
