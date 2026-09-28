from __future__ import annotations

import os
from pathlib import Path


def recipe_library_root() -> Path:
    configured = os.environ.get("VONK_RECIPE_LIBRARY_ROOT")
    if not configured:
        message = (
            "needs_recipe_library: set VONK_RECIPE_LIBRARY_ROOT to the exact "
            "canonical recipe library checkout"
        )
        if os.environ.get("CI", "").lower() == "true":
            raise RuntimeError(message)
        import pytest

        pytest.skip(message, allow_module_level=True)
    root = Path(configured).resolve()
    index = root / "catalog-index.json"
    if not index.is_file():
        message = (
            "needs_recipe_library: VONK_RECIPE_LIBRARY_ROOT must point to a checkout "
            f"containing catalog-index.json: {root}"
        )
        if os.environ.get("CI", "").lower() == "true":
            raise FileNotFoundError(message)
        import pytest

        pytest.skip(message, allow_module_level=True)
    return root
