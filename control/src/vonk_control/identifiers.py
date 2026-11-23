"""Pattern-bound identifier strings Controller contracts share."""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints

_UUID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


UuidId = Annotated[str, StringConstraints(pattern=_UUID_PATTERN)]
