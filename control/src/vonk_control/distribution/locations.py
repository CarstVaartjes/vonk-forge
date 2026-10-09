"""Distribution: locations."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

# Where an authorized object sits is as stable as the authorization itself.
_LOCATION_CACHE_ENTRIES = 4096


@dataclass(frozen=True, slots=True)
class ObjectLocation:
    """The stored file the edge serves for one authorized object."""

    size: int
    sha256: str
    path: Path


def _still_stored(location: ObjectLocation, expected_bytes: int) -> bool:
    try:
        metadata = os.lstat(location.path)
    except OSError:
        return False
    return stat.S_ISREG(metadata.st_mode) and metadata.st_size == expected_bytes
