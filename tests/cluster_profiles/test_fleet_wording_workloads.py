"""`vonkctl fleet` prints the Serving words the Controller web renders from the same vectors."""

import json
import re
from pathlib import Path

from cluster_profiles.cli_render import render_payload

_VECTORS = json.loads(
    (
        Path(__file__).parents[1] / "fixtures" / "fleet-wording-workloads.json"
    ).read_text()
)


def test_each_spark_row_prints_the_vector_serving_words(capsys):
    nodes = [
        {
            "lifecycle": "ready",
            "connection": {"online_state": "online", "last_seen_at": None},
            "inventory": None,
            "telemetry": None,
            "installed": [],
            "warnings": [],
            **node,
        }
        for node in _VECTORS["nodes"]
    ]
    render_payload({"nodes": nodes}, "fleet")
    lines = capsys.readouterr().out.splitlines()
    header = next(line for line in lines if line.startswith("SPARK"))
    columns = re.split(r"\s{2,}", header)
    rows = lines[lines.index(header) + 1 : lines.index(header) + 1 + len(nodes)]
    for node, row in zip(nodes, rows, strict=True):
        cells = dict(zip(columns, re.split(r"\s{2,}", row), strict=True))
        assert cells["SERVING"] == _VECTORS["expected"][node["id"]]["vonkctl"]


def _notes(capsys, **run):
    node = {
        "id": "spk_1",
        "display_name": "Spark 1",
        "lifecycle": "ready",
        "connection": {"online_state": "online", "last_seen_at": None},
        "inventory": None,
        "telemetry": None,
        "installed": [],
        "warnings": [],
        "loaded": [
            {
                "run_id": "run-a",
                "alias": "job",
                "run_state": "running",
                "group_state": "healthy",
                "route_state": "withdrawn",
                "rank_fresh": False,
                **run,
            }
        ],
    }
    render_payload({"nodes": [node]}, "fleet")
    return capsys.readouterr().out


def test_workload_notes_follow_the_controller_verdict(capsys):
    healthy_job = _notes(capsys, healthy=True)
    assert "route is" not in healthy_job and "stale rank" not in healthy_job
    degraded = {
        "healthy": False,
        "group_state": "degraded",
        "degraded_reason": "rank-stale",
        "route_reason": "x",
    }
    assert "job: rank-stale (x)" in _notes(capsys, **degraded)
