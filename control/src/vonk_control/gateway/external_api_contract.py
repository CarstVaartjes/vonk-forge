"""The wire documents of the external model and credential binding routes.

An operator reviews a change (preview), then accepts exactly that review: the accept
carries the review's ``effect_digest`` and is refused when anything it read changed.
No document carries a secret.  A credential is a binding name; the value stays in
NAS custody.
"""

from __future__ import annotations

from pydantic import AwareDatetime, Field
from vonk_forge_contracts import EndpointAlias

from vonk_agent_protocol.identifiers import Digest
from vonk_agent_protocol.state_machines import (
    ExternalBlockReason,
    ExternalPublicationState,
)
from vonk_agent_protocol.wire_model import WireEnum
from vonk_control.gateway.external_contract import (
    ApprovedDestination,
    BindingName,
    ExternalDeploymentSpec,
    ExternalDestination,
    ExternalEntryProblem,
    ExternalModelInput,
)
from vonk_control.identifiers import UuidId
from vonk_control.strict_json import StrictModel


class ExternalModelAction(WireEnum):
    """What an operator asks of one external model."""

    PUT = "put"
    DISABLE = "disable"
    ENABLE = "enable"
    REMOVE = "remove"


class ExternalEffect(WireEnum):
    """What the gateway does when a change is accepted: the review names each one."""

    #: Every config change restarts LiteLLM; its in-flight streams are cut after a grace.
    GATEWAY_RESTART = "gateway-restart"
    #: Every key without a model list can call the alias once it is published.
    OPEN_KEY_ACCESS = "open-key-access"


class ExternalModelView(StrictModel):
    """One external model, as the gateway holds it (no secret, no entry body)."""

    alias: EndpointAlias
    #: ``None`` when the stored entry is damaged; the state says why it does not serve.
    destination: ExternalDestination | None = None
    credential_bindings: list[BindingName]
    desired_revision: int = Field(ge=1)
    applied_revision: int | None = Field(default=None, ge=1)
    enabled: bool
    state: ExternalPublicationState
    reason: ExternalBlockReason | None = None
    next_check_at: AwareDatetime | None = None
    #: The gateway serves the desired revision: a request for the alias reaches it.
    published: bool


class ExternalModelList(StrictModel):
    models: list[ExternalModelView]


class ExternalValidateRequest(StrictModel):
    entry: ExternalModelInput


class ExternalValidation(StrictModel):
    """Every problem of an entry at once; no traffic is sent."""

    problems: list[ExternalEntryProblem]
    destination: ExternalDestination | None = None


class ExternalModelPreviewRequest(StrictModel):
    action: ExternalModelAction
    entry: ExternalModelInput | None = None
    #: The revision the operator read; ``None`` for a model that does not exist yet.
    expected_revision: int | None = Field(default=None, ge=1)


class ExternalModelReview(StrictModel):
    """What accepting the change would do, and the digest that names exactly this."""

    alias: EndpointAlias
    action: ExternalModelAction
    problems: list[ExternalEntryProblem]
    destination: ExternalDestination | None = None
    #: The destination the model serves now, when the change moves it.
    changed_from: ExternalDestination | None = None
    credential_bindings: list[BindingName] = Field(default_factory=list)
    effects: list[ExternalEffect]
    grace_seconds: int = Field(ge=0)
    expected_revision: int | None = Field(default=None, ge=1)
    effect_digest: Digest


class ExternalModelAcceptRequest(StrictModel):
    request_id: UuidId
    action: ExternalModelAction
    entry: ExternalModelInput | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    effect_digest: Digest


class ExternalModelAccepted(StrictModel):
    alias: EndpointAlias
    request_id: UuidId
    action: ExternalModelAction
    revision: int = Field(ge=1)
    state: ExternalPublicationState


class ExternalBindingAction(WireEnum):
    PUT = "put"
    REMOVE = "remove"


class ExternalBindingDependent(StrictModel):
    """A deployment that references a binding and what the change does to it."""

    alias: EndpointAlias
    state: ExternalPublicationState
    #: The change leaves the deployment's destination unapproved: it is withdrawn.
    withdrawn: bool


class ExternalBindingView(StrictModel):
    name: BindingName
    revision: int = Field(ge=1)
    destinations: list[ApprovedDestination]
    #: The supervisor loaded a secret for the name; ``None`` while it has not said.
    credential_loaded: bool | None = None


class ExternalBindingList(StrictModel):
    bindings: list[ExternalBindingView]


class ExternalBindingPreviewRequest(StrictModel):
    action: ExternalBindingAction
    destinations: list[ApprovedDestination] | None = None
    expected_revision: int | None = Field(default=None, ge=1)


class ExternalBindingReview(StrictModel):
    name: BindingName
    action: ExternalBindingAction
    added: list[ApprovedDestination]
    removed: list[ApprovedDestination]
    dependents: list[ExternalBindingDependent]
    credential_loaded: bool | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    effect_digest: Digest


class ExternalBindingAcceptRequest(StrictModel):
    request_id: UuidId
    action: ExternalBindingAction
    destinations: list[ApprovedDestination] | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    effect_digest: Digest


class ExternalBindingAccepted(StrictModel):
    name: BindingName
    request_id: UuidId
    action: ExternalBindingAction
    revision: int | None = Field(default=None, ge=1)


class ModelReviewed(StrictModel):
    """What a model review read: its digest names exactly this."""

    alias: EndpointAlias
    action: ExternalModelAction
    spec: ExternalDeploymentSpec | None
    expected_revision: int | None
    binding_revisions: dict[str, int]


class BindingReviewed(StrictModel):
    """What a binding review read: its digest names exactly this."""

    name: str
    action: ExternalBindingAction
    destinations: list[ApprovedDestination]
    expected_revision: int | None
    dependents: list[ExternalBindingDependent]
