"""`vonkctl fleet` prints the vitals words the Controller web renders from the same vectors."""

import json
import re
from pathlib import Path

import pytest

from cluster_profiles.cli_render import render_payload

_CASES = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "fleet-wording-vitals.json").read_text()
)["cases"]


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["name"])
def test_fleet_row_prints_the_vector_words(case, capsys):
    node = {
        "id": f"spk_{1:032x}",
        "display_name": "Alpha",
        "lifecycle": "ready",
        "connection": {"online_state": "online", "last_seen_at": None},
        "loaded": [],
        "installed": [],
        "warnings": [],
        **case["node"],
    }
    render_payload({"nodes": [node]}, "fleet")
    lines = capsys.readouterr().out.splitlines()
    header = next(line for line in lines if line.startswith("SPARK"))
    row = lines[lines.index(header) + 1]
    cells = dict(
        zip(re.split(r"\s{2,}", header), re.split(r"\s{2,}", row), strict=True)
    )
    expected = case["expected"]
    assert cells["MEMORY USED"] == expected["memory"]
    assert cells["DISK FREE"] == expected["disk"]
    assert cells["GPU"] == expected["gpu"]
