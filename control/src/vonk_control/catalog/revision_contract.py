"""Typed readers and writers for immutable catalog revision JSON columns."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING

from pydantic import ConfigDict, Field, ValidationError
from vonk_forge_contracts import (
    ModelDefinition,
    PrebuiltImage,
    RecipeApi,
    RecipeDefinition,
    document_sha256,
    read_model,
    read_recipe,
)
from vonk_forge_contracts.model import (
    ModelFormat,
    ModelIdentity,
)
from vonk_forge_contracts.qualification_status import (
    PublishedMeasuredMemory,
    PublishedQualificationStatusView,
    PublishedRoleMemory,
)
from vonk_forge_contracts.recipe import (
    ModelReference,
    RecipeIdentity,
    RecipeInterface,
    RecipeOption,
    RecipeProvenance,
    RecipeRelease,
    RecipeTopology,
    RecipeValidation,
)

from vonk_agent_protocol.build_import import RecipeBuildOptions
from vonk_agent_protocol.outcome import UnknownOutcomeError
from vonk_agent_protocol.wire_model import canonical_message
from vonk_control.bounded_json import require_mapping
from vonk_control.operator_receipt_contract import ReceiptOwner
from vonk_control.strict_json import (
    StrictJSONModel,
    serialize_json_value,
    stored_document_detail,
)

if TYPE_CHECKING:
    from vonk_control.models.catalog import CatalogDocumentRevision


# A published document as JSON, before any contract has read it: the exact bytes
# a digest names. Typed by what JSON can hold, never by ``object``.
type PublishedValue = (
    str | int | float | bool | list[PublishedValue] | dict[str, PublishedValue] | None
)
type PublishedDocument = dict[str, PublishedValue]


class CatalogRevisionContractError(UnknownOutcomeError, ValueError):
    """A persisted catalog document or projection is not current contract JSON."""


class RecipePackageHandleProjection(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    publication_commit: str
    source_commit: str
    package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    package_size: int = Field(gt=0)
    package_path: str
    recipe_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_path: str
    closure_path: str


class _CatalogRevisionProjection(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    failure_reason: str | None = None
    publication_commit: str | None = None
    source_path: str | None = None
    package_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    # Response only: the revision column owns the digest, so a stored projection
    # never holds it (the writer drops it) and the library view fills it in.
    source_bundle_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    package_handle: RecipePackageHandleProjection | None = None
    release_version: str | None = None
    release_released_at: str | None = None


class BuildResourcesProjection(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    cpu_cores: int = Field(ge=0)
    download_bytes: int = Field(ge=0)
    temporary_bytes: int = Field(ge=0)
    memory_bytes: int = Field(ge=0)
    processes: int = Field(ge=0)
    timeout_seconds: int = Field(ge=0)


class BuildSecurityProjection(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    capabilities: list[str]


class BuildModelArtifactProjection(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


class ArtifactInputProjection(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    selection_id: str
    artifact_key: str


class ModelRevisionProjection(_CatalogRevisionProjection):
    identity: ModelIdentity
    format: ModelFormat
    artifact_count: int = Field(ge=0)
    download_bytes: int = Field(ge=0)
    installed_bytes: int = Field(ge=0)


class RecipeRevisionProjection(_CatalogRevisionProjection):
    identity: RecipeIdentity
    provenance: RecipeProvenance
    release: RecipeRelease
    models: list[ModelReference] = Field(min_length=1)
    interfaces: list[RecipeInterface]
    validation: RecipeValidation
    base_image_digest: str
    options: list[RecipeOption]
    runtime_engine: str
    topology: RecipeTopology
    build_resources: BuildResourcesProjection | None = None
    build_security: BuildSecurityProjection | None = None
    build_options: RecipeBuildOptions | None = None
    build_model_artifacts: list[BuildModelArtifactProjection] | None = None
    artifact_inputs: list[ArtifactInputProjection] | None = None
    # The CI-built image the signed catalog pins for this revision, if any.
    prebuilt_image: PrebuiltImage | None = None
    # The signed catalog's qualification status for this revision; informative.
    qualification: PublishedQualificationStatusView | None = None
    # What each role of this exact content measured on hardware (the steady peak
    # admission reads); none when the signed catalog publishes none.
    measured_memory: PublishedMeasuredMemory | None = None
    # The signed catalog's gateway API facts for this revision (a sibling of the
    # recipe, never part of its content identity); none when it publishes none.
    api: RecipeApi | None = None


CatalogRevisionProjection = ModelRevisionProjection | RecipeRevisionProjection


class UnprojectedRevision(_CatalogRevisionProjection):
    """The column's own default: a revision whose projection is not derived yet.

    ``catalog_document_revisions.projected`` defaults to ``{}``.  The importer
    always writes the derived projection; a row that still holds the default is
    read as unknown by :func:`read_catalog_projection` and re-derived by the
    catalog's own repair path.
    """

    model_config = ConfigDict(extra="forbid", strict=True)


_CatalogRevisionProjection.model_rebuild()
ModelRevisionProjection.model_rebuild()
RecipeRevisionProjection.model_rebuild()


def _json(value: object) -> bytes:
    try:
        return canonical_message(value)
    except (TypeError, ValueError) as error:
        raise CatalogRevisionContractError(
            "catalog persisted value is not JSON"
        ) from error


def read_catalog_projection(
    revision: CatalogDocumentRevision,
) -> ModelRevisionProjection | RecipeRevisionProjection:
    """Validate a revision projection using the database's decoded JSON value."""

    try:
        projection_type = (
            ModelRevisionProjection
            if revision.kind == "model"
            else RecipeRevisionProjection
        )
        if revision.kind not in {"model", "recipe"}:
            raise ValueError("unknown catalog revision kind")
        return projection_type.model_validate_json(_json(revision.projected))
    except TypeError, ValueError, ValidationError:
        # Projection JSON is disposable. Re-derive it only from a document
        # whose exact content identity still verifies; damaged source JSON
        # remains unavailable until the normal catalog sync re-ingests it.
        parsed = read_catalog_document(revision)
        return rederive_projection(revision.projected, parsed)


def rederive_projection(
    stored: object, parsed: ModelDefinition | RecipeDefinition
) -> ModelRevisionProjection | RecipeRevisionProjection:
    """The current projection of ``parsed``, keeping what is recorded beside it.

    Two sources, one rule: every field the document determines comes from the
    document; every other field (what the catalog sync and the model bindings
    recorded) comes from the stored projection, read one key at a time, so a row
    written under an earlier contract loses no valid recorded state and a stored
    value that is itself invalid is dropped, not carried over.
    """
    if isinstance(parsed, ModelDefinition):
        projection_type: type[ModelRevisionProjection | RecipeRevisionProjection] = (
            ModelRevisionProjection
        )
        projected = model_document_projection(parsed).model_dump(mode="json")
    else:
        projection_type = RecipeRevisionProjection
        projected = recipe_document_projection(parsed)
    recorded = stored if isinstance(stored, Mapping) else {}
    for key in projection_type.model_fields.keys() - projected.keys():
        if recorded.get(key) is None:
            continue
        try:
            projection_type.model_validate_json(
                canonical_message({**projected, key: recorded[key]})
            )
        except ValueError:
            continue  # the next sync records it again from the signed catalog
        projected[key] = recorded[key]
    return projection_type.model_validate_json(canonical_message(projected))


def published_steady_peaks(
    revision: CatalogDocumentRevision,
) -> dict[str, PublishedRoleMemory]:
    """The measured steady peak of each role of ``revision``, by role name.

    Empty when the catalog published none or the projection is unreadable: the
    recipe's declared peak then admits, as before the measurement existed.
    """
    try:
        projected = read_catalog_projection(revision)
    except CatalogRevisionContractError:
        return {}
    if not isinstance(projected, RecipeRevisionProjection) or (
        projected.measured_memory is None
    ):
        return {}
    return {item.role: item for item in projected.measured_memory.roles}


def read_recipe_projection(
    revision: CatalogDocumentRevision,
) -> RecipeRevisionProjection:
    parsed = read_catalog_projection(revision)
    if not isinstance(parsed, RecipeRevisionProjection):
        raise CatalogRevisionContractError("catalog revision is not a recipe")
    return parsed


def write_catalog_projection(
    value: Mapping[str, object] | CatalogRevisionProjection,
    *,
    kind: str | None = None,
) -> dict[str, object]:
    """Normalize a projection for persistence, omitting unused optional fields."""

    try:
        if isinstance(value, (ModelRevisionProjection, RecipeRevisionProjection)):
            parsed = value
        elif kind == "model":
            parsed = ModelRevisionProjection.model_validate_json(_json(value))
        elif kind == "recipe":
            parsed = RecipeRevisionProjection.model_validate_json(_json(value))
        else:
            raise ValueError("catalog projection kind is required")
    except (TypeError, ValueError, ValidationError) as error:
        raise CatalogRevisionContractError("catalog projection is invalid") from error
    stored = dict(
        require_mapping(
            serialize_json_value(parsed), "catalog projection is not a JSON object"
        )
    )
    stored.pop("source_bundle_sha256", None)
    return stored


# Bound retained source and parsed trees by source bytes, rather than catalog
# cardinality. Immutable content is shared by every stored-document consumer.
_DOCUMENT_CACHE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class _CachedDocument:
    source: object
    parsed: ModelDefinition | RecipeDefinition
    source_bytes: int


_document_cache: OrderedDict[tuple[str, str], _CachedDocument] = OrderedDict()
_document_cache_size = 0
_document_cache_lock = Lock()


def cached_catalog_document(
    kind: str, digest: str, raw: object
) -> ModelDefinition | RecipeDefinition:
    """A caller's own copy of the parsed document; the shared parse never changes.

    Callers that only read or validate use :func:`shared_catalog_document`.
    """

    return shared_catalog_document(kind, digest, raw).model_copy(deep=True)


def shared_catalog_document(
    kind: str, digest: str, raw: object
) -> ModelDefinition | RecipeDefinition:
    """The shared parse itself, uncopied: the caller must never mutate it."""

    global _document_cache_size  # noqa: PLW0603 -- module state that tests reset by name
    document = require_mapping(raw, "catalog document is not a JSON object")
    key = (kind, digest)
    with _document_cache_lock:
        previous = _document_cache.get(key)
        if previous is not None and previous.source == document:
            _document_cache.move_to_end(key)
            return previous.parsed
        if kind == "model":
            parsed: ModelDefinition | RecipeDefinition = read_model(document)
        elif kind == ReceiptOwner.RECIPE:
            parsed = read_recipe(document)
        else:
            raise CatalogRevisionContractError(f"unknown catalog kind {kind!r}")
        # Digests are verified at ingress, not on trusted immutable row reads.
        size = len(_json(document))
        if previous is not None:
            _document_cache_size -= _document_cache.pop(key).source_bytes
        if size <= _DOCUMENT_CACHE_BYTES:
            while _document_cache_size + size > _DOCUMENT_CACHE_BYTES:
                _document_cache_size -= _document_cache.popitem(last=False)[
                    1
                ].source_bytes
            _document_cache[key] = _CachedDocument(deepcopy(document), parsed, size)
            _document_cache_size += size
        return parsed


def catalog_document_readable(revision: CatalogDocumentRevision) -> bool:
    """Whether :func:`read_catalog_document` reads ``revision``, without a copy."""

    try:
        shared_catalog_document(
            revision.kind, revision.content_digest, revision.document
        )
    except TypeError, ValueError, ValidationError:
        return False
    return True


def read_catalog_document(
    revision: CatalogDocumentRevision,
) -> ModelDefinition | RecipeDefinition:
    """Parse one persisted published document; its digest is stored with it."""

    return read_shared_catalog_document(revision).model_copy(deep=True)


def read_shared_catalog_document(
    revision: CatalogDocumentRevision,
) -> ModelDefinition | RecipeDefinition:
    """:func:`read_catalog_document` without the copy: never mutate the result."""

    try:
        return shared_catalog_document(
            revision.kind, revision.content_digest, revision.document
        )
    except CatalogRevisionContractError:
        raise
    except (TypeError, ValueError, ValidationError) as error:
        raise CatalogRevisionContractError(
            f"{stored_document_detail(error) or type(error).__name__}; "
            f"catalog {revision.kind} revision {revision.id} "
            f"({revision.publisher}/{revision.slug}@{revision.content_digest})"
        ) from error


def read_model_set(value: object) -> dict[str, ModelDefinition]:
    """Return published Model documents keyed by their document digest.

    Accepts a digest-keyed mapping, or a sequence of stored catalog revisions
    (which carry their digest) or raw published documents (hashed once here).
    """

    if isinstance(value, Mapping):
        items = list(value.items())
    elif isinstance(value, (list, tuple)):
        items = []
        for item in value:
            digest = getattr(item, "content_digest", None)
            document = getattr(item, "document", item)
            if not isinstance(document, Mapping):
                raise TypeError("model projection is not a published document")
            if not isinstance(digest, str):
                digest = document_sha256(document)
            items.append((digest, document))
    else:
        raise TypeError("canonical model projections are missing")
    result: dict[str, ModelDefinition] = {}
    for digest, document in items:
        if not isinstance(digest, str):
            raise TypeError("model digest is invalid")
        result[digest] = (
            document if isinstance(document, ModelDefinition) else read_model(document)
        )
    return result


__all__ = [
    "CatalogRevisionContractError",
    "CatalogRevisionProjection",
    "PublishedDocument",
    "PublishedValue",
    "read_catalog_document",
    "read_catalog_projection",
    "read_model_set",
    "read_shared_catalog_document",
    "rederive_projection",
    "shared_catalog_document",
    "write_catalog_projection",
]


def model_document_projection(model: ModelDefinition) -> ModelRevisionProjection:
    """Every projection field the immutable Model document alone determines."""
    return ModelRevisionProjection(
        identity=model.identity,
        format=model.format,
        artifact_count=len(model.files),
        download_bytes=model.download_bytes,
        installed_bytes=model.installed_bytes,
    )


def recipe_document_projection(recipe: RecipeDefinition) -> dict[str, object]:
    """Every projection field the immutable recipe document alone determines."""
    return {
        "identity": recipe.identity.model_dump(mode="json"),
        "provenance": recipe.provenance.model_dump(mode="json"),
        "release": recipe.release.model_dump(mode="json"),
        "models": [
            selection.model.model_dump(mode="json") for selection in recipe.models
        ],
        "interfaces": [
            interface.model_dump(mode="json") for interface in recipe.interfaces
        ],
        "validation": recipe.validation.model_dump(mode="json"),
        "base_image_digest": recipe.execution.build.base_image.digest,
        "options": [option.model_dump(mode="json") for option in recipe.options],
        "runtime_engine": recipe.runtime.engine,
        "topology": recipe.topology.model_dump(mode="json"),
        **build_policy_projection(recipe),
    }


def build_policy_projection(recipe: RecipeDefinition) -> dict[str, object]:
    """Compile platform-owned rootless build policy for every source recipe.

    These are admission budgets, not measured image sizes. The builder still
    measures and verifies the exported archive before it can be distributed.
    No public Recipe fields or local author overrides grant build authority.
    """
    gib = 1024**3
    disks = [role.resources.disk for role in recipe.topology.roles]
    image_bytes = max(disk.image_bytes for disk in disks)
    return {
        "build_resources": {
            "cpu_cores": 8,
            "download_bytes": image_bytes,
            "temporary_bytes": min(
                16 * 1024**4,
                max(3 * image_bytes, *(disk.working_bytes for disk in disks)),
            ),
            "memory_bytes": min(64 * gib, max(2 * gib, image_bytes)),
            "processes": 4096,
            "timeout_seconds": 86_400,
        },
        # Installation/ownership operations inside the existing rootless
        # namespace only. GPU, host mounts, sockets and privilege stay off.
        "build_security": {
            "capabilities": [
                "CHOWN",
                "DAC_OVERRIDE",
                "FOWNER",
                "FSETID",
                "SETFCAP",
                "SETGID",
                "SETUID",
            ],
        },
        "build_options": {
            "additional_contexts": [],
            "annotations": [],
            "environment": [],
            "format": "oci",
            "identity_label": True,
            "ignorefile": None,
            "jobs": 1,
            "labels": [],
            "layer_compression": "disabled",
            "layer_labels": [],
            "layers": False,
            "no_hostname": False,
            "no_hosts": False,
            "omit_history": False,
            "os_features": [],
            "os_version": None,
            "shm_bytes": 64 * 1024**2,
            "skip_unused_stages": True,
            "squash": "none",
            "timestamp": None,
            "unset_environment": [],
            "unset_labels": [],
        },
    }
