"""Node-local durable traversal checkpoint for managed run observation.

The existing SQLite StateStore owns these records; they are not grants or
proof of an empty node. Filesystem identity and the preceding entry witness
must still be checked against actual native directory metadata on restart.
"""

from __future__ import annotations

import calendar
import re
from typing import Annotated

from pydantic import Field, field_validator

from .wire_model import WireModel

# These are the actual native off_t/stat integer domains, not workload or
# retained-history limits. Bounds stay on the integer inside nullable fields.
FilesystemInteger = Annotated[int, Field(strict=True, ge=-(2**63), le=2**63 - 1)]
FilesystemByte = Annotated[
    int, Field(strict=True, ge=0, le=255, json_schema_extra={"format": "uint8"})
]


# Match the actual chrono RFC3339 reader, including its documented literal
# space separator, leap seconds, year zero and arbitrary fraction precision.
_CUTOFF = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt ]"
    r"([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.[0-9]+)?"
    r"(?:[Zz]|[+-]([0-9]{2}):([0-9]{2}))"
)


class RecipeRunObservationDirectoryStamp(WireModel):
    """Exact identity and modification witnesses of one native directory."""

    device: str
    inode: str
    modified_seconds: FilesystemInteger
    modified_nanoseconds: FilesystemInteger
    changed_seconds: FilesystemInteger
    changed_nanoseconds: FilesystemInteger


class RecipeRunObservationCursorWitness(WireModel):
    """An opaque cursor is reusable only after its preceding entry matches."""

    before: FilesystemInteger
    after: FilesystemInteger
    name: list[FilesystemByte]
    inode: str


class RecipeRunObservationCheckpoint(WireModel):
    """Existing recipe_observation_scan_v1 JSON, without a format migration."""

    root: str
    runs_stamp: RecipeRunObservationDirectoryStamp
    metadata_stamp: RecipeRunObservationDirectoryStamp | None
    started_at: str = Field(pattern=re.compile("^" + _CUTOFF.pattern + r"$(?![\s\S])"))
    witness: RecipeRunObservationCursorWitness | None
    had_plans: bool
    had_failures: bool

    @field_validator("started_at")
    @classmethod
    def aware_cutoff_without_normalization(cls, value: str) -> str:
        match = _CUTOFF.fullmatch(value)
        if match is None:
            raise ValueError("observation cutoff must be an RFC3339 timestamp")
        year, month, day, hour, minute, second, offset_hour, offset_minute = (
            int(part) if part is not None else 0 for part in match.groups()
        )
        if not (
            1 <= month <= 12
            and 1 <= day <= calendar.monthrange(year, month)[1]
            and hour <= 23
            and minute <= 59
            and second <= 60
            and offset_hour <= 23
            and offset_minute <= 59
        ):
            raise ValueError("observation cutoff has invalid calendar or offset fields")
        # No datetime conversion: even digits beyond native nanosecond precision
        # remain the original retained lexeme, as with the preceding SQL owner.
        return value
