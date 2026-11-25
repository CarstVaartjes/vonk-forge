from __future__ import annotations

import os
from pathlib import Path

import pytest


def recipe_library_root() -> Path:
    configured = os.environ.get("VONK_RECIPE_LIBRARY_ROOT")
    if not configured:
        message = (
            "needs_recipe_library: set VONK_RECIPE_LIBRARY_ROOT to the exact "
            "canonical recipe library checkout"
        )
        if os.environ.get("CI", "").lower() == "true":
            raise RuntimeError(message)
        pytest.skip(message, allow_module_level=True)
    root = Path(configured).resolve()
    index = root / "catalog-index.json"
    if not index.is_file():
        message = (
            "needs_recipe_library: VONK_RECIPE_LIBRARY_ROOT must point to a checkout "
            f"with a built catalog-index.json; run scripts/recipes/build-recipe-library {root}"
        )
        if os.environ.get("CI", "").lower() == "true":
            raise FileNotFoundError(message)
        pytest.skip(message, allow_module_level=True)
    return root
