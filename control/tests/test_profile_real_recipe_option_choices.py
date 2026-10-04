"""Stale saved option choices never block a profile, on the real recipe library.

Recipe refreshes add, rename and drop options (a two-Spark GLM recipe gained a
``drafter`` value, others dropped per-request options), while a saved profile
re-submits whatever it stored on every edit.
"""

from __future__ import annotations

import json

from vonk_control.fleet_profiles import _effective_option_choices

from .recipe_library_source import recipe_library_root

ROOT = recipe_library_root()


def _recipes() -> list[dict[str, object]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((ROOT / "recipes").glob("*.json"))
    ]


def test_every_library_recipe_replaces_stale_choices_with_its_defaults() -> None:
    recipes = _recipes()
    assert recipes
    optioned = 0
    for document in recipes:
        declared = {
            option["name"]: option
            for option in document.get("options", [])  # type: ignore[attr-defined]
        }
        defaults, notes = _effective_option_choices(document, {})
        assert notes == []
        assert set(defaults) == set(declared)

        stale = {name: "value-the-recipe-never-offered" for name in declared}
        stale["option-the-recipe-never-declared"] = "x"
        effective, notes = _effective_option_choices(document, stale)

        # Everything stale is replaced by the default, each replacement named.
        assert effective == defaults
        assert len(notes) == len(stale)

        # A choice the recipe still offers survives next to a stale one.
        for name, option in declared.items():
            offered = [choice["value"] for choice in option["choices"]]
            kept = offered[-1]
            effective, _notes = _effective_option_choices(
                document, {**stale, name: kept}
            )
            assert effective[name] == kept
            optioned += 1
    assert optioned
