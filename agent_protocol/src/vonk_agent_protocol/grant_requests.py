"""The Controller's host-helper and package grant requests, and the grant reply.

``_AgentGrantRequest`` is a shared base, private so the wire export does not name
it: the generated Rust has one type per message, not per base.
"""

from __future__ import annotations

from typing import Annotated, NamedTuple

from pydantic import ConfigDict, Field, model_validator

from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.host_helper import (
    ContainerRuntimeAction,
    ContainerRuntimeActionName,
    SignedHostHelperGrant,
)
from vonk_agent_protocol.integer_domains import MAX_DATABASE_BIGINT
from vonk_agent_protocol.package_upgrade import PackageActivationReceipt
from vonk_agent_protocol.recipe_operations import RecipeReconciliationIdentity
from vonk_agent_protocol.wire_model import UUID4_PATTERN, StrictJSONModel


class _AgentGrantRequest(StrictJSONModel):
    """A helper grant for the attempt the fence names; mTLS names the node."""

    model_config = ConfigDict(extra="forbid", strict=True)
    fence: str = Field(pattern=UUID4_PATTERN)
    expires_in_seconds: int = Field(ge=1, le=300)


class HostRuntimeGrantRequest(_AgentGrantRequest):
    installation_intent_nonce: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    action: ContainerRuntimeActionName
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    start_plan_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    stop_plan_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    run_generation: (
        Annotated[int, Field(ge=1, le=MAX_DATABASE_BIGINT, strict=True)] | None
    ) = None
    runtime_run_id: str | None = Field(default=None, pattern=UUID4_PATTERN)
    runtime_target_id: str | None = Field(default=None, pattern=UUID4_PATTERN)
    runtime_installation_id: str | None = Field(default=None, pattern=UUID4_PATTERN)
    installation_id: str | None = Field(default=None, pattern=UUID4_PATTERN)
    reconciliation_identity: RecipeReconciliationIdentity | None = None

    @model_validator(mode="after")
    def runtime_binding(self) -> HostRuntimeGrantRequest:
        if (self.installation_id is not None) != (
            self.action == "installation-cleanup"
        ):
            raise ValueError("host runtime installation binding is invalid")
        has_runtime_binding = any(
            value is not None
            for value in (
                self.start_plan_sha256,
                self.stop_plan_sha256,
                # An inspection may ask to be bound to its run's generation.
                None
                if self.action == ContainerRuntimeAction.RUN_INSPECT
                else self.run_generation,
                self.runtime_run_id,
                self.runtime_target_id,
                self.runtime_installation_id,
            )
        )
        if self.action in {"start", "stop"}:
            expected_plan = (
                self.start_plan_sha256
                if self.action == "start"
                else self.stop_plan_sha256
            )
            unexpected_plan = (
                self.stop_plan_sha256
                if self.action == "start"
                else self.start_plan_sha256
            )
            if (
                expected_plan is None
                or unexpected_plan is not None
                or self.run_generation is None
                or self.runtime_run_id is None
                or self.runtime_target_id is None
                or self.runtime_installation_id is None
            ):
                raise ValueError("host runtime plan binding is invalid")
        elif has_runtime_binding:
            raise ValueError("host runtime plan binding does not match the action")
        if self.reconciliation_identity is not None and (
            self.action != "installation-cleanup"
            or self.reconciliation_identity.installation_id != self.installation_id
        ):
            raise ValueError("host runtime reconciliation binding is invalid")
        return self


class OrphanStopBinding(NamedTuple):
    stop_plan_sha256: str
    run_generation: int
    run_id: str
    target_id: str
    installation_id: str


class OrphanStopGrantRequest(HostRuntimeGrantRequest):
    """The stop of a serving container named by its own labels, nothing else."""

    @model_validator(mode="after")
    def orphan_stop_binding(self) -> OrphanStopGrantRequest:
        # The serving container only: its target is the run itself, so a job
        # container carrying this run's label is never named.
        if (
            self.action != ContainerRuntimeAction.STOP
            or self.runtime_target_id != self.runtime_run_id
            or self.stop_plan_sha256 is None
            or self.run_generation is None
            or self.runtime_installation_id is None
        ):
            raise ValueError("orphan stop binding is invalid")
        return self

    def binding(self) -> OrphanStopBinding:
        """The validated identity, typed without the optional forms."""

        assert self.stop_plan_sha256 is not None
        assert self.run_generation is not None
        assert self.runtime_run_id is not None
        assert self.runtime_target_id is not None
        assert self.runtime_installation_id is not None
        return OrphanStopBinding(
            self.stop_plan_sha256,
            self.run_generation,
            self.runtime_run_id,
            self.runtime_target_id,
            self.runtime_installation_id,
        )


class PackageActivationGrantRequest(StrictJSONModel):
    receipt: PackageActivationReceipt
    runtime_identity: AgentRuntimeIdentity


class AgentUpgradeGrantRequest(_AgentGrantRequest):
    package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    package_signature: str = Field(pattern=r"^[0-9a-f]{128}$")


class HostHelperGrantResponse(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    grant: SignedHostHelperGrant
