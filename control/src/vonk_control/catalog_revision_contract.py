"""Typed readers and writers for immutable catalog revision JSON columns."""

from __future__ import annotations

from collections.abc import Mapping
from functools import lru_cache
from typing import TYPE_CHECKING, Literal

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ConfigDict, Field, ValidationError
from vonk_agent_protocol import canonical_message
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256
from vonk_forge_contracts.model import ModelIdentity
from vonk_forge_contracts.recipe import RecipeTopology

from .strict_json import StrictJSONModel, serialize_json_value
from .schema_resources import read_runtime_schema

if TYPE_CHECKING:
    from .models import CatalogDocumentRevision


class CatalogRevisionContractError(ValueError):
    """A persisted catalog document or projection is not current contract JSON."""


class _CatalogRevisionProjection(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    failure_reason: str | None = None
    publication_commit: str | None = None
    source_path: str | None = None
    package_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_bundle_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    package_handle: "RecipePackageHandleProjection" | None = None
    release_version: str | None = None
    release_released_at: str | None = None
    test_report: dict[str, object] | None = None


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


class BuildOptionsProjection(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    additional_contexts: list[object]
    annotations: list[object]
    environment: list[object]
    format: str
    identity_label: bool
    ignorefile: str | None
    jobs: int = Field(ge=0)
    labels: list[object]
    layer_compression: str
    layer_labels: list[object]
    layers: bool
    no_hostname: bool
    no_hosts: bool
    omit_history: bool
    os_features: list[object]
    os_version: str | None
    shm_bytes: int = Field(ge=0)
    skip_unused_stages: bool
    squash: str
    timestamp: str | None
    unset_environment: list[str]
    unset_labels: list[str]


class BuildModelArtifactProjection(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


class ArtifactInputProjection(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    selection_id: str
    artifact_key: str


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


class ModelRevisionProjection(_CatalogRevisionProjection):
    identity: ModelIdentity
    modalities: list[Literal["text", "image", "audio", "video", "3d", "embeddings"]]
    artifact_count: int = Field(ge=0)
    download_bytes: int = Field(ge=0)
    installed_bytes: int = Field(ge=0)


class RecipeRevisionProjection(_CatalogRevisionProjection):
    title: str
    description: str
    tags: list[str]
    runtime_engine: str
    topology: RecipeTopology
    build_resources: BuildResourcesProjection | None = None
    build_security: BuildSecurityProjection | None = None
    build_options: BuildOptionsProjection | None = None
    build_model_artifacts: list[BuildModelArtifactProjection] | None = None
    build_topology_inputs: dict[str, object] | None = None
    artifact_inputs: list[ArtifactInputProjection] | None = None


CatalogRevisionProjection = ModelRevisionProjection | RecipeRevisionProjection

_CatalogRevisionProjection.model_rebuild()
ModelRevisionProjection.model_rebuild()
RecipeRevisionProjection.model_rebuild()


def _json(value: object) -> bytes:
    try:
        return canonical_message(value)
    except (TypeError, ValueError) as error:
        raise CatalogRevisionContractError("catalog persisted value is not JSON") from error


@lru_cache(maxsize=1)
def _test_report_validator() -> Draft202012Validator:
    import json

    schema = json.loads(read_runtime_schema("test-report-v1.schema.json"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def read_catalog_projection(
    revision: CatalogDocumentRevision,
) -> ModelRevisionProjection | RecipeRevisionProjection:
    """Validate a revision projection using the database's decoded JSON value."""

    try:
        projection_type = (
            ModelRevisionProjection if revision.kind == "model" else RecipeRevisionProjection
        )
        if revision.kind not in {"model", "recipe"}:
            raise ValueError("unknown catalog revision kind")
        parsed = projection_type.model_validate_json(_json(revision.projected))
        if revision.kind == "recipe":
            recipe = read_catalog_document(revision)
            assert isinstance(parsed, RecipeRevisionProjection)
            if recipe.execution.mode == "build" and any(
                value is None
                for value in (parsed.build_resources, parsed.build_security, parsed.build_options)
            ):
                raise ValueError("source-build projection is incomplete")
            if parsed.test_report is not None and not _test_report_validator().is_valid(
                parsed.test_report
            ):
                raise ValueError("catalog test report projection is invalid")
        return parsed
    except (TypeError, ValueError, ValidationError) as error:
        raise CatalogRevisionContractError(
            f"catalog revision {revision.id} has invalid projected data"
        ) from error


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
    if parsed.test_report is not None and not _test_report_validator().is_valid(
        parsed.test_report
    ):
        raise CatalogRevisionContractError("catalog test report projection is invalid")
    return serialize_json_value(parsed)  # type: ignore[return-value]


def read_catalog_document(revision: CatalogDocumentRevision) -> ModelDefinition | RecipeDefinition:
    """Read and authenticate one persisted canonical public document."""

    try:
        if revision.kind == "model":
            parsed: ModelDefinition | RecipeDefinition = ModelDefinition.model_validate_json(
                _json(revision.document)
            )
        elif revision.kind == "recipe":
            parsed = RecipeDefinition.model_validate_json(_json(revision.document))
        else:
            raise CatalogRevisionContractError(
                f"catalog revision {revision.id} has unknown kind {revision.kind!r}"
            )
        if revision.content_digest != content_sha256(parsed):
            raise CatalogRevisionContractError(
                f"catalog revision {revision.id} document digest does not match"
            )
        if parsed.identity.publisher != revision.publisher or parsed.identity.slug != revision.slug:
            raise CatalogRevisionContractError(
                f"catalog revision {revision.id} document identity does not match"
            )
        return parsed
    except CatalogRevisionContractError:
        raise
    except (TypeError, ValueError, ValidationError) as error:
        raise CatalogRevisionContractError(
            f"catalog revision {revision.id} has invalid document data"
        ) from error


__all__ = [
    "CatalogRevisionContractError",
    "CatalogRevisionProjection",
    "read_catalog_document",
    "read_catalog_projection",
    "write_catalog_projection",
]
