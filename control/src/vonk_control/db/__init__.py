"""Database access.

Nothing is re-exported except ``build_engine`` and ``session_factory``: lane
programs that run inside an installed Controller of the previous release name
them here (see ``tests/test_baseline_program_surface.py``), so they resolve
lazily to avoid importing the connection layer with the package.
"""

from importlib import import_module
from typing import Any


def __getattr__(name: str) -> Any:
    if name in {"build_engine", "session_factory"}:
        return getattr(import_module(".connections", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
