"""`vonkctl fleet` prints the throttle-reason words the Controller web renders from the same vectors."""

import json
from pathlib import Path

import pytest

from cluster_profiles.cli_render.throttle import throttle_words

_VECTORS = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "fleet-wording-throttle.json").read_text()
)


@pytest.mark.parametrize(
    "case", _VECTORS["cases"], ids=lambda case: str(case["reasons"])
)
def test_each_reason_mask_reads_the_vector_words(case):
    assert throttle_words(case["reasons"]) == case["words"]
