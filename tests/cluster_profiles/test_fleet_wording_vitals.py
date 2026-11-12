"""`vonkctl fleet` words, checked against the vectors the Controller web also renders."""

import json
from pathlib import Path

import pytest

from cluster_profiles.cli_render.fleet import _disk, _gpu, _memory

_VITALS = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "fleet-wording-vitals.json").read_text()
)["cases"]


@pytest.mark.parametrize("case", _VITALS, ids=lambda case: case["name"])
def test_fleet_vitals_use_the_words_the_web_shows(case):
    node, expected = case["node"], case["expected"]
    inventory = node["inventory"] or {}
    assert _memory(node) == (expected["memory"] or "unavailable")
    assert _disk(
        inventory.get("disk_free_bytes"), inventory.get("disk_total_bytes")
    ) == (expected["disk"] or "unavailable")
    assert _gpu(node) == (expected["gpu"] or "unavailable")
