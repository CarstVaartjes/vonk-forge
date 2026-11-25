"""The external model entry: what is accepted, what each refusal names, how it renders."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

import pytest
from pydantic import JsonValue

from vonk_control.gateway.external_contract import (
    PROVIDER_ORIGIN_PREFIX,
    ApprovedDestination,
    CredentialBinding,
    ExternalDeploymentSpec,
    ExternalDestination,
    ExternalEntryCode,
    ExternalEntryProblem,
    ExternalModelInput,
)
from vonk_control.gateway.external_validation import validate_external_entry
from vonk_control.gateway.litellm import (
    LiteLlmConfig,
    LiteLlmPolicy,
    RouteState,
    render_config,
    render_external_entry,
)
from vonk_control.gateway.litellm_surface import LiteLlmSurface, load_surface

ROOT = Path(__file__).resolve().parents[2]
SECRET = "sk-live-0123456789abcdef"
PARTNER = "https://api.partner.example"


@cache
def _surface() -> LiteLlmSurface:
    return load_surface(ROOT / "deploy/compose/litellm/surface.json")


def _bindings() -> dict[str, CredentialBinding]:
    return {
        "PARTNER": CredentialBinding(
            name="PARTNER",
            revision=1,
            destinations=[
                ApprovedDestination(origin=PARTNER),
                ApprovedDestination(origin="https://10.1.2.3", private=True),
            ],
        ),
        "VERTEX_SA": CredentialBinding(
            name="VERTEX_SA",
            revision=1,
            destinations=[
                ApprovedDestination(origin="provider:vertex_ai", region="europe-west4")
            ],
        ),
    }


def _openai(**changes: JsonValue) -> ExternalModelInput:
    params: dict[str, JsonValue] = {
        "model": "openai/partner-large",
        "api_base": f"{PARTNER}/v1",
        "api_key": "os.environ/VONK_CREDENTIAL_PARTNER",
    }
    params.update(changes)
    return ExternalModelInput(
        model_name="partner-large",
        litellm_params={key: value for key, value in params.items() if value != ""},
    )


def _vertex() -> ExternalModelInput:
    return ExternalModelInput(
        model_name="gemini",
        litellm_params={
            "model": "vertex_ai/gemini-2.5-pro",
            "vertex_project": "demo",
            "vertex_location": "europe-west4",
            "vertex_credentials": "os.environ/VONK_CREDENTIAL_VERTEX_SA",
        },
    )


def _validate(
    entry: ExternalModelInput,
) -> ExternalDeploymentSpec | list[ExternalEntryProblem]:
    return validate_external_entry(entry, _surface(), _bindings(), taken_aliases=())


@pytest.mark.parametrize("entry", [_openai(), _vertex()], ids=["openai", "vertex"])
def test_a_valid_entry_is_accepted_and_its_canonical_json_round_trips(
    entry: ExternalModelInput,
) -> None:
    spec = _validate(entry)
    assert isinstance(spec, ExternalDeploymentSpec)
    canonical = spec.model_dump_json(exclude_none=True)
    again = ExternalDeploymentSpec.model_validate_json(canonical)
    assert again == spec
    assert again.model_dump_json(exclude_none=True) == canonical


def test_the_destination_names_the_origin_the_region_and_the_binding() -> None:
    openai = _validate(_openai())
    vertex = _validate(_vertex())
    assert isinstance(openai, ExternalDeploymentSpec)
    assert isinstance(vertex, ExternalDeploymentSpec)
    assert openai.destination.origins == [PARTNER]
    assert openai.credential_bindings == ["PARTNER"]
    assert vertex.destination.origins == ["provider:vertex_ai"]
    assert vertex.destination.region == "europe-west4"


REFUSALS = {
    "extra_headers": (
        _openai(extra_headers={"x-a": "b"}),
        "litellm_params.extra_headers",
        ExternalEntryCode.UNDECLARED_FIELD,
    ),
    "hook beats file path": (
        _openai(auto_router_config_path="/etc/x"),
        "litellm_params.auto_router_config_path",
        ExternalEntryCode.EXECUTABLE_HOOK,
    ),
    "identity token file": (
        _openai(anthropic_identity_token_file="/run/token"),
        "litellm_params.anthropic_identity_token_file",
        ExternalEntryCode.FILE_ACCESS,
    ),
    "master key reference": (
        _openai(api_key="os.environ/LITELLM_MASTER_KEY"),
        "litellm_params.api_key",
        ExternalEntryCode.UNAPPROVED_BINDING,
    ),
    "oidc reference": (
        _openai(api_key="oidc/file//x"),
        "litellm_params.api_key",
        ExternalEntryCode.UNAPPROVED_BINDING,
    ),
    "literal secret": (
        _openai(api_key=SECRET),
        "litellm_params.api_key",
        ExternalEntryCode.LITERAL_SECRET,
    ),
    "tls verification": (
        _openai(ssl_verify=False),
        "litellm_params.ssl_verify",
        ExternalEntryCode.INSECURE_DESTINATION,
    ),
    "wildcard alias": (
        ExternalModelInput(model_name="*", litellm_params=_openai().litellm_params),
        "model_name",
        ExternalEntryCode.WILDCARD,
    ),
    "reserved alias": (
        ExternalModelInput(
            model_name="all-proxy-models", litellm_params=_openai().litellm_params
        ),
        "model_name",
        ExternalEntryCode.RESERVED_NAME,
    ),
    "access groups": (
        ExternalModelInput(
            model_name="partner-large",
            litellm_params=_openai().litellm_params,
            model_info={"access_groups": ["all"]},
        ),
        "model_info.access_groups",
        ExternalEntryCode.CONTROLLER_OWNED_FIELD,
    ),
    "retries are the Controller's": (
        _openai(num_retries=5),
        "litellm_params.num_retries",
        ExternalEntryCode.CONTROLLER_OWNED_FIELD,
    ),
    "plain http": (
        _openai(api_base="http://api.partner.example/v1"),
        "litellm_params.api_base",
        ExternalEntryCode.INSECURE_DESTINATION,
    ),
    "origin the binding does not approve": (
        _openai(api_base="https://elsewhere.example/v1"),
        "litellm_params.api_base",
        ExternalEntryCode.DESTINATION_NOT_APPROVED,
    ),
    "sts endpoint outside the binding": (
        _openai(aws_sts_endpoint="https://sts.elsewhere.example"),
        "litellm_params.aws_sts_endpoint",
        ExternalEntryCode.DESTINATION_NOT_APPROVED,
    ),
    "private address": (
        _openai(api_base="https://10.4.5.6/v1"),
        "litellm_params.api_base",
        ExternalEntryCode.PRIVATE_ADDRESS,
    ),
    "malformed port": (
        _openai(api_base="https://api.partner.example:abc/v1"),
        "litellm_params.api_base",
        ExternalEntryCode.TYPE_MISMATCH,
    ),
    "malformed ipv6 host": (
        _openai(api_base="https://[::1/v1"),
        "litellm_params.api_base",
        ExternalEntryCode.TYPE_MISMATCH,
    ),
    "host the origin cannot carry": (
        _openai(api_base="https://api_gw.partner.example/v1"),
        "litellm_params.api_base",
        ExternalEntryCode.TYPE_MISMATCH,
    ),
    "endpoint without a scheme": (
        _openai(api_base="api.partner.example/v1"),
        "litellm_params.api_base",
        ExternalEntryCode.TYPE_MISMATCH,
    ),
    "endpoint with leading space": (
        _openai(api_base=" https://api.partner.example/v1"),
        "litellm_params.api_base",
        ExternalEntryCode.TYPE_MISMATCH,
    ),
    "unknown provider": (
        _openai(model="nowhere/x"),
        "litellm_params.model",
        ExternalEntryCode.UNKNOWN_PROVIDER,
    ),
    "wrong type": (
        _openai(rpm="many"),
        "litellm_params.rpm",
        ExternalEntryCode.TYPE_MISMATCH,
    ),
    "unknown binding": (
        _openai(api_key="os.environ/VONK_CREDENTIAL_MISSING"),
        "litellm_params.api_key",
        ExternalEntryCode.UNAPPROVED_BINDING,
    ),
}


@pytest.mark.parametrize(
    ("entry", "path", "code"), REFUSALS.values(), ids=list(REFUSALS)
)
def test_each_refusal_names_its_field_and_rule_and_never_echoes_a_secret(
    entry: ExternalModelInput, path: str, code: ExternalEntryCode
) -> None:
    problems = _validate(entry)
    assert isinstance(problems, list)
    assert (path, code) in {(item.path, item.code) for item in problems}
    assert SECRET not in json.dumps([item.model_dump(mode="json") for item in problems])


def test_an_approved_private_origin_is_accepted_only_with_its_binding() -> None:
    approved = _validate(_openai(api_base="https://10.1.2.3/v1"))
    assert isinstance(approved, ExternalDeploymentSpec)
    unbound = _validate(_openai(api_base="https://10.1.2.3/v1", api_key=""))
    assert isinstance(unbound, list)
    assert [item.code for item in unbound] == [ExternalEntryCode.PRIVATE_ADDRESS]


def test_an_alias_another_owner_serves_is_refused() -> None:
    problems = validate_external_entry(
        _openai(), _surface(), _bindings(), taken_aliases={"partner-large"}
    )
    assert isinstance(problems, list)
    assert [(item.path, item.code) for item in problems] == [
        ("model_name", ExternalEntryCode.ALIAS_TAKEN)
    ]


def test_every_pinned_provider_has_a_nameable_destination() -> None:
    # Break caught: a provider the origin contract cannot spell would make an
    # accepted entry fail to build its destination.
    for provider in _surface().providers:
        ExternalDestination(origins=[PROVIDER_ORIGIN_PREFIX + provider])


def test_the_surface_names_the_pinned_image() -> None:
    lock = json.loads((ROOT / "deploy/compose/images.lock.json").read_text())
    pinned = lock["build_bases"]["litellm"]
    assert pinned.endswith(f"@{_surface().image_digest}")


def test_an_external_entry_renders_beside_local_routes_and_round_trips() -> None:
    spec = _validate(_openai())
    assert isinstance(spec, ExternalDeploymentSpec)
    entry = render_external_entry(spec, "0d4f5b0e-0000-4000-8000-000000000001", 3)
    document = json.loads(
        render_config(RouteState({}, "0" * 64), LiteLlmPolicy({}), [entry])
    )
    served = document["model_list"][0]
    assert served["litellm_params"]["num_retries"] == 0
    assert served["model_info"]["id"].endswith(":3")
    assert served["model_info"]["vonk_origin"] == "external"
    parsed = LiteLlmConfig.model_validate(document)
    assert parsed.model_list == [entry]
