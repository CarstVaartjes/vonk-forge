"""Shared helpers for the spark_lifecycle_runner tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ENTRY_POINT = Path(__file__).parent / "acceptance/test_spark_lifecycle.py"


def _module():
    specification = importlib.util.spec_from_file_location(
        "spark_lifecycle_runner", ENTRY_POINT
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module
