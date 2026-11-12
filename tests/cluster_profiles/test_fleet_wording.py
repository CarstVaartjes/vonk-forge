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
    for render, key in ((_memory, "memory"), (_disk, "disk"), (_gpu, "gpu")):
        assert render(node) == (expected[key] or "unavailable")
