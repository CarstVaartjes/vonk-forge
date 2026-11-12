"""`vonkctl fleet` words, checked against the vectors the Controller web also renders."""

import json
from pathlib import Path

from cluster_profiles.cli_render.fleet import _node_workloads

_FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_each_spark_serves_in_the_words_the_web_shows():
    vectors = json.loads((_FIXTURES / "fleet-wording-workloads.json").read_text())
    names = {node["id"]: node["display_name"] for node in vectors["nodes"]}
    for node in vectors["nodes"]:
        assert _node_workloads(node, names) == vectors["expected"][node["id"]]
