from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RETIRED_ROOTS = (
    ROOT / "config/catalog/development",
    ROOT / "config/recipes/development",
)


def test_prototype_development_catalog_and_recipe_trees_are_absent() -> None:
    """Development catalog copies cannot shadow the published v2 authorities."""
    assert all(not root.exists() for root in RETIRED_ROOTS)
