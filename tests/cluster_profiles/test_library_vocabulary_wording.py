"""`vonkctl recipe detail` prints the words the release publishes.

The Controller web test renders the same fixture and compares its own text.
"""

import json
from pathlib import Path

import pytest

from cluster_profiles.cli_render import _library_item
from cluster_profiles.cli_render.vocabulary import use_vocabulary
from cluster_profiles.generated_control.models.catalog_vocabulary import (
    CatalogVocabulary,
)

_FIXTURES = Path(__file__).parents[1] / "fixtures"
_WORDING = json.loads((_FIXTURES / "library-vocabulary-wording.json").read_text())
_VOCABULARY = CatalogVocabulary.from_dict(_WORDING["vocabulary"])
_RECIPE = json.loads((_FIXTURES / "library-list.json").read_text())["recipes"][0]


@pytest.fixture(autouse=True)
def _published_words():
    use_vocabulary(_VOCABULARY)
    yield
    use_vocabulary(None)


def _printed(capsys, **changes) -> list[str]:
    _library_item(_RECIPE | changes, "recipe", detail=True)
    return capsys.readouterr().out.splitlines()


@pytest.mark.parametrize(
    "case", _WORDING["capabilities"], ids=lambda case: case["name"]
)
def test_usage_line_is_the_vector_line(case, capsys):
    assert case["vonkctl"] in _printed(capsys, usage=case["usage"])


@pytest.mark.parametrize("case", _WORDING["statuses"], ids=lambda case: case["status"])
def test_status_meaning_line_is_the_vector_line(case, capsys):
    qualification = _RECIPE["qualification"] | {"status": case["status"]}
    assert case["vonkctl"] in _printed(capsys, qualification=qualification)


def test_without_the_vocabulary_the_names_show_and_no_meaning(capsys):
    use_vocabulary(None)
    lines = _printed(capsys, usage=["chat"])
    assert "Usage: chat" in lines
    assert not any(line.startswith("Meaning:") for line in lines)
