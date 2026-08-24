from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .schema_resources import read_runtime_schema


class CatalogContractError(ValueError):
    def __init__(self, code: str, path: str, detail: str) -> None:
        self.code = code
        self.path = path
        self.detail = detail[:240]
        super().__init__(f"{path}: {self.detail}")


class CatalogKind(StrEnum):
    MODEL_GROUP = "model-group"
    MODEL = "model"
    MODEL_VERSION = "model-version"
    EXECUTION_HARNESS = "execution-harness"
    RUNTIME_DISTRIBUTION = "runtime-distribution"
    PATCH_BUNDLE = "patch-bundle"


@dataclass(frozen=True, slots=True)
class CatalogReference:
    kind: CatalogKind
    publisher: str
    slug: str
    content_sha256: str

    @property
    def portable_identity(self) -> tuple[str, str, str, str]:
        return (self.kind.value, self.publisher, self.slug, self.content_sha256)


_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def _reject_float(_: str) -> None:
    raise CatalogContractError(
        "catalog.float_forbidden", "$", "floats are not permitted"
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CatalogContractError(
                "catalog.duplicate_key", "$", f"duplicate object key: {key}"
            )
        result[key] = value
    return result


def parse_catalog_json(payload: bytes | str) -> Mapping[str, object]:
    try:
        document = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_float,
        )
    except CatalogContractError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CatalogContractError(
            "catalog.invalid_json", "$", "catalog entity is not valid UTF-8 JSON"
        ) from error
    if not isinstance(document, dict):
        raise CatalogContractError(
            "catalog.object_required", "$", "catalog entity must be a JSON object"
        )
    return document


def _assert_canonical_value(value: object, path: str = "$") -> None:
    if isinstance(value, float):
        raise CatalogContractError(
            "catalog.float_forbidden", path, "floats are not permitted"
        )
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise CatalogContractError(
                    "catalog.key_type", path, "object keys must be strings"
                )
            _assert_canonical_value(child, f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            _assert_canonical_value(child, f"{path}[{index}]")
        return
    raise CatalogContractError(
        "catalog.value_type", path, "catalog entity contains an unsupported value type"
    )


def canonical_catalog_document(document: Mapping[str, object]) -> bytes:
    _assert_canonical_value(document)
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def catalog_content_sha256(document: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_catalog_document(document)).hexdigest()


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema = json.loads(read_runtime_schema("catalog-entity-v1.schema.json"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_catalog_document(document: Mapping[str, object]) -> None:
    errors = sorted(
        _validator().iter_errors(document),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        error = _most_specific(errors[0])
        path = ".".join(str(part) for part in error.absolute_path) or "$"
        raise CatalogContractError(
            f"catalog.schema.{error.validator}", path, _safe_detail(error)
        )
    kind = document.get("kind")
    if kind == CatalogKind.MODEL_VERSION.value:
        _validate_model_version(document)
    elif kind == CatalogKind.RUNTIME_DISTRIBUTION.value:
        _validate_runtime_distribution(document)
    elif kind == CatalogKind.PATCH_BUNDLE.value:
        _validate_patch_bundle(document)


def _validate_model_version(document: Mapping[str, object]) -> None:
    artifacts = document.get("artifacts")
    sizes = document.get("sizes")
    source = document.get("source")
    if not isinstance(artifacts, list) or not isinstance(sizes, Mapping):
        raise CatalogContractError(
            "catalog.model_version_shape", "artifacts", "model version inventory is invalid"
        )
    ids = [artifact.get("id") for artifact in artifacts if isinstance(artifact, Mapping)]
    paths = [artifact.get("path") for artifact in artifacts if isinstance(artifact, Mapping)]
    if len(ids) != len(artifacts) or len(set(ids)) != len(ids):
        raise CatalogContractError(
            "catalog.artifact_id", "artifacts", "artifact IDs must be unique"
        )
    if len(set(paths)) != len(paths):
        raise CatalogContractError(
            "catalog.artifact_path", "artifacts", "artifact paths must be unique"
        )
    revisions = {
        artifact.get("revision")
        for artifact in artifacts
        if isinstance(artifact, Mapping)
    }
    if not isinstance(source, Mapping) or revisions != {source.get("revision")}:
        raise CatalogContractError(
            "catalog.artifact_revision",
            "artifacts",
            "every artifact revision must match the immutable model-version source",
        )
    for field in ("download_bytes", "installed_bytes"):
        total = sum(
            int(artifact[field])
            for artifact in artifacts
            if isinstance(artifact, Mapping)
        )
        if sizes.get(field) != total:
            raise CatalogContractError(
                "catalog.artifact_size",
                f"sizes.{field}",
                f"{field} must equal the exact artifact inventory total",
            )


def _validate_runtime_distribution(document: Mapping[str, object]) -> None:
    manifest = document.get("image_manifest")
    image = document.get("image")
    if manifest is not None:
        if not isinstance(manifest, Mapping) or not isinstance(image, str):
            raise CatalogContractError(
                "catalog.image_manifest", "image_manifest", "image manifest is invalid"
            )
        if image.rsplit("@sha256:", 1)[-1] != manifest.get("digest"):
            raise CatalogContractError(
                "catalog.image_manifest",
                "image_manifest.digest",
                "image manifest digest must match the pinned image",
            )
    capabilities = document.get("capabilities")
    distributed_capabilities = (
        tuple(
            (name, capabilities.get(name))
            for name in ("distributed_vllm", "distributed_sglang")
            if isinstance(capabilities.get(name), Mapping)
        )
        if isinstance(capabilities, Mapping)
        else ()
    )
    if not distributed_capabilities:
        return
    if len(distributed_capabilities) != 1:
        raise CatalogContractError(
            "catalog.distributed_runtime",
            "capabilities",
            "runtime distribution must bind exactly one distributed harness capability",
        )
    capability_name, distributed = distributed_capabilities[0]
    assert isinstance(distributed, Mapping)
    harness_slug = "vllm" if capability_name == "distributed_vllm" else "sglang"
    label = "vLLM" if capability_name == "distributed_vllm" else "SGLang"
    harness = document.get("implements_harness")
    dimensions = tuple(
        distributed.get(name)
        for name in (
            "tensor_parallel_size",
            "pipeline_parallel_size",
            "data_parallel_size",
        )
    )
    if (
        not isinstance(harness, Mapping)
        or harness.get("slug") != harness_slug
        or any(type(value) is not int for value in dimensions)
        or dimensions[0] * dimensions[1] * dimensions[2]
        != distributed.get("world_size")
        or distributed.get("world_size") != distributed.get("node_count")
    ):
        raise CatalogContractError(
            f"catalog.{capability_name}",
            f"capabilities.{capability_name}",
            f"distributed {label} capability must bind {label} and an exact rank topology",
        )
    launch = distributed.get("launch")
    rendezvous = launch.get("rendezvous") if isinstance(launch, Mapping) else None
    profiles = launch.get("rank_profiles") if isinstance(launch, Mapping) else None
    node_count = distributed.get("node_count")
    endpoint_role = distributed.get("endpoint_role")
    worker_role = distributed.get("worker_role")
    if (
        not isinstance(rendezvous, Mapping)
        or rendezvous.get("master_role") != endpoint_role
        or not isinstance(profiles, list)
        or type(node_count) is not int
        or len(profiles) != node_count
        or any(not isinstance(profile, Mapping) for profile in profiles)
        or [profile.get("rank") for profile in profiles] != list(range(node_count))
        or profiles[0].get("role") != endpoint_role
        or any(profile.get("role") != worker_role for profile in profiles[1:])
    ):
        raise CatalogContractError(
            f"catalog.{capability_name}_launch",
            f"capabilities.{capability_name}.launch",
            f"distributed {label} launch profiles must bind every exact rank and rendezvous role",
        )


def _validate_patch_bundle(document: Mapping[str, object]) -> None:
    patches = document.get("patches")
    if patches is None:
        return
    if not isinstance(patches, list) or [
        patch.get("order") if isinstance(patch, Mapping) else None for patch in patches
    ] != list(range(1, len(patches) + 1)):
        raise CatalogContractError(
            "catalog.patch_order",
            "patches",
            "patch order must be contiguous and start at one",
        )


def parse_catalog_reference(
    value: Mapping[str, object], *, expected_kind: CatalogKind | None = None
) -> CatalogReference:
    if set(value) != {"kind", "publisher", "slug", "content_sha256"}:
        raise CatalogContractError(
            "catalog.reference_shape", "$", "reference must contain exactly kind, publisher, slug, and content_sha256"
        )
    try:
        kind = CatalogKind(value["kind"])
    except (KeyError, TypeError, ValueError) as error:
        raise CatalogContractError(
            "catalog.reference_kind", "kind", "reference kind is not supported"
        ) from error
    publisher = value["publisher"]
    slug = value["slug"]
    content_sha256 = value["content_sha256"]
    if not isinstance(publisher, str) or not _SLUG.fullmatch(publisher):
        raise CatalogContractError(
            "catalog.reference_publisher", "publisher", "publisher must be a catalog slug"
        )
    if not isinstance(slug, str) or not _SLUG.fullmatch(slug):
        raise CatalogContractError(
            "catalog.reference_slug", "slug", "slug must be a catalog slug"
        )
    if not isinstance(content_sha256, str) or not _SHA256.fullmatch(content_sha256):
        raise CatalogContractError(
            "catalog.reference_digest", "content_sha256", "content_sha256 must be a lowercase SHA-256 digest"
        )
    if expected_kind is not None and kind is not expected_kind:
        raise CatalogContractError(
            "catalog.reference_kind", "kind", f"reference kind must be {expected_kind.value}"
        )
    return CatalogReference(kind, publisher, slug, content_sha256)


def _most_specific(error: ValidationError) -> ValidationError:
    candidates = [error]
    pending = list(error.context)
    while pending:
        candidate = pending.pop()
        candidates.append(candidate)
        pending.extend(candidate.context)
    return max(
        candidates,
        key=lambda candidate: (
            len(candidate.absolute_path),
            candidate.validator == "additionalProperties",
            candidate.validator == "unevaluatedProperties",
            candidate.validator == "required",
            -len(candidate.context),
        ),
    )


def _safe_detail(error: ValidationError) -> str:
    field = str(error.absolute_path[-1]) if error.absolute_path else "catalog entity"
    if error.validator == "required":
        missing = sorted(set(error.validator_value) - set(error.instance))
        return f"required field missing: {missing[0]}"
    if error.validator in {"additionalProperties", "unevaluatedProperties"}:
        allowed = set(error.schema.get("properties", {}))
        extra = sorted(set(error.instance) - allowed)
        return f"{error.validator}: unexpected field: {extra[0] if extra else field}"
    if error.validator == "const":
        return f"{field} must equal {error.validator_value!r}"
    if error.validator == "pattern":
        return f"{field} does not match the required format"
    return f"{field} violates the {error.validator} constraint"
