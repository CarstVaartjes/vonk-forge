import json
from pathlib import Path

from deploy.compose.tests.test_networking import _rendered

ROOT = Path(__file__).resolve().parents[3]


def test_agent_alerts_use_bounded_operational_metrics() -> None:
    document = json.loads((ROOT / "deploy/compose/prometheus/alerts.yaml").read_text())
    alerts = {
        rule["alert"]: rule for group in document["groups"] for rule in group["rules"]
    }
    expected_metrics = {
        "NodeInventoryStale": "vonk_node_inventory_freshness",
        "NodeInventoryMissing": "vonk_node_inventory_freshness",
        "NodeTelemetryStale": "vonk_node_telemetry_freshness",
        "NodeCertificateInvalid": "vonk_node_certificate_state",
        "NodeAgentStale": "vonk_node_connection_state",
        "NodeAgentCertificateExpiring": "vonk_agent_certificate_expiry_seconds",
        "RepeatedAgentOperationFailures": "vonk_recent_agent_operation_failures",
        "AgentOperationStalled": "vonk_stalled_operations",
        "RepeatedControlJobFailure": "vonk_recent_job_failures",
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
    assert expression == (
        'vonk_node_connection_state{state="offline"} == 1 '
        'and on(node_id) vonk_agent_state{state="active"} == 1'
    )
    assert "vonk_agent_last_seen_age_seconds" not in expression


def test_starvation_alert_uses_runnable_age_per_kind() -> None:
    # A running job of another kind used to mask a starved queued one, and an
    # intentional retry backoff could look like starvation.  The alert now keys
    # on the oldest runnable queued age per kind, which distinguishes both.
    document = json.loads((ROOT / "deploy/compose/prometheus/alerts.yaml").read_text())
    alerts = {
        rule["alert"]: rule for group in document["groups"] for rule in group["rules"]
    }
    assert "WorkerLeaseStarvation" not in alerts
    rule = alerts["RunnableControlJobStarved"]
    assert rule["expr"] == "max by (kind) (vonk_runnable_job_age_seconds) > 300"
    assert "running" not in rule["expr"]


def test_every_service_has_bounded_logging() -> None:
    for service in _rendered()["services"].values():
        assert service["logging"]["driver"] == "local"
        assert service["logging"]["options"]["max-size"]
        assert service["logging"]["options"]["max-file"]


def test_no_alert_can_fire_on_the_previous_process_series() -> None:
    # Prometheus writes no staleness marker when it stops, so for one lookback
    # (five minutes) after a restart it still sees the previous Controller's
    # last samples. A rule that pends at least that long cannot fire on them
    # alone; a shorter one must compare the target's own current series, as
    # NasComposeBundleStale does (#1726), or read ``up``, which Prometheus
    # itself writes for every scrape.
    from deploy.compose.tests.test_runtime_asset_reload import _services

    command = _services()["prometheus"]["command"]
    assert not any(flag.startswith("--query.lookback-delta") for flag in command)
    lookback = 5 * 60
    seconds = {"s": 1, "m": 60, "h": 3600}
    document = json.loads((ROOT / "deploy/compose/prometheus/alerts.yaml").read_text())
    short = {
        rule["alert"]
        for group in document["groups"]
        for rule in group["rules"]
        if int(rule.get("for", "0s")[:-1]) * seconds[rule.get("for", "0s")[-1]]
        < lookback
        and "unless on(job, instance)" not in rule["expr"]
        and not rule["expr"].startswith("up{")
    }
    assert short == set()
