"""Complete public-kit membership produced while assembling the Controller image."""

from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class RuntimeAssetInventory(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal[2]
    files: tuple[str, ...]

    @field_validator("files")
    @classmethod
    def safe_members(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(set(value)) != len(value):
            raise ValueError("kit inventory must contain unique members")
        for member in value:
            path = PurePosixPath(member)
            if path.is_absolute() or ".." in path.parts or path.as_posix() != member:
                raise ValueError("kit member must be a relative canonical path")
        return value
