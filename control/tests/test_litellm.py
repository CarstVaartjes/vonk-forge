import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from vonk_control.litellm import (
    LiteLlmDeployment,
    LiteLlmPolicy,
    LiteLlmPolicyError,
    LiteLlmPublisher,
)
from vonk_control.routes import RouteState


def _snapshot():
    return RouteState(1, "published", "a" * 40, "agent", "deepseek", ("spk_00000000000000000000000000000001",), {"deepseek": "http://node.internal:8000/v1"}, datetime(2026, 8, 3, tzinfo=UTC).isoformat(), None, "b" * 64)


def _policy(models=("deepseek",)):
    return LiteLlmPolicy(models={model: {"requests_per_minute": 30, "tokens_per_minute": 10000} for model in models})


def test_litellm_cannot_add_unknown_repository_model(tmp_path: Path) -> None:
    publisher = LiteLlmPublisher(tmp_path, validate=lambda _: True, apply=lambda _: None)
    with pytest.raises(LiteLlmPolicyError, match="published aliases"):
        publisher.render(_snapshot(), _policy(("deepseek", "shadow-model")))


def test_rendered_config_contains_secret_references_not_values(tmp_path: Path) -> None:
    publisher = LiteLlmPublisher(tmp_path, validate=lambda _: True, apply=lambda _: None)
    rendered = publisher.render(_snapshot(), _policy())
    decoded = json.loads(rendered)
    assert decoded["general_settings"]["master_key"] == "os.environ/LITELLM_MASTER_KEY"
    assert decoded["model_list"][0]["litellm_params"]["api_key"] == "os.environ/LITELLM_UPSTREAM_KEY"
    assert decoded["model_list"][0]["litellm_params"]["api_base"] == "http://node.internal:8000/v1"
    assert b"sk-live" not in rendered
    assert decoded["model_list"][0]["model_name"] == "deepseek"


def test_public_model_name_can_target_a_distinct_local_upstream(
    tmp_path: Path,
) -> None:
    policy = LiteLlmPolicy(
        models={
            "deepseek": {
                "requests_per_minute": 30,
                "tokens_per_minute": 10_000,
                "upstream_model": "deepseek-v4-flash-dspark",
            }
        }
    )

    config = json.loads(
        LiteLlmPublisher(
            tmp_path, validate=lambda _: True, apply=lambda _: None
        ).render(_snapshot(), policy)
    )

    assert config["model_list"][0]["model_name"] == "deepseek"
    assert (
        config["model_list"][0]["litellm_params"]["model"]
        == "openai/deepseek-v4-flash-dspark"
    )


def test_upstream_model_must_be_a_bounded_local_identifier(tmp_path: Path) -> None:
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
        LiteLlmPublisher(
            tmp_path, validate=lambda _: True, apply=lambda _: None
        ).render(_snapshot(), policy)


def test_rendered_config_enables_ui_without_database_model_authority(
    tmp_path: Path,
) -> None:
    publisher = LiteLlmPublisher(tmp_path, validate=lambda _: True, apply=lambda _: None)

    config = json.loads(publisher.render(_snapshot(), _policy()))

    assert config["general_settings"]["disable_admin_ui"] is False
    assert config["general_settings"]["store_model_in_db"] is False


def test_apply_is_atomic_and_retains_previous_generation(tmp_path: Path) -> None:
    applied = []
    publisher = LiteLlmPublisher(tmp_path, validate=lambda content: b"deepseek" in content, apply=lambda content: applied.append(content))
    generation = publisher.publish(_snapshot(), _policy())
    assert publisher.active() == generation
    rejecting = LiteLlmPublisher(tmp_path, validate=lambda _: False, apply=lambda _: None)
    with pytest.raises(LiteLlmPolicyError, match="validation"):
        rejecting.publish(_snapshot(), _policy())
    assert rejecting.active() == generation


def test_maintenance_snapshot_cannot_render_models(tmp_path: Path) -> None:
    snapshot = _snapshot()
    maintenance = RouteState(snapshot.generation, "maintenance", None, None, None, snapshot.node_ids, {}, None, "switch", snapshot.digest)
    with pytest.raises(LiteLlmPolicyError, match="published"):
        LiteLlmPublisher(tmp_path, validate=lambda _: True, apply=lambda _: None).render(maintenance, _policy())


def test_litellm_accepts_only_already_rendered_route_strings(tmp_path: Path) -> None:
    snapshot = replace(_snapshot(), aliases={"deepseek": object()})

    with pytest.raises(LiteLlmPolicyError, match="rendered strings"):
        LiteLlmPublisher(
            tmp_path,
            validate=lambda _: True,
            apply=lambda _: None,
        ).render(snapshot, _policy())


def test_hermes_group_is_local_ordered_and_retry_bounded(tmp_path: Path) -> None:
    publisher = LiteLlmPublisher(
        tmp_path,
        validate=lambda _: True,
        apply=lambda _: None,
    )
    policy = LiteLlmPolicy(
        models={"deepseek": {"requests_per_minute": 30, "tokens_per_minute": 10_000}},
        deployments=(
            LiteLlmDeployment(
                "hermes-agent",
                "recipe-run-canary",
                "http://10.0.0.42:8888/v1",
                1,
                20,
                8_000,
            ),
            LiteLlmDeployment(
                "hermes-agent",
                "recipe-run-primary",
                "http://10.0.0.43:8888/v1",
                2,
                30,
                10_000,
            ),
        ),
    )

    config = json.loads(publisher.render(_snapshot(), policy))
    hermes = [
        model for model in config["model_list"]
        if model["model_name"] == "hermes-agent"
    ]
    assert [model["litellm_params"]["model"] for model in hermes] == [
        "openai/recipe-run-canary",
        "openai/recipe-run-primary",
    ]
    assert [model["litellm_params"]["order"] for model in hermes] == [1, 2]
    assert [model["litellm_params"]["api_base"] for model in hermes] == [
        "http://10.0.0.42:8888/v1",
        "http://10.0.0.43:8888/v1",
    ]
    assert all(
        model["litellm_params"]["api_key"] == "os.environ/LITELLM_UPSTREAM_KEY"
        for model in hermes
    )
    assert config["router_settings"] == {
        "allowed_fails": 0,
        "enable_pre_call_checks": True,
        "num_retries": 1,
        "retry_policy": {
            "AuthenticationErrorRetries": 0,
            "BadRequestErrorRetries": 0,
            "ContentPolicyViolationErrorRetries": 0,
            "RateLimitErrorRetries": 1,
            "TimeoutErrorRetries": 1,
        },
        "routing_strategy": "simple-shuffle",
    }
    assert b"openai.com" not in publisher.render(_snapshot(), policy)


@pytest.mark.parametrize(
    "deployment",
    (
        LiteLlmDeployment("hermes-agent", "cloud", "https://api.openai.com/v1", 1, 1, 1),
        LiteLlmDeployment("hermes-agent", "host", "http://node.local:8888/v1", 1, 1, 1),
        LiteLlmDeployment("other", "local", "http://10.0.0.42:8888/v1", 1, 1, 1),
    ),
)
def test_hermes_deployments_reject_cloud_hosts_and_wrong_alias(
    tmp_path: Path,
    deployment: LiteLlmDeployment,
) -> None:
    with pytest.raises(LiteLlmPolicyError, match="Hermes"):
        LiteLlmPublisher(
            tmp_path,
            validate=lambda _: True,
            apply=lambda _: None,
        ).render(_snapshot(), LiteLlmPolicy(models={}, deployments=(deployment,)))
