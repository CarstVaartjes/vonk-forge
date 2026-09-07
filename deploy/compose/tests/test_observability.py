import json
from pathlib import Path

from deploy.compose.tests.test_networking import _rendered

ROOT = Path(__file__).resolve().parents[3]


def test_grafana_is_only_reachable_via_caddy_and_has_no_anonymous_admin() -> None:
    services = _rendered()["services"]
    grafana = services["grafana"]
    assert "ports" not in grafana
    assert set(grafana["networks"]) == {"application", "ingress"}
    assert grafana["environment"]["GF_AUTH_ANONYMOUS_ENABLED"] == "false"
    assert grafana["environment"]["GF_SECURITY_ADMIN_PASSWORD__FILE"] == "/run/vonk-normalized-secrets/grafana-admin-password"
    caddy = (ROOT / "deploy/compose/Caddyfile").read_text()
    assert "handle /grafana/*" in caddy and "grafana:3000" in caddy


def test_agent_alerts_use_bounded_operational_metrics() -> None:
    document = json.loads((ROOT / "deploy/compose/prometheus/alerts.yaml").read_text())
    alerts = {
        rule["alert"]: rule
        for group in document["groups"]
        for rule in group["rules"]
    }
    expected_metrics = {
        "NodeInventoryStale": "vonk_node_inventory_freshness",
        "NodeInventoryMissing": "vonk_node_inventory_freshness",
        "NodeTelemetryStale": "vonk_node_telemetry_freshness",
        "NodeCertificateInvalid": "vonk_node_certificate_state",
        "NodeAgentStale": "vonk_node_connection_state",
        "NodeAgentCertificateExpiring": "vonk_agent_certificate_expiry_seconds",
        "RepeatedAgentOperationFailures": "vonk_agent_operations",
        "RepeatedControlJobFailure": "vonk_jobs",
    }
    for alert_name, metric in expected_metrics.items():
        assert metric in alerts[alert_name]["expr"]


def test_stale_agent_alert_uses_current_connection_projection() -> None:
    document = json.loads((ROOT / "deploy/compose/prometheus/alerts.yaml").read_text())
    alert = next(
        rule
        for group in document["groups"]
        for rule in group["rules"]
        if rule["alert"] == "NodeAgentStale"
    )
    expression = alert["expr"]
    assert expression == 'vonk_node_connection_state{state="offline"} == 1'
    assert "vonk_agent_last_seen_age_seconds" not in expression


def test_every_service_has_bounded_logging() -> None:
    for service in _rendered()["services"].values():
        assert service["logging"]["driver"] == "local"
        assert service["logging"]["options"]["max-size"]
        assert service["logging"]["options"]["max-file"]
