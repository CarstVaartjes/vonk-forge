"""`vonkctl fleet` status line, checked against the vectors the Controller web also renders."""

import json
from pathlib import Path

import pytest

from cluster_profiles.cli_render.fleet import _fleet_status_line

_CASES = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "fleet-wording-status.json").read_text()
)["cases"]


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["name"])
def test_status_line_uses_the_words_the_web_shows(case):
    assert _fleet_status_line({"nodes": case["nodes"]}) == case["expected"]
