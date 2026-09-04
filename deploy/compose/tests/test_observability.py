import json
import re
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
        "NodeAgentStale": "vonk_agent_last_seen_age_seconds",
        "NodeAgentCertificateExpiring": "vonk_agent_certificate_expiry_seconds",
        "RepeatedAgentOperationFailures": "vonk_agent_operations",
        "AgentRolloutPaused": "vonk_agent_rollouts",
    }
    for alert_name, metric in expected_metrics.items():
        assert metric in alerts[alert_name]["expr"]


def test_stale_agent_alert_semantics_exclude_inactive_nodes_from_every_branch() -> None:
    document = json.loads((ROOT / "deploy/compose/prometheus/alerts.yaml").read_text())
    alert = next(
        rule
        for group in document["groups"]
        for rule in group["rules"]
        if rule["alert"] == "NodeAgentStale"
    )
    expression = alert["expr"]
    outer = re.fullmatch(
        r'\((?P<candidates>.+)\) and on\(node_id\) '
        r'vonk_agent_state\{state="(?P<outer_state>[^"]+)"\} == 1',
        expression,
    )
    assert outer is not None
    candidates = outer.group("candidates")
    old_age = re.search(
        r"vonk_agent_last_seen_age_seconds > (?P<threshold>[0-9]+)", candidates
    )
    missing = re.search(
        r'vonk_agent_state\{state="(?P<missing_state>[^"]+)"\} '
        r"unless on\(node_id\) vonk_agent_last_seen_age_seconds",
        candidates,
    )
    assert old_age is not None and missing is not None

    states = {
        "active-stale": "active",
        "retired-stale": "retired",
        "active-fresh": "active",
        "active-never": "active",
        "retired-never": "retired",
    }
    last_seen_ages = {
        "active-stale": 500,
        "retired-stale": 500,
        "active-fresh": 30,
    }
    threshold = int(old_age.group("threshold"))
    candidate_nodes = {
        node_id for node_id, age in last_seen_ages.items() if age > threshold
    } | {
        node_id
        for node_id, state in states.items()
        if state == missing.group("missing_state") and node_id not in last_seen_ages
    }
    firing = {
        node_id
        for node_id in candidate_nodes
        if states[node_id] == outer.group("outer_state")
    }

    assert missing.group("missing_state") == "active"
    assert outer.group("outer_state") == "active"
    assert firing == {"active-stale", "active-never"}


def test_every_service_has_bounded_logging() -> None:
    for service in _rendered()["services"].values():
        assert service["logging"]["driver"] == "local"
        assert service["logging"]["options"]["max-size"]
        assert service["logging"]["options"]["max-file"]
