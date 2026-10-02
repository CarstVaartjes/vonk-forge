import json
import re
from pathlib import Path

import yaml

from deploy.compose.tests.test_agent_ingress import (
    _adapted_caddy,
    _environment,
    _rendered,
    _routes_with_handlers,
    _server_on_port,
)

ROOT = Path(__file__).resolve().parents[3]
COMPOSE = ROOT / "deploy/compose"
DASHBOARD = COMPOSE / "grafana/dashboards/inference.json"

# Labels that identify a person, a client or an unbounded dimension.
FORBIDDEN_LABELS = {
    "end_user",
    "user",
    "user_email",
    "user_alias",
    "client_ip",
    "user_agent",
    "tag",
    "api_base",
}


def _prometheus_command() -> list[str]:
    return _rendered()["services"]["prometheus"]["command"]


def test_prometheus_keeps_a_year_with_a_size_cap_and_the_same_tsdb() -> None:
    command = _prometheus_command()
    assert "--storage.tsdb.retention.time=365d" in command
    size = next(c for c in command if c.startswith("--storage.tsdb.retention.size="))
    gigabytes = int(re.fullmatch(r"--storage.tsdb.retention.size=(\d+)GB", size)[1])
    assert 30 <= gigabytes <= 50
    # Retention flags alone keep existing data: same path and volume.
    assert "--storage.tsdb.path=/prometheus" in command
    volumes = _rendered()["services"]["prometheus"]["volumes"]
    assert any(
        v["source"] == "prometheus-data" and v["target"] == "/prometheus"
        for v in volumes
    )


def test_prometheus_scrapes_litellm_over_a_dedicated_internal_network() -> None:
    config = yaml.safe_load((COMPOSE / "prometheus/prometheus.yml").read_text())
    jobs = {job["job_name"]: job for job in config["scrape_configs"]}
    assert "vonk-control" in jobs
    litellm = jobs["litellm"]
    assert litellm["static_configs"] == [{"targets": ["litellm:4000"]}]
    assert litellm["metrics_path"] in {"/metrics", "/metrics/"}
    assert litellm["scheme"] == "http"

    rendered = _rendered()
    assert rendered["networks"]["litellm-metrics"]["internal"] is True
    assert {
        name
        for name, service in rendered["services"].items()
        if "litellm-metrics" in service.get("networks", {})
    } == {"litellm", "prometheus"}


def test_litellm_bootstrap_config_enables_bounded_prometheus_metrics() -> None:
    settings = json.loads((COMPOSE / "litellm/bootstrap-config.json").read_text())[
        "litellm_settings"
    ]
    assert settings["success_callback"] == ["prometheus"]
    assert settings["failure_callback"] == ["prometheus"]
    assert FORBIDDEN_LABELS <= set(settings["prometheus_exclude_labels"])
    # The scrape path is network-isolated, so no credential is shared.
    assert settings["require_auth_for_metrics_endpoint"] is False


def test_litellm_metrics_are_not_routed_through_caddy() -> None:
    adapted = _adapted_caddy(_environment())
    for port in (8080, 8081, 8087):
        routes = _routes_with_handlers(_server_on_port(adapted, port)["routes"])
        for route in routes:
            patterns = [
                path
                for matcher in route.get("match", [])
                for path in matcher.get("path", [])
            ]
            if any("metrics" in pattern for pattern in patterns):
                encoded = json.dumps(route["handle"])
                assert "reverse_proxy" not in encoded, (port, patterns)
                assert '"status_code": 404' in encoded, (port, patterns)
    browser = _routes_with_handlers(_server_on_port(adapted, 8080)["routes"])
    patterns = [
        [p for m in r.get("match", []) for p in m.get("path", [])] for r in browser
    ]
    guard = next(i for i, p in enumerate(patterns) if "/litellm/metrics" in p)
    proxy = next(i for i, p in enumerate(patterns) if "/litellm/*" in p)
    assert guard < proxy
    # The other listeners only allow-list /v1/* and key routes.
    for port in (8081, 8087):
        text = json.dumps(_server_on_port(adapted, port))
        assert "/metrics" not in text.replace("static_response", "")


def test_inference_dashboard_is_valid_and_uses_produced_metrics() -> None:
    dashboard = json.loads(DASHBOARD.read_text())
    assert dashboard["uid"] == "vonk-inference"
    assert dashboard["title"] == "Vonk Forge Inference"
    assert dashboard["tags"] == ["vonk-forge"]
    assert dashboard["editable"] is False
    existing = {
        json.loads(path.read_text())["uid"]
        for path in (COMPOSE / "grafana/dashboards").glob("*.json")
        if path != DASHBOARD
    }
    assert dashboard["uid"] not in existing
    ids = [panel["id"] for panel in dashboard["panels"]]
    assert len(ids) == len(set(ids))
    expressions = [t["expr"] for panel in dashboard["panels"] for t in panel["targets"]]
    joined = "\n".join(expressions)
    for metric in (
        "litellm_requests_metric_total",
        "litellm_input_tokens_metric_total",
        "litellm_output_tokens_metric_total",
        "litellm_request_total_latency_metric_bucket",
        "litellm_llm_api_time_to_first_token_metric_bucket",
        "litellm_deployment_failure_responses_total",
        "litellm_total_tokens_metric_total",
    ):
        assert metric in joined
    assert "api_key_alias" in joined
    # Every queried LiteLLM series must survive the metric exclusions.
    settings = json.loads((COMPOSE / "litellm/bootstrap-config.json").read_text())[
        "litellm_settings"
    ]
    for excluded in settings["prometheus_exclude_metrics"]:
        assert excluded not in joined
    for label in FORBIDDEN_LABELS:
        assert not re.search(rf"\b{label}\b", joined)
