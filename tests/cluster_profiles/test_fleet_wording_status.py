"""`vonkctl fleet` prints the status line the Controller web renders from the same vectors."""

import json
from pathlib import Path

import pytest

from cluster_profiles.cli_render import render_payload

_CASES = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "fleet-wording-status.json").read_text()
)["cases"]


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["name"])
def test_first_printed_line_is_the_vector_status_line(case, capsys):
    payload = {
        "nodes": case["nodes"],
        "attention": case.get("attention", []),
        "attention_unavailable": case.get("attention_unavailable", False),
    }
    render_payload(payload, "fleet")
    assert capsys.readouterr().out.splitlines()[0] == case["expected"]
