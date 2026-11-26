"""Where the synthetic canary's package bytes live.

The canary fixture index names its package in the release layout
(``packages/<name>``), but the library does not build the canary, so the bytes
sit beside the fixture, in its ``package/`` directory, as the recipe library's
own tests read them. Every consumer resolves the archive through this function.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath


def canary_package_path(index_path: Path, package_path: str) -> Path:
    """The canary archive named ``package_path`` by the fixture index at ``index_path``."""

    return index_path.parent / "package" / PurePosixPath(package_path).name
