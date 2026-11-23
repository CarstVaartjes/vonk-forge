"""HTTP API.

Nothing is re-exported except ``production_app``: lane programs that run inside an
installed Controller of the previous release name it there (see
``tests/test_baseline_program_surface.py``), so it resolves lazily to avoid
importing the whole API with the package.
"""

from importlib import import_module
from typing import Any


def __getattr__(name: str) -> Any:
    if name == "production_app":
        return import_module(".production", __name__).production_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
