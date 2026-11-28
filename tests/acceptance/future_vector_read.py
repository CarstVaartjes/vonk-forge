"""Read the recipes future vector with the installed Controller's index parser.

The future vector (G4) is a real signed index as a newer recipes minor could
publish it: an unknown word at every closed-word site outside an execution
document, an unknown field on every closed object, and one extra Recipe whose
serving kind no release executes. This program hands its bytes to the
Controller's own release parser (``RecipePackageClient._parse_index``, the
path every signed index takes) and checks what that parser made of it:

- nothing is a sync problem, and exactly the one Recipe with the unknown
  serving kind is withheld; every other Recipe and every Model is kept;
- every capability word is kept: in each Model display, in each Recipe's
  ``served_capabilities`` and in the vocabulary;
- no block is lost, except an API block whose own mode, reasoning placement or
  remaining-context word is unknown: none has a cautious neutral (a guessed
  placement sends a wrong request), so the parser withholds that block and
  marks it withheld (``withheld_blocks``), which keeps the recorded value.

It stops at the snapshot. The vector's kept entries are rebound to new digests
while their package archives are not, so no package is loaded from it.

The Spark lane runs this program inside the installed Controller, the stable
one on the stable channel, so it names only entry points every release ships
(``tests/test_baseline_program_surface.py``). ``control/tests`` runs the same
checks in process against this tree's parser.

Input (stdin): the vector's bytes. Output (stdout, JSON): ``failures`` (empty
when the parser survived the vector) and what was kept; the exit status is 1
when anything failed.
"""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from vonk_control.recipe_packages.client import RecipePackageClient

# The producer's markers (vonk-forge-recipes ``recipe_tooling.future_vector``).
WORD = "zz-future-word"
FIELD = "zz_future_field"
KIND = "zz.future-kind"
# API words with no cautious neutral: the path inside a Recipe's base block and
# the unknown word the vector puts there.
NO_NEUTRAL_API_SITES = {
    ("mode",): WORD,
    ("reasoning", "via"): WORD,
    ("default_output_tokens", "remaining"): WORD,
}
# The optional entry blocks a Recipe carries into the library snapshot.
RECIPE_BLOCKS = (
    "display",
    "qualification",
    "measured_memory",
    "prebuilt_image",
    "served_capabilities",
)


def _holds(value: object, marker: str) -> bool:
    if isinstance(value, dict):
        return marker in value or any(_holds(item, marker) for item in value.values())
    if isinstance(value, list):
        return any(_holds(item, marker) for item in value)
    return value == marker


def _name(entry: dict[str, Any]) -> str:
    identity = entry["document"]["identity"]
    return f"{identity['publisher']}/{identity['slug']}"


def _at(value: object, path: tuple[str, ...]) -> object:
    for step in path:
        if not isinstance(value, dict):
            return None
        value = value.get(step)
    return value


def _no_neutral(entry: dict[str, Any]) -> bool:
    """Whether the entry's base API block holds the word where it has no neutral."""
    api = entry.get("api")
    return isinstance(api, dict) and any(
        _at(api.get("api"), site) == word for site, word in NO_NEUTRAL_API_SITES.items()
    )


def _words(values: Iterable[object] | None) -> set[str]:
    return {str(value) for value in values} if values is not None else set()


def check(raw: bytes) -> dict[str, Any]:
    """What the installed parser made of the vector, and every way it fell short."""

    index = json.loads(raw)
    recipes = {_name(entry): entry for entry in index["recipes"]}
    models = {_name(entry): entry for entry in index["catalog_entities"]}
    future = sorted(
        name for name, entry in recipes.items() if _holds(entry["document"], KIND)
    )
    failures: list[str] = []
    # A vector that stopped carrying its markers would pass vacuously.
    if len(future) != 1:
        failures.append(f"the vector carries {len(future)} Recipes of kind {KIND}")
    if not _holds(index, FIELD):
        failures.append(f"the vector carries no {FIELD}")
    if not any(
        _holds(entry.get("served_capabilities"), WORD) for entry in recipes.values()
    ):
        failures.append(f"no served_capabilities carries {WORD}")
    if not any(_holds(entry.get("display"), WORD) for entry in models.values()):
        failures.append(f"no Model display carries {WORD}")
    for site, word in NO_NEUTRAL_API_SITES.items():
        if not any(
            _at(entry["api"].get("api"), site) == word
            for entry in recipes.values()
            if isinstance(entry.get("api"), dict)
        ):
            failures.append(f"no API block carries {word} at {'.'.join(site)}")
    with tempfile.TemporaryDirectory() as cache_root:
        client = RecipePackageClient(cache_root=Path(cache_root))
        try:
            snapshot, _packages = client._parse_index(
                raw, publication_commit=str(index["source_commit"])
            )
        finally:
            client.close()
    if snapshot.problems:
        failures.append(f"sync problems: {[p.detail for p in snapshot.problems]}")
    if len(snapshot.withheld) != 1:
        failures.append(f"{len(snapshot.withheld)} entries withheld, not 1")
    items = {f"{item.publisher}/{item.slug}": item for item in snapshot.items}
    if set(items) != set(recipes) - set(future):
        failures.append(
            f"Recipes lost: {sorted(set(recipes) - set(future) - set(items))}; "
            f"kept although withheld: {sorted(set(items) & set(future))}"
        )
    kept_models = {
        f"{identity.publisher}/{identity.slug}": model.entry
        for model in snapshot.catalog_entities
        for identity in (model.entry.document.identity,)
    }
    if set(kept_models) != set(models):
        failures.append(f"Models lost: {sorted(set(models) - set(kept_models))}")
    for name, entry in sorted(models.items()):
        kept = kept_models.get(name)
        if kept is None or entry.get("display") is None:
            continue
        if kept.display is None:
            failures.append(f"Model {name} lost its display")
        elif WORD in entry["display"].get("capabilities", ()) and WORD not in _words(
            kept.display.capabilities
        ):
            failures.append(f"Model {name} lost the capability {WORD}")
    withheld_api: list[str] = []
    for name, entry in sorted(recipes.items()):
        item = items.get(name)
        if item is None:
            continue
        failures.extend(
            f"Recipe {name} lost its {block}"
            for block in (*RECIPE_BLOCKS, "api")
            if entry.get(block) is not None
            and getattr(item, block) is None
            # Only an API block may be withheld: its recorded facts stay. Any
            # other block (a qualification, a display) must survive a newer word.
            and not (block == "api" and block in item.withheld_blocks)
        )
        if WORD in (entry.get("served_capabilities") or ()) and WORD not in _words(
            item.served_capabilities
        ):
            failures.append(f"Recipe {name} lost the capability {WORD}")
        if entry.get("api") is not None and item.api is None:
            withheld_api.append(name)
            if not _no_neutral(entry):
                failures.append(
                    f"Recipe {name} API block withheld without an unknown word "
                    "that has no neutral"
                )
    vocabulary = snapshot.vocabulary
    if vocabulary is None or WORD not in {
        word.name for word in vocabulary.capabilities
    }:
        failures.append(f"the vocabulary lost {WORD}")
    return {
        "failures": failures,
        "models": len(kept_models),
        "recipes": len(items),
        "withheld": [problem.detail for problem in snapshot.withheld],
        "withheld_api": withheld_api,
    }


def main() -> int:
    verdict = check(sys.stdin.buffer.read())
    print(json.dumps(verdict, sort_keys=True))
    return 1 if verdict["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
