"""`vonkctl fleet` alert words, checked against the vectors the Controller web also renders."""

import json
from pathlib import Path

from cluster_profiles.cli_render import render_payload
from cluster_profiles.cli_render.fleet import ALERTS_UNAVAILABLE, _alert_notes

_VECTORS = json.loads(
    (
        Path(__file__).parents[1] / "fixtures" / "fleet-wording-attention.json"
    ).read_text()
)


def test_alerts_read_as_plain_words_the_web_shows():
    payload = {"nodes": _VECTORS["nodes"], "attention": _VECTORS["alerts"]}
    assert _alert_notes(payload) == _VECTORS["expected"]
    assert _alert_notes({"nodes": [], "attention_unavailable": True}) == [
        _VECTORS["unavailable"]
    ]
    assert ALERTS_UNAVAILABLE == _VECTORS["unavailable"]


def test_fleet_overview_lists_alerts_under_needs_attention(capsys):
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
    render_payload({"nodes": nodes, "attention": _VECTORS["alerts"]}, "fleet")
    output = capsys.readouterr().out
    attention = output[output.index("Needs attention") :]
    assert all(f"  {line}" in attention for line in _VECTORS["expected"])
