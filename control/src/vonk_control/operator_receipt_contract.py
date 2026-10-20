"""Canonical public operator acceptance references and existing-Job decisions."""

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator
from vonk_agent_protocol import OperatorActionName

from .library_contract import UuidId
from .strict_json import StrictModel


class ReceiptCode(StrEnum):
    UNAVAILABLE = "operator.receipt_unavailable"
    MISMATCH = "operator.receipt_mismatch"


class ReceiptPolicy(StrEnum):
    OBSERVATION = "observation"
    SECURITY_EDGE = "security-edge"
    AGENT_CLAIM = "agent-claim"
    AGENT_HEARTBEAT = "agent-heartbeat"
    AGENT_RESULT = "agent-result"
    AGENT_INVENTORY = "agent-inventory"
    AGENT_TELEMETRY = "agent-telemetry"
    AGENT_OBSERVATIONS = "agent-observations"
    AGENT_IMAGE = "agent-image"
    AGENT_OUTPUT = "agent-output"
    AGENT_GRANT = "agent-grant"
    ENROLLMENT_ISSUE = "enrollment-issue"
    ENROLLMENT_ACTIVATE = "enrollment-activate"
    PROFILE_SAVE = "profile-save"
    PROFILE_LOAD = "profile-load"
    PROFILE_CANCEL = "profile-cancel"
    MODEL_REQUEST = "model-request"
    MODEL_CANCEL = "model-cancel"
    RECIPE_REQUEST = "recipe-request"
    RECIPE_CANCEL = "recipe-cancel"
    ARTIFACT_CREATE = "artifact-create"
    ARTIFACT_JOB = "artifact-job"
    ARTIFACT_SUBMIT = "artifact-submit"
    ARTIFACT_CANCEL = "artifact-cancel"
    JOB = "job"
    FLEET_NODE = "fleet-node"
    FLEET_ACTION = "fleet-action"
    ENROLLMENT_REVOKE = "enrollment-revoke"
    KEY = "key"
    INSTALLATION = "installation"


class ReceiptOwner(StrEnum):
    PROFILE = "profile"
    PROFILE_APPLICATION = "profile-application"
    MODEL_CACHE = "model-cache"
    RECIPE = "recipe"
    ARTIFACT_JOB = "artifact-job"
    JOB = "job"
    NODE = "node"
    ENROLLMENT = "enrollment"
    KEY = "key"
    RUN_SWITCH = "run-switch"
    AGENT_ATTEMPT = "agent-attempt"
    AGENT_OPERATION = "agent-operation"
    INVENTORY = "inventory"
    TELEMETRY = "telemetry"
    RUN_OBSERVATION = "run-observation"
    RECIPE_IMAGE = "recipe-image"


class AcceptanceReceipt(StrictModel):
    """Public, secret-free reference; the referenced owner retains the facts."""

    owner: ReceiptOwner
    id: str = Field(min_length=1, max_length=256)
    href: str = Field(pattern=r"^/(?:api|agent)/[^\x00-\x20\x7f]*$", max_length=2048)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    revision: int | None = Field(default=None, ge=1)
    content_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class JobOperatorDecision(StrictModel):
    request_id: UuidId
    actor: str = Field(min_length=1, max_length=200)
    action: Literal[OperatorActionName.RESUME, OperatorActionName.RETIRE]


class JobOperatorDecisions(StrictModel):
    decisions: list[JobOperatorDecision]

    @model_validator(mode="after")
    def unique_request_identities(self) -> Self:
        if len({decision.request_id for decision in self.decisions}) != len(
            self.decisions
        ):
            raise ValueError("job operator request identities must be unique")
        return self


class JobOperatorReceipt(JobOperatorDecision):
    job_id: str
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class SecurityEdge(StrEnum):
    """Closed credential/session exemptions; never replay credential issuance."""

    LOGIN = "login"
    LOGOUT = "logout"
    CLI_TOKEN = "cli-token"

    @property
    def endpoint(self) -> tuple[str, str]:
        name = self.value.replace("-", "_")
        return "vonk_control.auth_api", f"install_auth_routes.<locals>.{name}"
