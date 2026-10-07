"""Policy-limited LiteLLM configuration derived only from published routes."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from .strict_json import StrictModel

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


PROMETHEUS_EXCLUDE_LABELS = (
    "api_base",
    "client_ip",
    "end_user",
    "model_id",
    "org_alias",
    "org_id",
    "tag",
    "user",
    "user_agent",
    "user_alias",
    "user_email",
)
PROMETHEUS_EXCLUDE_METRICS = (
    "litellm_api_key_budget_remaining_hours_metric",
    "litellm_api_key_max_budget_metric",
    "litellm_api_key_rate_limit_allowed_metric",
    "litellm_api_key_rate_limit_used_metric",
    "litellm_cache_hits_metric",
    "litellm_cache_misses_metric",
    "litellm_cached_tokens_metric",
    "litellm_check_batch_cost_errors_total",
    "litellm_check_batch_cost_jobs_polled",
    "litellm_check_batch_cost_jobs_processed_total",
    "litellm_check_batch_cost_last_run_timestamp",
    "litellm_customer_budget_remaining_hours_metric",
    "litellm_customer_max_budget_metric",
    "litellm_deployment_cooled_down",
    "litellm_deployment_latency_per_output_token",
    "litellm_deployment_rpm_limit",
    "litellm_deployment_tpm_limit",
    "litellm_guardrail_errors_total",
    "litellm_guardrail_latency_seconds",
    "litellm_guardrail_requests_total",
    "litellm_images_generated_metric",
    "litellm_in_memory_daily_spend_update_queue_size",
    "litellm_in_memory_spend_update_queue_size",
    "litellm_input_audio_tokens_metric",
    "litellm_input_cache_creation_tokens_metric",
    "litellm_input_cached_tokens_metric",
    "litellm_managed_batch_created_total",
    "litellm_managed_batch_duration_seconds",
    "litellm_managed_file_created_total",
    "litellm_managed_file_deleted_total",
    "litellm_managed_file_size_bytes",
    "litellm_mcp_tool_call_spend_metric",
    "litellm_mcp_tool_calls_total",
    "litellm_org_budget_remaining_hours_metric",
    "litellm_org_max_budget_metric",
    "litellm_output_audio_tokens_metric",
    "litellm_output_reasoning_tokens_metric",
    "litellm_overhead_latency_metric",
    "litellm_overhead_with_guardrails_latency_metric",
    "litellm_pod_lock_manager_size",
    "litellm_provider_cache_creation_input_tokens_metric",
    "litellm_provider_cache_read_input_tokens_metric",
    "litellm_redis_daily_spend_update_queue_size",
    "litellm_redis_spend_update_queue_size",
    "litellm_remaining_api_key_budget_metric",
    "litellm_remaining_api_key_requests_for_model",
    "litellm_remaining_api_key_tokens_for_model",
    "litellm_remaining_customer_budget_metric",
    "litellm_remaining_org_budget_metric",
    "litellm_remaining_requests_metric",
    "litellm_remaining_team_budget_metric",
    "litellm_remaining_tokens_metric",
    "litellm_remaining_user_budget_metric",
    "litellm_request_queue_time_seconds",
    "litellm_spend_metric",
    "litellm_team_budget_remaining_hours_metric",
    "litellm_team_max_budget_metric",
    "litellm_team_members_metric",
    "litellm_team_rate_limit_allowed_metric",
    "litellm_team_rate_limit_used_metric",
    "litellm_user_budget_remaining_hours_metric",
    "litellm_user_max_budget_metric",
    "litellm_video_duration_seconds_metric",
)


class _ConfigModel(StrictModel):
    """The LiteLLM configuration the Controller renders: our own document."""


class LiteLlmGeneralSettings(_ConfigModel):
    database_url: Literal["os.environ/LITELLM_DATABASE_URL"] = (
        "os.environ/LITELLM_DATABASE_URL"
    )
    disable_admin_ui: bool = False
    master_key: Literal["os.environ/LITELLM_MASTER_KEY"] = (
        "os.environ/LITELLM_MASTER_KEY"
    )
    store_model_in_db: bool = False


class LiteLlmSettings(_ConfigModel):
    drop_params: bool = True
    failure_callback: list[Literal["prometheus"]] = Field(
        default_factory=lambda: ["prometheus"]
    )
    # Bounded inference metrics for Prometheus. The scrape path is a
    # dedicated internal network and Caddy never routes /metrics, so
    # no credential is handed to Prometheus. Identity-bearing or
    # unbounded labels and unused metric families are dropped.
    prometheus_exclude_labels: list[str] = Field(
        default_factory=lambda: list(PROMETHEUS_EXCLUDE_LABELS)
    )
    prometheus_exclude_metrics: list[str] = Field(
        default_factory=lambda: list(PROMETHEUS_EXCLUDE_METRICS)
    )
    require_auth_for_metrics_endpoint: bool = False
    set_verbose: bool = False
    success_callback: list[Literal["prometheus"]] = Field(
        default_factory=lambda: ["prometheus"]
    )


class LiteLlmUpstreamParams(_ConfigModel):
    model: str
    api_base: str
    api_key: Literal["os.environ/LITELLM_UPSTREAM_KEY"] = (
        "os.environ/LITELLM_UPSTREAM_KEY"
    )
    rpm: int = Field(ge=1, le=100_000)
    tpm: int = Field(ge=1, le=100_000_000)


class LiteLlmModelEntry(_ConfigModel):
    model_name: str
    litellm_params: LiteLlmUpstreamParams


class LiteLlmRouterSettings(_ConfigModel):
    enable_pre_call_checks: bool = True
    routing_strategy: Literal["simple-shuffle"] = "simple-shuffle"


class LiteLlmConfig(_ConfigModel):
    """The whole rendered file; LiteLLM reads it, nothing else does."""

    general_settings: LiteLlmGeneralSettings = LiteLlmGeneralSettings()
    litellm_settings: LiteLlmSettings = LiteLlmSettings()
    model_list: list[LiteLlmModelEntry]
    router_settings: LiteLlmRouterSettings = LiteLlmRouterSettings()


def _document(model_list: list[LiteLlmModelEntry]) -> bytes:
    document = LiteLlmConfig(model_list=model_list).model_dump(mode="json")
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
    model_list: list[LiteLlmModelEntry] = []
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
            LiteLlmModelEntry(
                model_name=alias,
                litellm_params=LiteLlmUpstreamParams(
                    model=f"openai/{upstream_model}",
                    api_base=routes.aliases[alias].rstrip("/"),
                    rpm=rpm,
                    tpm=tpm,
                ),
            )
        )
    return _document(model_list)


def render_empty_config() -> bytes:
    """Render the LiteLLM configuration that serves no model."""

    return _document([])
