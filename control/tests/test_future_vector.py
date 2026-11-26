"""G4: this Controller's index parser survives the recipes future vector.

The vector is a real signed index as a newer recipes minor could publish it
(``fixtures/future-canary-index.json``, placed by the lock refresh). The checks
live in ``tests/acceptance/future_vector_read.py``, which the Spark lane also
runs inside the installed Controller, so both readers face one checker.
"""

from __future__ import annotations

import json
import runpy
from pathlib import Path
from typing import Any

from vonk_forge_contracts.catalog_reader import (
    CatalogSection,
    SkippedEntry,
    UnrecognisedWordReport,
    read_catalog,
)

REPOSITORY = Path(__file__).resolve().parents[2]
VECTOR = Path(__file__).with_name("fixtures") / "future-canary-index.json"
PROGRAM = runpy.run_path(str(REPOSITORY / "tests/acceptance/future_vector_read.py"))


def _word_fields(value: object, path: tuple[str, ...] = ()) -> set[str]:
    """Dotted paths (list positions left out) holding the vector's unknown word."""

    if isinstance(value, dict):
        return {
            field
            for key, item in value.items()
            for field in _word_fields(item, (*path, key))
        }
    if isinstance(value, list):
        return {field for item in value for field in _word_fields(item, path)}
    return {".".join(path)} if value == PROGRAM["WORD"] else set()


def test_the_parser_keeps_all_but_the_newer_kind_and_every_capability_word() -> None:
    verdict = PROGRAM["check"](VECTOR.read_bytes())

    assert verdict["failures"] == []


def test_the_contract_reader_keeps_every_word_outside_execution_documents() -> None:
    index: dict[str, Any] = json.loads(VECTOR.read_bytes())

    problems = read_catalog(index).problems

    expected = {
        (section, field)
        for section in (CatalogSection.MODELS, CatalogSection.RECIPES)
        for entry in index[section.value]
        if not PROGRAM["_holds"](entry["document"], PROGRAM["KIND"])
        for field in _word_fields({k: v for k, v in entry.items() if k != "document"})
    }
    kept = {
        (problem.section, problem.field)
        for problem in problems
        if isinstance(problem, UnrecognisedWordReport)
    }
    assert expected
    assert kept == expected
    others = [p for p in problems if not isinstance(p, UnrecognisedWordReport)]
    assert [(type(p), p.section) for p in others] == [
        (SkippedEntry, CatalogSection.RECIPES)
    ]


def test_a_vector_without_its_newer_kind_fails_the_check() -> None:
    index: dict[str, Any] = json.loads(VECTOR.read_bytes())
    index["recipes"] = [
        entry
        for entry in index["recipes"]
        if not PROGRAM["_holds"](entry["document"], PROGRAM["KIND"])
    ]

    verdict = PROGRAM["check"](json.dumps(index).encode())

    assert verdict["failures"]
