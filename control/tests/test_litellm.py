import json
from dataclasses import replace
from pathlib import Path

import pytest
from vonk_control.litellm import (
    LiteLlmPolicy,
    LiteLlmPolicyError,
    RouteState,
    render_config,
    render_empty_config,
)


def _snapshot():
    return RouteState({"deepseek": "http://node.internal:8000/v1"}, "b" * 64)


def _policy(models=("deepseek",)):
    return LiteLlmPolicy(
        models={
            model: {"requests_per_minute": 30, "tokens_per_minute": 10000}
            for model in models
        }
    )


def test_litellm_cannot_add_unknown_repository_model() -> None:
    with pytest.raises(LiteLlmPolicyError, match="published aliases"):
        render_config(_snapshot(), _policy(("deepseek", "shadow-model")))


def test_rendered_config_contains_secret_references_not_values() -> None:
    rendered = render_config(_snapshot(), _policy())
    decoded = json.loads(rendered)
    assert decoded["general_settings"]["master_key"] == "os.environ/LITELLM_MASTER_KEY"
    assert (
        decoded["model_list"][0]["litellm_params"]["api_key"]
        == "os.environ/LITELLM_UPSTREAM_KEY"
    )
    assert (
        decoded["model_list"][0]["litellm_params"]["api_base"]
        == "http://node.internal:8000/v1"
    )
    assert b"sk-live" not in rendered
    assert decoded["model_list"][0]["model_name"] == "deepseek"


def test_public_model_name_can_target_a_distinct_local_upstream() -> None:
    policy = LiteLlmPolicy(
        models={
            "deepseek": {
                "requests_per_minute": 30,
                "tokens_per_minute": 10_000,
                "upstream_model": "deepseek-v4-flash-dspark",
            }
        }
    )

    config = json.loads(render_config(_snapshot(), policy))

    assert config["model_list"][0]["model_name"] == "deepseek"
    assert (
        config["model_list"][0]["litellm_params"]["model"]
        == "openai/deepseek-v4-flash-dspark"
    )


def test_upstream_model_must_be_a_bounded_local_identifier() -> None:
    policy = LiteLlmPolicy(
        models={
            "deepseek": {
                "requests_per_minute": 30,
                "tokens_per_minute": 10_000,
                "upstream_model": "../remote model",
            }
        }
    )

    with pytest.raises(LiteLlmPolicyError, match="upstream model"):
        render_config(_snapshot(), policy)


def test_rendered_config_enables_ui_without_database_model_authority() -> None:
    for rendered in (render_config(_snapshot(), _policy()), render_empty_config()):
        config = json.loads(rendered)
        assert config["general_settings"]["disable_admin_ui"] is False
        assert config["general_settings"]["store_model_in_db"] is False


def test_empty_route_snapshot_cannot_render_models() -> None:
    with pytest.raises(LiteLlmPolicyError, match="published"):
        render_config(replace(_snapshot(), aliases={}), _policy())
    assert json.loads(render_empty_config())["model_list"] == []


def test_litellm_accepts_only_already_rendered_route_strings() -> None:
    snapshot = replace(_snapshot(), aliases={"deepseek": object()})

    with pytest.raises(LiteLlmPolicyError, match="rendered strings"):
        render_config(snapshot, _policy())


def test_every_rendered_config_enables_bounded_prometheus_metrics() -> None:
    bootstrap = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "deploy/compose/litellm/bootstrap-config.json"
        ).read_text()
    )["litellm_settings"]
    for rendered in (render_config(_snapshot(), _policy()), render_empty_config()):
        settings = json.loads(rendered)["litellm_settings"]
        assert settings == bootstrap
        assert settings["success_callback"] == ["prometheus"]
        assert settings["failure_callback"] == ["prometheus"]
        assert {"end_user", "user", "user_email", "client_ip", "user_agent"} <= set(
            settings["prometheus_exclude_labels"]
        )
