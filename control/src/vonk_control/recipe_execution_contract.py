"""Strict persisted contracts for recipe execution and source builds.

The database JSON columns in this module are durable boundaries between
admission/build producers and later operation consumers.  They deliberately
use the same JSON-mode validation as the agent wire protocol: a database
driver may have already decoded the value to Python objects, but that does
not make Python coercion part of the contract.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator
from vonk_agent_protocol import (
    CompiledExecutionPlan,
    RecipeBuildRequest,
    canonical_message,
)
from vonk_agent_protocol.compiled_execution_plan import COMPILED_PLAN_STORAGE_CONTEXT
from vonk_agent_protocol.inventory import MemoryPool

from .library_contract import Digest, ImageDigest, NodeId, Text64, UuidId
from .strict_json import StrictJSONModel
from .validation_detail import validation_error_detail

if TYPE_CHECKING:
    from .models import RecipeInstallation

DateTimeString = Annotated[
    str,
    Field(json_schema_extra={"format": "date-time"}),
]


class RecipeExecutionContractError(ValueError):
    """A persisted recipe execution document is not the current contract.

    ``detail`` is the bounded "field:rule:message" suffix derived where the
    pydantic error was raised.  A caller that wraps this error into its own
    operator-facing sentence can append it, so the failing rule survives even
    though the wrapper replaces the message.
    """

    def __init__(self, message: str, *, detail: str = "") -> None:
        self.detail = detail
        super().__init__(message)


class _PersistedModel(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class StoredAdmissionReason(_PersistedModel):
    code: str = Field(min_length=1, max_length=80)
    detail: str = Field(min_length=1, max_length=512)


class StoredInstallNodePlan(_PersistedModel):
    node_id: NodeId
    rank: int = Field(ge=0)
    role: Text64
    allowed: bool
    # These nullable fields are required: their null is an observation, not
    # an omitted optional default.
    inventory_observed_at: DateTimeString | None
    free_bytes: int | None = Field(ge=0)
    active_reserved_bytes: int = Field(ge=0)
    reused_bytes: int = Field(ge=0)
    required_download_bytes: int = Field(ge=0)
    required_bytes: int = Field(ge=0)
    # Optional with a declared ``None`` default: plans admitted before the
    # payload expectation existed omit the field, and omission, explicit
    # ``null`` and an absent expectation are the same observation.  It is
    # omitted again on serialization, so re-reading and re-writing a stored
    # document leaves its canonical bytes (and recorded plan digest) unchanged.
    required_payload_bytes: int | None = Field(default=None, ge=0)
    disk_floor_bytes: int = Field(ge=0)
    free_after_bytes: int | None
    blockers: list[StoredAdmissionReason]
    warnings: list[StoredAdmissionReason]

    @field_validator("inventory_observed_at")
    @classmethod
    def observation_timestamp_is_aware(cls, value: str | None) -> str | None:
        if value is not None and datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("observation timestamp must include a timezone")
        return value


class StoredInstallationPlan(_PersistedModel):
    schema_version: Literal[1]
    mapping_id: UuidId
    mapping_generation: int = Field(ge=1)
    recipe_build_id: UuidId | None
    image_digest: ImageDigest
    recipe_revision_id: UuidId
    recipe_content_sha256: Digest
    allowed: bool
    nodes: list[StoredInstallNodePlan]
    plan_digest: Digest
    compiled_execution_plans: dict[NodeId, CompiledExecutionPlan]

    @model_validator(mode="after")
    def node_and_compiled_ids_match(self) -> StoredInstallationPlan:
        node_ids = [node.node_id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("installation plan nodes must be unique")
        if set(node_ids) != set(self.compiled_execution_plans):
            raise ValueError("installation plan compiled documents do not match nodes")
        return self


class StoredRunNodePlan(_PersistedModel):
    node_id: NodeId
    rank: int = Field(ge=0)
    role: Text64
    endpoint_owner: bool
    port: int = Field(ge=1, le=65535)
    allowed: bool
    inventory_observed_at: DateTimeString | None
    memory_kind: Literal["unified", "host", "accelerator"]
    memory_pool: MemoryPool
    required_memory_bytes: int = Field(ge=0)
    available_memory_bytes: int | None
    active_reserved_bytes: int = Field(ge=0)
    free_after_bytes: int | None
    memory_floor_bytes: int = Field(ge=0)
    fabric_address: str | None
    fabric_bandwidth_mbps: int | None
    rendezvous_port: int | None
    blockers: list[StoredAdmissionReason]
    warnings: list[StoredAdmissionReason]

    @field_validator("inventory_observed_at")
    @classmethod
    def observation_timestamp_is_aware(cls, value: str | None) -> str | None:
        if value is not None and datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("observation timestamp must include a timezone")
        return value


class StoredRunPlan(_PersistedModel):
    schema_version: Literal[1]
    observation_schema_version: Literal[2]
    run_generation: int = Field(ge=1)
    installation_id: UuidId
    alias: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$")
    mapping_id: UuidId
    mapping_generation: int = Field(ge=1)
    recipe_revision_id: UuidId
    plan_digest: Digest
    nodes: list[StoredRunNodePlan]
    # This is added only for one-shot logical jobs.  Its omission is the
    # declared optional-default form; explicit null is normalized away.
    execution_mode: Literal["one-shot-jobs"] | None = None

    @model_validator(mode="after")
    def nodes_are_unique(self) -> StoredRunPlan:
        node_ids = [node.node_id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("run plan nodes must be unique")
        return self


class StoredRunEndpoint(_PersistedModel):
    url: str = Field(min_length=1, max_length=2048)


class StoredPolicyFinding(_PersistedModel):
    code: str
    path: str
    line: int | None = Field(ge=1)
    detail: str


class StoredBuildPolicyReport(_PersistedModel):
    passed: bool
    source_bundle_sha256: Digest
    dockerfile: str
    findings: list[StoredPolicyFinding]
    builder_binary_digest: Digest | None = None
    artifact_format: str = Field(min_length=1, max_length=64)


def _validate_json[ModelT: _PersistedModel](
    value: object, model: type[ModelT], label: str, *, context: object = None
) -> ModelT:
    try:
        # Always take the JSON path.  This keeps decoded DB arrays/objects
        # subject to the same strict semantics as bytes received on the wire.
        return model.model_validate_json(canonical_message(value), context=context)
    except (TypeError, ValueError) as error:
        detail = validation_error_detail(error)
        raise RecipeExecutionContractError(
            f"{label} is invalid{detail}", detail=detail
        ) from error


def _document(model: _PersistedModel) -> dict[str, object]:
    return json.loads(canonical_message(model))


def parse_stored_installation_plan(
    value: object, *, for_uninstall: bool = False
) -> StoredInstallationPlan:
    return _validate_json(
        value,
        StoredInstallationPlan,
        "stored installation plan",
        context=COMPILED_PLAN_STORAGE_CONTEXT if for_uninstall else None,
    )


def installation_plan_document(
    value: object, *, for_uninstall: bool = False
) -> dict[str, object]:
    return _document(parse_stored_installation_plan(value, for_uninstall=for_uninstall))


def installation_matches_runtime_image(
    installation: RecipeInstallation,
    *,
    build_id: str | None,
    image_digest: str | None,
    oci_layout_sha256: str | None,
    image_bytes: int | None,
) -> bool:
    """Whether the stored execution plan uses this exact immutable image.

    A build row can acquire a different result after repair. Its ID and the
    installation's state cannot establish that existing compiled ranks use
    the newly selected image or archive.
    """
    if (
        image_digest is None
        or oci_layout_sha256 is None
        or image_bytes is None
        or installation.recipe_build_id != build_id
        or installation.image_digest != image_digest
    ):
        return False
    plan = parse_stored_installation_plan(installation.plan)
    expected = {
        "build_id": build_id,
        "image_digest": image_digest,
        "oci_layout_sha256": oci_layout_sha256,
        "image_bytes": image_bytes,
    }
    return (
        plan.mapping_id == installation.mapping_id
        and plan.mapping_generation == installation.mapping_generation
        and plan.recipe_revision_id == installation.recipe_revision_id
        and plan.plan_digest == installation.plan_digest
        and plan.recipe_build_id == build_id
        and plan.image_digest == image_digest
        and bool(plan.compiled_execution_plans)
        and all(
            all(
                getattr(compiled.runtime_image, name) == value
                for name, value in expected.items()
            )
            for compiled in plan.compiled_execution_plans.values()
        )
    )


def parse_stored_run_plan(value: object) -> StoredRunPlan:
    return _validate_json(value, StoredRunPlan, "stored run plan")


def run_plan_document(value: object) -> dict[str, object]:
    return _document(parse_stored_run_plan(value))


def parse_stored_build_plan(value: object) -> RecipeBuildRequest:
    try:
        return RecipeBuildRequest.model_validate_json(canonical_message(value))
    except (TypeError, ValueError) as error:
        detail = validation_error_detail(error)
        raise RecipeExecutionContractError(
            "stored recipe build plan is invalid" + detail, detail=detail
        ) from error


def build_plan_document(value: object) -> dict[str, object]:
    return json.loads(canonical_message(parse_stored_build_plan(value)))


def parse_stored_build_policy(value: object) -> StoredBuildPolicyReport:
    return _validate_json(value, StoredBuildPolicyReport, "stored build policy report")


def build_policy_document(value: object) -> dict[str, object]:
    return _document(parse_stored_build_policy(value))


def parse_stored_run_endpoint(value: object) -> StoredRunEndpoint | None:
    if value is None:
        return None
    return _validate_json(value, StoredRunEndpoint, "stored run endpoint")


def run_endpoint_document(value: object) -> dict[str, object] | None:
    endpoint = parse_stored_run_endpoint(value)
    return None if endpoint is None else _document(endpoint)
