"""Closed declarative protocol for digest-bound recipe lifecycle work."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError, model_validator

from .compiled_execution_plan import CompiledExecutionPlan
from .contracts import (
    AgentOperation,
    AgentProtocolError,
    canonical_message,
)
from .host_helper import RecipeReconciliationIdentity
from .wire_model import MAX_RUN_GENERATION, WireModel

RECIPE_OPERATIONS = frozenset(
    {
        AgentOperation.RECIPE_INSTALL,
        AgentOperation.RECIPE_START,
        AgentOperation.RECIPE_STOP,
        AgentOperation.RECIPE_UNINSTALL,
        AgentOperation.RECIPE_RECONCILE,
    }
)

_UUID_PATTERN = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
CanonicalUuid = Annotated[str, Field(pattern=f"^{_UUID_PATTERN}$")]
ByteCount = Annotated[int, Field(ge=0, le=16 * 1024**4)]
RunGeneration = Annotated[int, Field(ge=1, le=MAX_RUN_GENERATION, strict=True)]


class _StrictPayload(WireModel):
    """Base for immutable, exact recipe lifecycle payloads."""


class RecipeInstallPayload(_StrictPayload):
    installation_id: CanonicalUuid
    plan_digest: Digest
    expected_bytes: ByteCount
    compiled_execution_plan: CompiledExecutionPlan


class RecipeStartPayload(_StrictPayload):
    """Start one rank; placement, image and addresses come from the plan."""

    run_id: CanonicalUuid
    installation_id: CanonicalUuid
    recipe_revision_id: CanonicalUuid
    mapping_id: CanonicalUuid
    plan_digest: Digest
    compiled_execution_plan: CompiledExecutionPlan
    phase: Literal["rank-launch", "collective-readiness"] | None = None
    start_deadline: str | None = Field(
        default=None, json_schema_extra={"format": "date-time"}
    )
    run_generation: RunGeneration

    @model_validator(mode="after")
    def start_is_serving(self) -> RecipeStartPayload:
        plan = self.compiled_execution_plan
        placement = plan.runtime.placement
        if plan.endpoint is None or placement.port is None:
            raise ValueError("start requires a serving plan")
        if placement.world_size > 1 and (
            placement.local_address is None
            or placement.master_address is None
            or placement.master_port is None
        ):
            raise ValueError("distributed rendezvous is invalid")
        if placement.world_size == 1 and (
            self.phase is not None or self.start_deadline is not None
        ):
            raise ValueError("single-node start phases are invalid")
        if (self.phase is None) != (self.start_deadline is None):
            raise ValueError("start phase binding is invalid")
        if self.start_deadline is not None:
            try:
                deadline = datetime.fromisoformat(self.start_deadline)
            except ValueError as error:
                raise ValueError("start deadline is invalid") from error
            if deadline.tzinfo is None or deadline.utcoffset() != UTC.utcoffset(
                deadline
            ):
                raise ValueError("start deadline must be UTC")
        return self


class RecipeStopPayload(_StrictPayload):
    """Exact cleanup authority, independent of historical launch-plan readability.

    The Controller binds these identities and timeout into the signed helper
    grant; the helper reconciles only the matching runtime generation.
    """

    run_id: CanonicalUuid
    target_runtime_id: CanonicalUuid
    run_generation: RunGeneration
    installation_id: CanonicalUuid
    recipe_revision_id: CanonicalUuid
    mapping_id: CanonicalUuid
    plan_digest: Digest
    rank: Annotated[int, Field(ge=0, strict=True)]
    role: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
    recipe_content_sha256: Digest
    stop_timeout_seconds: Annotated[int, Field(ge=1, le=600, strict=True)]
    cancel_pending_start: bool = False


class RecipeUninstallPayload(_StrictPayload):
    installation_id: CanonicalUuid
    plan_digest: Digest
    recipe_content_sha256: Digest
    # The key is required on the wire.  Null means this operation retains the
    # shared model cache and is deliberately different from an omitted key.
    cleanup_model_content_sha256: Digest | None


class RecipeStopResult(_StrictPayload):
    """A stop succeeds with an empty result."""


class RecipeUninstallResult(_StrictPayload):
    """An uninstall succeeds with an empty result."""


class RecipeReconcilePayload(RecipeReconciliationIdentity):
    """Authority to remove one managed install with an invalid launch contract."""


class RecipeReconcileResult(_StrictPayload):
    """A reconciliation succeeds with an empty result."""


_REQUEST_MODELS = {
    AgentOperation.RECIPE_INSTALL: RecipeInstallPayload,
    AgentOperation.RECIPE_START: RecipeStartPayload,
    AgentOperation.RECIPE_STOP: RecipeStopPayload,
    AgentOperation.RECIPE_UNINSTALL: RecipeUninstallPayload,
    AgentOperation.RECIPE_RECONCILE: RecipeReconcilePayload,
}


class RecipeOperationRequest(_StrictPayload):
    """Typed lifecycle request envelope with an operation-specific payload."""

    operation: AgentOperation
    payload: (
        RecipeInstallPayload
        | RecipeStartPayload
        | RecipeStopPayload
        | RecipeUninstallPayload
        | RecipeReconcilePayload
    )

    @classmethod
    def parse(cls, operation: AgentOperation, payload: Any) -> RecipeOperationRequest:
        if operation not in RECIPE_OPERATIONS:
            raise AgentProtocolError("recipe operation is not supported")
        try:
            model = _REQUEST_MODELS[operation]
            typed = model.model_validate_json(canonical_message(payload))
            return cls(operation=operation, payload=typed)
        except (ValidationError, TypeError, ValueError) as error:
            raise AgentProtocolError(
                f"{operation.value.removeprefix('recipe ')} payload is invalid"
            ) from error

    @model_validator(mode="after")
    def operation_matches_payload(self) -> RecipeOperationRequest:
        try:
            model = _REQUEST_MODELS[self.operation]
        except KeyError:
            model = None
        if model is None or not isinstance(self.payload, model):
            raise ValueError("recipe operation payload type does not match operation")
        return self

    @property
    def plan_digest(self) -> str:
        return self.payload.plan_digest

    @property
    def installation_id(self) -> str | None:
        return getattr(self.payload, "installation_id", None)

    @property
    def recipe_revision_id(self) -> str | None:
        return getattr(self.payload, "recipe_revision_id", None)

    @property
    def mapping_id(self) -> str | None:
        return getattr(self.payload, "mapping_id", None)

    @property
    def phase(self) -> str | None:
        return getattr(self.payload, "phase", None)

    @property
    def start_deadline(self) -> str | None:
        return getattr(self.payload, "start_deadline", None)

    @property
    def run_generation(self) -> int | None:
        return getattr(self.payload, "run_generation", None)

    @property
    def expected_bytes(self) -> int | None:
        return getattr(self.payload, "expected_bytes", None)

    @property
    def run_id(self) -> str | None:
        return getattr(self.payload, "run_id", None)

    @property
    def compiled_execution_plan(self) -> CompiledExecutionPlan | None:
        return getattr(self.payload, "compiled_execution_plan", None)

    @property
    def cleanup_model_content_sha256(self) -> str | None:
        return getattr(self.payload, "cleanup_model_content_sha256", None)


def parse_recipe_operation_result(
    operation: AgentOperation, result: Any
) -> RecipeStopResult | RecipeUninstallResult | RecipeReconcileResult:
    """Parse a successful stop or uninstall result exactly."""

    result_models = {
        AgentOperation.RECIPE_STOP: RecipeStopResult,
        AgentOperation.RECIPE_UNINSTALL: RecipeUninstallResult,
        AgentOperation.RECIPE_RECONCILE: RecipeReconcileResult,
    }
    try:
        model = result_models[operation]
        return model.model_validate_json(canonical_message(result))
    except (KeyError, ValidationError, TypeError, ValueError) as error:
        raise AgentProtocolError("recipe operation result is invalid") from error


__all__ = [
    "RECIPE_OPERATIONS",
    "RecipeInstallPayload",
    "RecipeOperationRequest",
    "RecipeReconcilePayload",
    "RecipeReconcileResult",
    "RecipeStartPayload",
    "RecipeStopPayload",
    "RecipeStopResult",
    "RecipeUninstallPayload",
    "RecipeUninstallResult",
    "parse_recipe_operation_result",
]
