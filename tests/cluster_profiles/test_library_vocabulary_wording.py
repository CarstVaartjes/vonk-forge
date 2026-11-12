"""`vonkctl recipe detail` prints the words the release publishes.

The Controller web test renders the same fixture and compares its own text.
"""

import json
from argparse import Namespace
from pathlib import Path
from typing import cast

import pytest
from vonk_forge_contracts import catalog_vocabulary

from cluster_profiles.cli_render import _library_item
from cluster_profiles.cli_render.vocabulary import capability_label, use_vocabulary
from cluster_profiles.control_client import ControlNotFound
from cluster_profiles.controller_cli.common import ControllerClient
from cluster_profiles.controller_cli.library import _release_words
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


def test_the_fixture_vocabulary_is_what_the_contract_publishes():
    assert _WORDING["vocabulary"] == catalog_vocabulary().model_dump(mode="json")


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


class _Client:
    def __init__(self, answer: object) -> None:
        self.answer, self.paths = answer, []

    def request(self, method: str, path: str, **_: object) -> object:
        self.paths.append(path)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _fetch(answer: object, **flags: bool) -> list[str]:
    client = _Client(answer)
    _release_words(cast(ControllerClient, client), Namespace(**flags))
    return client.paths


def test_the_command_fetches_the_words_once_for_terminal_output_only():
    use_vocabulary(None)
    assert _fetch(_WORDING["vocabulary"], global_json=False) == [
        "/api/library/vocabulary"
    ]
    assert capability_label("chat") == "Chat"

    assert _fetch(_WORDING["vocabulary"], global_json=True) == []
    assert capability_label("chat") == "chat"


def test_a_controller_without_the_endpoint_leaves_the_names():
    # Older, or the synced release publishes no vocabulary: 404.
    _fetch(ControlNotFound(404, "not found"))
    assert capability_label("chat") == "chat"
