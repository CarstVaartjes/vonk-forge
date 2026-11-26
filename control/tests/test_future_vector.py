"""G4: this Controller's index parser survives the recipes future vector.

The vector is a real signed index as a newer recipes minor could publish it
(``fixtures/future-canary-index.json``; FB's lock refresh, in progress, will
place it from each recipes release). The checks live in
``tests/acceptance/future_vector_read.py``, which the Spark lane also runs
inside the installed Controller, so both readers face one checker.
"""

from __future__ import annotations

import json
import runpy
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[2]
VECTOR = Path(__file__).with_name("fixtures") / "future-canary-index.json"
PROGRAM = runpy.run_path(str(REPOSITORY / "tests/acceptance/future_vector_read.py"))


def test_the_parser_keeps_all_but_the_newer_kind_and_every_capability_word() -> None:
    verdict = PROGRAM["check"](VECTOR.read_bytes())

    assert verdict["failures"] == []


def test_a_vector_without_its_newer_kind_fails_the_check() -> None:
    index: dict[str, Any] = json.loads(VECTOR.read_bytes())
    index["recipes"] = [
        entry
        for entry in index["recipes"]
        if not PROGRAM["_holds"](entry["document"], PROGRAM["KIND"])
    ]

    verdict = PROGRAM["check"](json.dumps(index).encode())

    assert verdict["failures"]
