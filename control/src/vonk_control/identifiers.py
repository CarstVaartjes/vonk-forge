"""Pattern-bound identifier strings only Controller contracts share."""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints

# Version 1-5, RFC 4122 variant. The agent protocol's looser ``UuidId`` is a
# different type.
UUID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

UuidId = Annotated[str, StringConstraints(pattern=UUID_PATTERN)]
