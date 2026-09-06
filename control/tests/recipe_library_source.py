from __future__ import annotations

import os
from pathlib import Path


def recipe_library_root() -> Path:
    configured = os.environ.get("VONK_RECIPE_LIBRARY_ROOT")
    if not configured:
        raise RuntimeError(
            "VONK_RECIPE_LIBRARY_ROOT must point to the exact canonical recipe "
            "library checkout"
        )
    root = Path(configured).resolve()
    index = root / "catalog-index.json"
    if not index.is_file():
        raise FileNotFoundError(
            f"VONK_RECIPE_LIBRARY_ROOT does not contain catalog-index.json: {root}"
        )
    return root
