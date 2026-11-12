"""`vonkctl fleet` prints the alert words the Controller web renders from the same vectors."""

import json
from pathlib import Path

from cluster_profiles.cli_render import render_payload

_FIXTURES = Path(__file__).parents[1] / "fixtures"
_VECTORS = json.loads((_FIXTURES / "fleet-wording-attention.json").read_text())
_ALERTS_YAML = (
    _FIXTURES.parents[1] / "deploy" / "compose" / "prometheus" / "alerts.yaml"
)


def _overview(capsys, **fleet):
    nodes = [
        {
            **node,
            "connection": {"online_state": "online", "last_seen_at": None},
            "inventory": None,
            "telemetry": None,
            "loaded": [],
            "installed": [],
            "warnings": [],
        }
        for node in _VECTORS["nodes"]
    ]
    render_payload({"nodes": nodes, **fleet}, "fleet")
    return capsys.readouterr().out.splitlines()


def _needs_attention(lines):
    start = lines.index("Needs attention") + 1
    return [line.strip() for line in lines[start : lines.index("", start)]]


def test_alerts_print_the_vector_lines(capsys):
    lines = _overview(capsys, attention=_VECTORS["alerts"])
    assert _needs_attention(lines) == _VECTORS["expected"]


def test_unreadable_alerts_print_the_vector_sentence(capsys):
    lines = _overview(capsys, attention=[], attention_unavailable=True)
    assert _needs_attention(lines) == [_VECTORS["unavailable"]]


def test_vector_alerts_carry_the_summaries_of_the_shipped_rules():
    rules = {
        rule["alert"]: rule["annotations"]["summary"]
        for group in json.loads(_ALERTS_YAML.read_text())["groups"]
        for rule in group["rules"]
    }
    shipped = [alert for alert in _VECTORS["alerts"] if alert["name"] in rules]
    assert shipped
    assert all(alert["summary"] == rules[alert["name"]] for alert in shipped)
