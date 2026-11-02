"""Each carry phase reads documents with the contracts of the library it targets.

The lane runs a 2.x baseline and a 3.0 candidate in one process. Python caches
the first import of ``vonk_forge_contracts``, so the baseline library's
contracts used to read the candidate's documents ("metadata: Field required").
The libraries here ship two small contracts packages that disagree the way 2.x
and 3.0 do about the Recipe's ``metadata``; the baseline document is the frozen
canary recipe of the library pinned at 900d294a.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.acceptance.test_spark_lifecycle import (
    LifecycleError,
    _canonical_recipe_matches,
    _library_contracts,
)

ROOT = Path(__file__).resolve().parents[1]
BASELINE = json.loads(
    (ROOT / "tests/fixtures/baseline-canary-900d294a/index.json").read_text()
)["recipes"][0]["document"]
CANDIDATE = {key: value for key, value in BASELINE.items() if key != "metadata"}

CONTRACTS = """
class RecipeDefinition:
    def __init__(self, document):
        self.document = document

    @classmethod
    def model_validate(cls, document):
        if {required} not in document or {forbidden} in document:
            raise ValueError("document does not satisfy these contracts")
        return cls(dict(document))

    def __eq__(self, other):
        return self.document == other.document

VERSION = "{version}"
"""


def _library(tmp_path: Path, version: str, *, metadata: bool) -> Path:
    package = tmp_path / version / "contracts/src/vonk_forge_contracts"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        CONTRACTS.format(
            version=version,
            required='"metadata"' if metadata else '"kind"',
            forbidden='"nothing"' if metadata else '"metadata"',
        )
    )
    return tmp_path / version


def test_each_library_reads_its_own_documents_in_one_process(tmp_path: Path) -> None:
    baseline = _library(tmp_path, "2.8.1", metadata=True)
    candidate = _library(tmp_path, "3.0.0", metadata=False)

    assert _canonical_recipe_matches(BASELINE, BASELINE, baseline)
    # Reading the baseline first must not decide how the candidate is read.
    assert _canonical_recipe_matches(CANDIDATE, CANDIDATE, candidate)
    assert not _canonical_recipe_matches(CANDIDATE, CANDIDATE, baseline)
    assert not _canonical_recipe_matches(BASELINE, BASELINE, candidate)


def test_the_process_contracts_are_restored_after_each_block(tmp_path: Path) -> None:
    import vonk_forge_contracts

    before = sys.modules["vonk_forge_contracts"]
    path = list(sys.path)
    with _library_contracts(_library(tmp_path, "2.8.1", metadata=True)) as inner:
        assert inner.VERSION == "2.8.1"
        assert sys.modules["vonk_forge_contracts"] is inner
    assert sys.modules["vonk_forge_contracts"] is before is vonk_forge_contracts
    assert sys.path == path


def test_a_library_without_contract_source_is_refused(tmp_path: Path) -> None:
    with pytest.raises(LifecycleError), _library_contracts(tmp_path):
        pass
