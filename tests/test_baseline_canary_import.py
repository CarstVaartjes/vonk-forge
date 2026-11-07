"""The canary import program reads the library its Controller was accepted with.

The upgrade-carry lane runs this program inside the baseline Controller (the
previous promoted release, contracts 2.x) with that release's canary index, and
later inside the candidate (contracts 3.0) with the candidate's. The first run
at release is the first time the baseline path executes, so it is run here on
the exact canary index of the baseline library pinned at 900d294a.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASELINE_INDEX = ROOT / "tests/fixtures/baseline-canary-900d294a/index.json"


def _program():
    pytest.importorskip("vonk_control", reason="requires the control environment")
    specification = importlib.util.spec_from_file_location(
        "spark_canary_catalog_import",
        ROOT / "tests/acceptance/spark_canary_catalog_import.py",
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_a_2x_baseline_canary_index_without_displays_is_read_with_the_installed_derivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vonk_forge_contracts as contracts

    program = _program()
    index = json.loads(BASELINE_INDEX.read_text(encoding="utf-8"))
    assert all("display" not in entry for entry in index["recipes"])

    derived: list[str] = []

    def recipe_display_of(recipe):
        derived.append(recipe.identity.slug)
        return contracts.read_recipe_display(
            {
                "kind": "recipe-display",
                "identity": {
                    "publisher": recipe.identity.publisher,
                    "slug": recipe.identity.slug,
                },
                "title": "Derived",
                "description": "Derived by the installed 2.x contracts.",
                "tags": [],
            }
        )

    def model_display_of(model):
        derived.append(model.identity.slug)
        return contracts.read_model_display(
            {
                "kind": "model-display",
                "identity": {
                    "publisher": model.identity.publisher,
                    "slug": model.identity.slug,
                },
                "family_title": "Derived",
                "model_title": "Derived",
                "description": "Derived by the installed 2.x contracts.",
                "tags": [],
                "modalities": ["text"],
                "capabilities": [],
                "license": {
                    "spdx": "Apache-2.0",
                    "url": "https://example.invalid/license",
                    "attribution": [],
                },
            }
        )

    # What the baseline Controller's own contracts provide and 3.0 removed.
    monkeypatch.setattr(
        contracts, "recipe_display_of", recipe_display_of, raising=False
    )
    monkeypatch.setattr(contracts, "model_display_of", model_display_of, raising=False)
    # The program parses the 2.x documents with the installed contracts; the
    # reader's own work is the display, which is what the release run failed on.
    monkeypatch.setattr(
        program,
        "read_recipe",
        lambda document: SimpleNamespace(
            identity=SimpleNamespace(**document["identity"])
        ),
    )
    monkeypatch.setattr(
        program,
        "read_model",
        lambda document: SimpleNamespace(
            identity=SimpleNamespace(**document["identity"])
        ),
    )
    archives = {
        str(entry["package"]["sha256"]): Path("/archive") for entry in index["recipes"]
    }

    reader = program.FixtureReader(index, archives)

    (item,) = reader.snapshot.items
    assert item.display.title == "Derived"
    assert item.title == "Derived"
    assert len(reader.snapshot.catalog_entities) == len(index["catalog_entities"])
    assert derived


def test_a_3_0_canary_index_uses_the_displays_it_carries(tmp_path: Path) -> None:
    program = _program()
    index = json.loads(BASELINE_INDEX.read_text(encoding="utf-8"))
    for entry in index["recipes"]:
        document = entry["document"]
        entry["display"] = {
            "kind": "recipe-display",
            "identity": document["identity"],
            "title": "Carried",
            "description": "Carried by the index.",
            "tags": [],
        }
    for entry in index["catalog_entities"]:
        identity = entry["document"]["identity"]
        entry["display"] = {
            "kind": "model-display",
            "identity": {"publisher": identity["publisher"], "slug": identity["slug"]},
            "family_title": "Carried",
            "model_title": "Carried",
            "description": "Carried by the index.",
            "tags": [],
            "modalities": ["text"],
            "capabilities": [],
            "license": {
                "spdx": "Apache-2.0",
                "url": "https://example.invalid/license",
                "attribution": [],
            },
        }
    archives = {
        str(entry["package"]["sha256"]): Path("/archive") for entry in index["recipes"]
    }

    reader = program.FixtureReader(index, archives)

    assert reader.snapshot.items[0].title == "Carried"
