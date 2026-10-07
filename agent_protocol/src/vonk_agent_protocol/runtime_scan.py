"""Node-local durable traversal checkpoint for managed run observation.

The existing SQLite StateStore owns these records; they are not grants or
proof of an empty node. Filesystem identity and the preceding entry witness
must still be checked against actual native directory metadata on restart.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AwareDatetime, Field

from .wire_model import WireModel

# These are the actual native off_t/stat integer domains, not workload or
# retained-history limits. Bounds stay on the integer inside nullable fields.
FilesystemInteger = Annotated[int, Field(strict=True, ge=-(2**63), le=2**63 - 1)]
FilesystemByte = Annotated[
    int, Field(strict=True, ge=0, le=255, json_schema_extra={"format": "uint8"})
]


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
    started_at: AwareDatetime
    witness: RecipeRunObservationCursorWitness | None
    had_plans: bool
    had_failures: bool
