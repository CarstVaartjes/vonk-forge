"""Install admission: contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from vonk_agent_protocol import (
    InstallAdmissionCode,
    InvalidRequestError,
    RuntimePreflightCode,
    UnknownOutcomeError,
    WaitReason,
)
from vonk_agent_protocol.compiled_execution_plan import (
    CompiledExecutionPlan as WireCompiledExecutionPlan,
)

from ..recipe_execution_contract import (
    StoredInstallationPlan,
    parse_stored_installation_plan,
)

# Sparks pull runtime images from the Controller's layered image store; an
# agent without this capability cannot install or start any recipe.


IMAGE_PULL_CAPABILITY = "recipe.image.pull.v1"

AGENT_UPGRADE_REQUIRED_DETAIL = (
    "Upgrade the Spark agent: this agent cannot pull runtime images from the "
    "Controller's layered image store."
)


@dataclass(frozen=True, slots=True)
class AdmissionReason:
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class InstallNodePlan:
    node_id: str
    rank: int
    role: str
    allowed: bool
    inventory_observed_at: datetime | None
    free_bytes: int | None
    active_reserved_bytes: int
    reused_bytes: int
    required_download_bytes: int
    required_bytes: int
    #: The model payload the compiled plan materializes into the installation
    #: tree.  ``required_bytes`` is the disk reservation, so the presence health
    #: check compares the agent's measured tree against this payload instead.
    required_payload_bytes: int
    disk_floor_bytes: int
    free_after_bytes: int | None
    blockers: tuple[AdmissionReason, ...]
    warnings: tuple[AdmissionReason, ...]


@dataclass(frozen=True, slots=True)
class InstallPlan:
    mapping_id: str
    mapping_generation: int
    recipe_build_id: str | None
    image_digest: str
    recipe_revision_id: str
    recipe_content_sha256: str
    allowed: bool
    nodes: tuple[InstallNodePlan, ...]
    plan_digest: str
    # One strict Controller-issued launch document per mapped node.  It is
    # appended to preserve the positional shape used by older in-process
    # callers; production admission populates it before accepting an install.
    compiled_execution_plans: tuple[tuple[str, WireCompiledExecutionPlan], ...] = ()

    @property
    def compiled_plan_by_node(self) -> dict[str, WireCompiledExecutionPlan]:
        return dict(self.compiled_execution_plans)

    def stored_plan(self) -> StoredInstallationPlan:
        return parse_stored_installation_plan(
            {
                "schema_version": 1,
                "mapping_id": self.mapping_id,
                "mapping_generation": self.mapping_generation,
                "recipe_build_id": self.recipe_build_id,
                "image_digest": self.image_digest,
                "recipe_revision_id": self.recipe_revision_id,
                "recipe_content_sha256": self.recipe_content_sha256,
                "allowed": self.allowed,
                "plan_digest": self.plan_digest,
                "compiled_execution_plans": {
                    node_id: compiled.model_dump(mode="json")
                    for node_id, compiled in self.compiled_plan_by_node.items()
                },
                "nodes": [_node_document(item) for item in self.nodes],
            }
        )


class InstallPlanConflict(RuntimeError):
    code = InstallAdmissionCode.PLAN_INVALID


class InstallPlanStale(InvalidRequestError, InstallPlanConflict):
    """The plan no longer fits the request or the recipe: the caller re-plans."""


class InstallEvidenceChanged(UnknownOutcomeError, InstallPlanConflict):
    """Evidence the plan rested on moved or is unavailable: observe and retry."""


class StoredInstallIdentityDamaged(UnknownOutcomeError, ValueError, TypeError):
    """A stored installation identity that does not parse: unknown, to rebuild."""


class InstallAdmissionBusy(UnknownOutcomeError, InstallPlanConflict):
    """A capacity writer owns the row; retry only after releasing this transaction."""

    code = InstallAdmissionCode.CAPACITY_BUSY

    def __init__(
        self,
        *args: object,
        reason: WaitReason | None = WaitReason.OBSERVATION_UNAVAILABLE,
    ) -> None:
        super().__init__(*args, reason=reason)


class InstallPreflightExpired(InstallAdmissionBusy):
    """The exact plan is admissible except that its runtime evidence expired."""

    code = RuntimePreflightCode.STALE

    def __init__(
        self,
        code: str = RuntimePreflightCode.STALE,
        detail: str | None = None,
        *,
        reason: WaitReason | None = WaitReason.STALE_PLAN,
    ):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code, reason=reason)


#: Starts the detail of a ``compiled_plan_unavailable`` blocker whose cause is an
#: outcome that cannot be confirmed yet (as opposed to a plan that is invalid):
#: the install waits and is admitted again instead of ending as blocked.
UNSETTLED_PLAN_PREFIX = "Waiting for evidence: "

_RETRYABLE_INSTALL_BLOCKERS = {
    InstallAdmissionCode.INVENTORY_MISSING,
    InstallAdmissionCode.STALE_INVENTORY,
    InstallAdmissionCode.INSUFFICIENT_DISK,
    InstallAdmissionCode.ARTIFACT_STORE_READ_ONLY,
    InstallAdmissionCode.IMAGE_DISTRIBUTION_PENDING,
    RuntimePreflightCode.HOST_CHANGED,
    RuntimePreflightCode.REQUIREMENTS_CHANGED,
    RuntimePreflightCode.STALE,
}

_REFRESHABLE_PREFLIGHT_BLOCKERS = {
    RuntimePreflightCode.HOST_CHANGED,
    RuntimePreflightCode.REQUIREMENTS_CHANGED,
    RuntimePreflightCode.STALE,
}


def _node_document(node: InstallNodePlan) -> dict[str, object]:
    return {
        **asdict(node),
        "inventory_observed_at": (
            node.inventory_observed_at.isoformat()
            if node.inventory_observed_at
            else None
        ),
    }
