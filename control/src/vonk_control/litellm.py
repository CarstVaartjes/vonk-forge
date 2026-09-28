"""Policy-limited LiteLLM configuration derived only from published routes."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

_UPSTREAM_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,119}\Z")


class LiteLlmPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class RouteState:
    """One route candidate: rendered alias API bases and its identity digest."""

    aliases: Mapping[str, str]
    digest: str


@dataclass(frozen=True)
class LiteLlmPolicy:
    models: Mapping[str, Mapping[str, int | str]]


@dataclass(frozen=True)
class LiteLlmGeneration:
    generation: int
    route_digest: str
    config_sha256: str
    path: str


def _document(model_list: list[dict[str, object]]) -> bytes:
    document = {
        "general_settings": {
            "database_url": "os.environ/LITELLM_DATABASE_URL",
            "disable_admin_ui": False,
            "master_key": "os.environ/LITELLM_MASTER_KEY",
            "store_model_in_db": False,
        },
        "litellm_settings": {
            "drop_params": True,
            "failure_callback": [],
            "set_verbose": False,
            "success_callback": [],
        },
        "model_list": model_list,
        "router_settings": {
            "enable_pre_call_checks": True,
            "routing_strategy": "simple-shuffle",
        },
    }
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()


def render_config(routes: RouteState, policy: LiteLlmPolicy) -> bytes:
    """Render the LiteLLM configuration serving exactly the published aliases."""

    if not routes.aliases:
        raise LiteLlmPolicyError("LiteLLM models require a published route snapshot")
    if any(
        not isinstance(value, str) or not value for value in routes.aliases.values()
    ):
        raise LiteLlmPolicyError("LiteLLM routes must be already-rendered strings")
    models = dict(policy.models)
    if set(models) - set(routes.aliases):
        raise LiteLlmPolicyError(
            "LiteLLM policy contains models outside published aliases"
        )
    if not models:
        raise LiteLlmPolicyError("LiteLLM policy must publish at least one model")
    model_list: list[dict[str, object]] = []
    for alias in sorted(models):
        quota = dict(models[alias])
        required = {"requests_per_minute", "tokens_per_minute"}
        fields = set(quota)
        if fields not in (required, required | {"upstream_model"}):
            raise LiteLlmPolicyError("LiteLLM model quota fields are invalid")
        upstream_model = quota.pop("upstream_model", alias)
        if (
            not isinstance(upstream_model, str)
            or _UPSTREAM_MODEL.fullmatch(upstream_model) is None
        ):
            raise LiteLlmPolicyError("LiteLLM upstream model is invalid")
        rpm, tpm = quota["requests_per_minute"], quota["tokens_per_minute"]
        if (
            not isinstance(rpm, int)
            or not isinstance(tpm, int)
            or not 1 <= rpm <= 100_000
            or not 1 <= tpm <= 100_000_000
        ):
            raise LiteLlmPolicyError("LiteLLM model quotas are outside allowed bounds")
        model_list.append(
            {
                "model_name": alias,
                "litellm_params": {
                    "model": f"openai/{upstream_model}",
                    "api_base": routes.aliases[alias].rstrip("/"),
                    "api_key": "os.environ/LITELLM_UPSTREAM_KEY",
                    "rpm": rpm,
                    "tpm": tpm,
                },
            }
        )
    return _document(model_list)


def render_empty_config() -> bytes:
    """Render the LiteLLM configuration that serves no model."""

    return _document([])
