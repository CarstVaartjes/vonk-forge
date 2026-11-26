"""The contracts of an external model: what an operator submits and what is accepted.

An external model is one LiteLLM ``model_list`` entry that points at a provider
outside the fleet.  LiteLLM owns the types of its own fields, so the operator's
entry carries them as received (``LiteLlmValue``); everything the Controller adds
or checks is typed here.  ``external_validation.validate_external_entry`` is the
one function that turns an :class:`ExternalModelInput` into an
:class:`ExternalDeploymentSpec` or into every problem at once.  No secret value
appears in any of these contracts: a credential is a binding name, and the value
stays in the NAS custody directory.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BeforeValidator, ConfigDict, Field, JsonValue, StringConstraints
from vonk_forge_contracts import EndpointAlias

from vonk_control.stored.json_values import ExternalPassthrough
from vonk_control.strict_json import StrictModel

#: The environment variable the supervisor gives LiteLLM for one credential
#: binding is ``VONK_CREDENTIAL_<BINDING>``; the entry names it as ``os.environ/...``.
CREDENTIAL_ENV_PREFIX = "VONK_CREDENTIAL_"

BINDING_NAME_PATTERN = r"[A-Z][A-Z0-9_]{0,62}"
BindingName = Annotated[str, StringConstraints(pattern=f"^{BINDING_NAME_PATTERN}$")]

#: The origin of a provider-native destination that names no URL.
PROVIDER_ORIGIN_PREFIX = "provider:"

_DEFAULT_HTTPS_PORT = re.compile(r"^(https://(?:\[[0-9a-f:.]+\]|[a-z0-9.\-]+)):443$")


def normalise_origin(value: object) -> object:
    """The one spelling of an origin: the default ``https`` port is left off.

    ``https://host`` and ``https://host:443`` name one origin.  Entries and
    bindings both read their origins through this function, so a binding that
    approves one spelling approves the other.
    """

    if isinstance(value, str) and (match := _DEFAULT_HTTPS_PORT.fullmatch(value)):
        return match.group(1)
    return value


#: ``https://host[:port]`` for a network destination, ``provider:<name>`` for a
#: provider-native one that names no URL.
Origin = Annotated[
    str,
    BeforeValidator(normalise_origin),
    StringConstraints(
        pattern=r"^(https://[a-z0-9.\-\[\]:]{1,253}|"
        + PROVIDER_ORIGIN_PREFIX
        + r"[a-z0-9_\-]{1,64})$"
    ),
]

LiteLlmValue = Annotated[
    JsonValue,
    ExternalPassthrough(
        "A LiteLLM litellm_params or model_info value: LiteLLM, not the Controller, "
        "owns the type of each field. The pinned surface (surface.json) is checked "
        "against it before the value is stored or rendered."
    ),
]


LiteLlmFields = dict[str, LiteLlmValue]


class RouteOrigin(StrEnum):
    """Where the upstream of a gateway entry runs: the rendered entry says which."""

    LOCAL = "local"
    EXTERNAL = "external"


class DefaultBaseProvider(StrEnum):
    """LiteLLM providers whose adapter falls back to a plain-``http`` or local ``api_base``."""

    A2A = "a2a"
    A2A_AGENT = "a2a_agent"
    CUSTOM = "custom"
    CUSTOM_OPENAI = "custom_openai"
    DOCKER_MODEL_RUNNER = "docker_model_runner"
    HOSTED_VLLM = "hosted_vllm"
    LANGFLOW = "langflow"
    LANGGRAPH = "langgraph"
    LEMONADE = "lemonade"
    LLAMAFILE = "llamafile"
    LM_STUDIO = "lm_studio"
    OLLAMA = "ollama"
    OLLAMA_CHAT = "ollama_chat"
    OPENAI_LIKE = "openai_like"
    RAGFLOW = "ragflow"
    VLLM = "vllm"
    XINFERENCE = "xinference"


class ExternalEntryCode(StrEnum):
    """Why an external entry is refused: one code per rule, with the field it names."""

    UNDECLARED_FIELD = "undeclared_field"
    TYPE_MISMATCH = "type_mismatch"
    UNKNOWN_PROVIDER = "unknown_provider"
    WILDCARD = "wildcard"
    EXECUTABLE_HOOK = "executable_hook"
    FILE_ACCESS = "file_access"
    PROXY_WIDE_SETTING = "proxy_wide_setting"
    IMPLICIT_FALLBACK = "implicit_fallback"
    CLIENT_SIDE_CREDENTIALS = "client_side_credentials"
    LITERAL_SECRET = "literal_secret"
    UNAPPROVED_BINDING = "unapproved_binding"
    DESTINATION_NOT_APPROVED = "destination_not_approved"
    INSECURE_DESTINATION = "insecure_destination"
    PRIVATE_ADDRESS = "private_address"
    RESERVED_NAME = "reserved_name"
    CONTROLLER_OWNED_FIELD = "controller_owned_field"
    ALIAS_TAKEN = "alias_taken"


class ExternalEntryProblem(StrictModel):
    """One refusal: the field path, the rule, and a bound where one applies.

    ``observed`` never carries a value taken from a secret-typed field.
    """

    path: str
    code: ExternalEntryCode
    limit: str | None = Field(default=None, exclude_if=lambda value: value is None)
    observed: str | None = Field(default=None, exclude_if=lambda value: value is None)


class ExternalModelInput(StrictModel):
    """The operator's input: exactly one LiteLLM ``model_list`` entry.

    ``model_name`` is plain text here so a wildcard or a malformed alias is
    reported with the other problems instead of failing before validation.
    """

    model_name: str
    litellm_params: LiteLlmFields
    model_info: LiteLlmFields | None = None


class ExternalDestination(StrictModel):
    """Where the data of an external model goes, for the review and for a change check."""

    origins: list[Origin] = Field(min_length=1)
    region: str | None = Field(default=None, exclude_if=lambda value: value is None)


class ExternalDeploymentSpec(StrictModel):
    """The accepted, normalized and non-secret form of an external model."""

    alias: EndpointAlias
    litellm_params: LiteLlmFields
    model_info: LiteLlmFields = Field(default_factory=dict)
    destination: ExternalDestination
    credential_bindings: list[BindingName] = Field(default_factory=list)


class ApprovedDestination(StrictModel):
    """One place a credential binding may send its secret.

    ``region`` ``None`` approves the origin in any region.  ``private`` approves
    this one exact origin although its host is a private IP literal.
    """

    origin: Origin
    region: str | None = Field(default=None, exclude_if=lambda value: value is None)
    private: bool = False


class CredentialBinding(StrictModel):
    """The ``gateway_credential_bindings`` row: a name and where its secret may go."""

    name: BindingName
    revision: int = Field(ge=1)
    destinations: list[ApprovedDestination]


class ExternalRouteParams(StrictModel):
    """The ``litellm_params`` of a rendered external entry: the spec's plus ``num_retries``."""

    model_config = ConfigDict(extra="allow")

    model: str
    # The Controller owns retries; the router never retries across deployments.
    num_retries: Literal[0] = 0


class ExternalRouteModelInfo(StrictModel):
    """The ``model_info`` of a rendered external entry.

    ``id`` is the stable deployment identity with its revision; ``vonk_origin``
    marks the entry as external.  The operator's descriptive fields ride along as
    LiteLLM's own.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    vonk_origin: Literal[RouteOrigin.EXTERNAL] = RouteOrigin.EXTERNAL


class ExternalRouteEntry(StrictModel):
    """One external ``model_list`` entry, exactly as LiteLLM reads it."""

    model_name: EndpointAlias
    litellm_params: ExternalRouteParams
    model_info: ExternalRouteModelInfo
