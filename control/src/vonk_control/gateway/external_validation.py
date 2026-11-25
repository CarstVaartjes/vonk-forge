"""The one admission rule for an external model entry: a pure function, no I/O.

``validate_external_entry`` returns the accepted spec or every problem at once.
A problem names a field path and a rule code and never repeats a submitted
value, so a literal secret that was pasted by mistake is refused without being
echoed.  Where one key breaks two rules the first applies: executable hook, file
access, proxy-wide setting, client-side credentials, implicit fallback,
Controller-owned field, TLS, then the declared-field and type checks.  A refusal that names a credential
reference (``os.environ/`` or ``oidc/``) does not depend on the field it sits in.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Collection, Iterator, Mapping, Sequence
from typing import Final
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from pydantic import JsonValue, TypeAdapter
from vonk_forge_contracts import EndpointAlias

from vonk_control.gateway.external_contract import (
    BINDING_NAME_PATTERN,
    CREDENTIAL_ENV_PREFIX,
    PROVIDER_ORIGIN_PREFIX,
    CredentialBinding,
    ExternalDeploymentSpec,
    ExternalDestination,
    ExternalEntryCode,
    ExternalEntryProblem,
    ExternalModelInput,
    LiteLlmFields,
)
from vonk_control.gateway.litellm import LiteLlmModelInfo
from vonk_control.gateway.litellm_surface import LiteLlmSurface

_PARAMS: Final = "litellm_params"
_INFO: Final = "model_info"
_ENV_REFERENCE: Final = "os.environ/"
_OIDC_REFERENCE: Final = "oidc/"
_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_BINDING_NAME = re.compile(BINDING_NAME_PATTERN)
_ALIAS = TypeAdapter(EndpointAlias)

#: LiteLLM's own reserved model names: a key's ``models`` list gives them meaning.
RESERVED_ALIASES: Final = frozenset(
    {"all-proxy-models", "all-team-models", "no-default-models"}
)

_HOOK_PREFIXES: Final = (
    "auto_router_",
    "complexity_router_",
    "adaptive_router_",
    "quality_router_",
)
_HOOKS: Final = frozenset({"mock_response", "use_in_pass_through", "use_litellm_proxy"})
_FILE_SUFFIXES: Final = ("_file", "_path")
_PROXY_WIDE_PREFIXES: Final = ("milvus_", "valkey_", "s3_", "default_api_key_")
_PROXY_WIDE: Final = frozenset(
    {
        "vector_store_id",
        "gcs_bucket_name",
        "bucket_name",
        "litellm_credential_name",
        "tags",
        "tag_regex",
        "model_info",
    }
)
_CLIENT_SIDE: Final = frozenset({"configurable_clientside_auth_params"})
#: The Controller owns retries and fallbacks: the router never retries across deployments.
_CONTROLLER_PARAMS: Final = frozenset({"num_retries", "max_retries"})
_INSECURE_TLS: Final = frozenset({"ssl_verify"})
#: Fields that carry a credential: only a reference to an approved binding is accepted.
_SECRET_PARAMS: Final = frozenset(
    {
        "api_key",
        "azure_ad_token",
        "client_secret",
        "azure_password",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
        "aws_web_identity_token",
        "vertex_credentials",
        "anthropic_identity_token",
        "anthropic_issuer_signing_key_ref",
        "anthropic_keycloak_client_secret_ref",
    }
)
_REGION_PARAMS: Final = (
    "aws_region_name",
    "vertex_location",
    "region_name",
    "watsonx_region_name",
)
#: ``model_info`` fields the Controller sets itself.
_CONTROLLER_INFO: Final = frozenset(
    {
        "id",
        "access_groups",
        "team_id",
        "team_public_model_name",
        "db_model",
        "disable_background_health_check",
        "vonk_origin",
    }
)
_CONTROLLER_INFO_PREFIXES: Final = ("health_check_",)
_VONK_FACTS: Final = {
    name: TypeAdapter(field.annotation)
    for name, field in LiteLlmModelInfo.model_fields.items()
    if name.startswith("vonk_") and name not in _CONTROLLER_INFO
}


def validate_external_entry(
    entry: ExternalModelInput,
    surface: LiteLlmSurface,
    bindings: Mapping[str, CredentialBinding],
    taken_aliases: Collection[str],
) -> ExternalDeploymentSpec | list[ExternalEntryProblem]:
    """Accept one entry or return every reason it is refused.

    ``taken_aliases`` holds the aliases another owner serves already (the
    namespace's answer, read by the caller).  An entry that references no binding
    carries no secret, so it may name any ``https`` host.
    """

    problems: list[ExternalEntryProblem] = []
    params = _without_nulls(entry.litellm_params)
    info = _without_nulls(entry.model_info or {})
    _check_alias(entry.model_name, taken_aliases, problems)
    _check_model(params.get("model"), surface, problems)
    for key in sorted(params):
        _check_param(key, params[key], surface, problems)
    for key in sorted(info):
        _check_info(key, info[key], surface, problems)
    found = _origins(params, problems)
    references = _binding_references(params, info, problems)
    region = next(
        (value for key in _REGION_PARAMS if isinstance(value := params.get(key), str)),
        None,
    )
    provider = str(params.get("model", "")).partition("/")[0]
    origins = sorted(found) or [PROVIDER_ORIGIN_PREFIX + provider]
    approving = [bindings.get(name) for name in sorted(references)]
    for name, binding in zip(sorted(references), approving, strict=True):
        _check_binding(binding, references[name], origins, region, found, problems)
    for origin, path in sorted(found.items()):
        if _private_origin(origin) and not _private_approved(origin, approving):
            _problem(path, ExternalEntryCode.PRIVATE_ADDRESS, problems, observed=origin)
    if problems:
        return problems
    return ExternalDeploymentSpec(
        alias=entry.model_name,
        litellm_params=params,
        model_info=info,
        destination=ExternalDestination(origins=origins, region=region),
        credential_bindings=sorted(references),
    )


def _problem(
    path: str,
    code: ExternalEntryCode,
    problems: list[ExternalEntryProblem],
    *,
    limit: str | None = None,
    observed: str | None = None,
) -> None:
    problems.append(
        ExternalEntryProblem(path=path, code=code, limit=limit, observed=observed)
    )


def _without_nulls(values: LiteLlmFields) -> LiteLlmFields:
    return {key: value for key, value in values.items() if value is not None}


def _check_alias(
    alias: str, taken: Collection[str], problems: list[ExternalEntryProblem]
) -> None:
    path = "model_name"
    if "*" in alias:
        _problem(path, ExternalEntryCode.WILDCARD, problems)
    elif alias in RESERVED_ALIASES:
        _problem(path, ExternalEntryCode.RESERVED_NAME, problems)
    elif not _is_alias(alias):
        _problem(path, ExternalEntryCode.TYPE_MISMATCH, problems)
    elif alias in taken:
        _problem(path, ExternalEntryCode.ALIAS_TAKEN, problems)


def _is_alias(alias: str) -> bool:
    try:
        _ALIAS.validate_python(alias, strict=True)
    except ValueError:
        return False
    return True


def _check_model(
    model: JsonValue | None,
    surface: LiteLlmSurface,
    problems: list[ExternalEntryProblem],
) -> None:
    path = f"{_PARAMS}.model"
    if not isinstance(model, str) or "/" not in model:
        _problem(path, ExternalEntryCode.TYPE_MISMATCH, problems)
    elif "*" in model:
        _problem(path, ExternalEntryCode.WILDCARD, problems)
    elif model.partition("/")[0] not in surface.providers:
        _problem(path, ExternalEntryCode.UNKNOWN_PROVIDER, problems)


def _denied_param(key: str) -> ExternalEntryCode | None:
    if key.startswith(_HOOK_PREFIXES) or key in _HOOKS:
        return ExternalEntryCode.EXECUTABLE_HOOK
    if key.endswith(_FILE_SUFFIXES):
        return ExternalEntryCode.FILE_ACCESS
    if key.startswith(_PROXY_WIDE_PREFIXES) or key in _PROXY_WIDE:
        return ExternalEntryCode.PROXY_WIDE_SETTING
    if key in _CLIENT_SIDE:
        return ExternalEntryCode.CLIENT_SIDE_CREDENTIALS
    if "fallback" in key:
        return ExternalEntryCode.IMPLICIT_FALLBACK
    if key in _CONTROLLER_PARAMS:
        return ExternalEntryCode.CONTROLLER_OWNED_FIELD
    if key in _INSECURE_TLS:
        return ExternalEntryCode.INSECURE_DESTINATION
    return None


def _check_param(
    key: str,
    value: JsonValue,
    surface: LiteLlmSurface,
    problems: list[ExternalEntryProblem],
) -> None:
    path = f"{_PARAMS}.{key}"
    denied = _denied_param(key)
    if denied is not None:
        _problem(path, denied, problems)
        return
    schema = surface.litellm_params.get(key)
    if schema is None:
        _problem(path, ExternalEntryCode.UNDECLARED_FIELD, problems)
        return
    if key in _SECRET_PARAMS:
        _check_secret(path, key, value, problems)
        return
    if not _valid(schema, value):
        _problem(path, ExternalEntryCode.TYPE_MISMATCH, problems)


def _valid(schema: JsonValue, value: JsonValue) -> bool:
    """Whether ``value`` satisfies a surface schema; a schema that is no document accepts nothing."""

    if not isinstance(schema, (dict, bool)):
        return False
    return Draft202012Validator(schema).is_valid(value)


def _check_secret(
    path: str, key: str, value: JsonValue, problems: list[ExternalEntryProblem]
) -> None:
    """A secret-typed field holds a reference to a binding, never a value."""

    if isinstance(value, str) and value.startswith((_ENV_REFERENCE, _OIDC_REFERENCE)):
        return  # judged with every other reference in ``_binding_references``
    if key == "vertex_credentials" and isinstance(value, str):
        literal = value.lstrip().startswith("{")
        code = (
            ExternalEntryCode.LITERAL_SECRET
            if literal
            else ExternalEntryCode.FILE_ACCESS
        )
        _problem(path, code, problems)
        return
    _problem(path, ExternalEntryCode.LITERAL_SECRET, problems)


def _check_info(
    key: str,
    value: JsonValue,
    surface: LiteLlmSurface,
    problems: list[ExternalEntryProblem],
) -> None:
    path = f"{_INFO}.{key}"
    if key in _CONTROLLER_INFO or key.startswith(_CONTROLLER_INFO_PREFIXES):
        _problem(path, ExternalEntryCode.CONTROLLER_OWNED_FIELD, problems)
    elif (fact := _VONK_FACTS.get(key)) is not None:
        try:
            fact.validate_python(value)
        except ValueError:
            _problem(path, ExternalEntryCode.TYPE_MISMATCH, problems)
    elif (schema := surface.model_info.get(key)) is None:
        _problem(path, ExternalEntryCode.UNDECLARED_FIELD, problems)
    elif not _valid(schema, value):
        _problem(path, ExternalEntryCode.TYPE_MISMATCH, problems)


def _strings(value: JsonValue, path: str) -> Iterator[tuple[str, str]]:
    """Every string value in a document, with the path of the field that holds it."""

    if isinstance(value, str):
        yield path, value
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{path}[{index}]")
    elif isinstance(value, dict):
        for key in sorted(value):
            yield from _strings(value[key], f"{path}.{key}")


def _origins(
    params: LiteLlmFields, problems: list[ExternalEntryProblem]
) -> dict[str, str]:
    """The origin of every absolute ``https`` URL in the entry, with its first path."""

    origins: dict[str, str] = {}
    for key in sorted(params):
        for path, text in _strings(params[key], f"{_PARAMS}.{key}"):
            if _URL.match(text) is None:
                continue
            parts = urlsplit(text)
            host = parts.hostname
            if parts.scheme != "https" or not host:
                _problem(path, ExternalEntryCode.INSECURE_DESTINATION, problems)
            elif parts.username is not None or parts.password is not None:
                _problem(path, ExternalEntryCode.LITERAL_SECRET, problems)
            else:
                port = f":{parts.port}" if parts.port else ""
                bracket = f"[{host}]" if ":" in host else host
                origins.setdefault(f"https://{bracket}{port}", path)
    return origins


def _private_origin(origin: str) -> bool:
    return _private_host(urlsplit(origin).hostname or "")


def _private_approved(
    origin: str, approving: Collection[CredentialBinding | None]
) -> bool:
    """A private origin is allowed only when every referenced binding approves it as such."""

    return bool(approving) and all(
        binding is not None
        and any(item.origin == origin and item.private for item in binding.destinations)
        for binding in approving
    )


def _private_host(host: str) -> bool:
    """Whether the host is ``localhost`` or an IP literal outside the public ranges.

    Loopback, link-local and metadata, private, CGNAT, multicast and reserved
    addresses are all "not global"; admission does no DNS lookup.
    """

    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return False


def _binding_references(
    params: LiteLlmFields,
    info: LiteLlmFields,
    problems: list[ExternalEntryProblem],
) -> dict[str, str]:
    """The binding names the entry references, each with the path of its first use.

    A reference is accepted only as the whole value of a secret-typed field;
    any other reference is refused.
    """

    names: dict[str, str] = {}
    for section, document in ((_PARAMS, params), (_INFO, info)):
        for key in sorted(document):
            for path, text in _strings(document[key], f"{section}.{key}"):
                if not text.startswith((_ENV_REFERENCE, _OIDC_REFERENCE)):
                    continue
                name = _binding_name(text.removeprefix(_ENV_REFERENCE))
                whole = path == f"{section}.{key}"
                if (
                    text.startswith(_OIDC_REFERENCE)
                    or name is None
                    or section != _PARAMS
                    or key not in _SECRET_PARAMS
                    or not whole
                ):
                    _problem(path, ExternalEntryCode.UNAPPROVED_BINDING, problems)
                else:
                    names.setdefault(name, path)
    return names


def _binding_name(variable: str) -> str | None:
    name = variable.removeprefix(CREDENTIAL_ENV_PREFIX)
    if variable.startswith(CREDENTIAL_ENV_PREFIX) and _BINDING_NAME.fullmatch(name):
        return name
    return None


def _check_binding(
    binding: CredentialBinding | None,
    reference_path: str,
    origins: Sequence[str],
    region: str | None,
    paths: Mapping[str, str],
    problems: list[ExternalEntryProblem],
) -> None:
    """Every origin of the entry, in its region, must be approved by the binding.

    The refusal names the field that holds the origin (``litellm_params.model``
    for a provider-native destination).
    """

    if binding is None:
        _problem(reference_path, ExternalEntryCode.UNAPPROVED_BINDING, problems)
        return
    approved = {item.origin: item for item in binding.destinations}
    for origin in origins:
        item = approved.get(origin)
        if item is None or (item.region is not None and item.region != region):
            _problem(
                paths.get(origin, f"{_PARAMS}.model"),
                ExternalEntryCode.DESTINATION_NOT_APPROVED,
                problems,
                observed=origin,
            )
