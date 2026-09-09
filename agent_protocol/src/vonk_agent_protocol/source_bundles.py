"""Canonical source-bundle digest document and verified storage metadata."""
from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .contracts import canonical_message
from .wire_model import WireModel

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SourceBundleFile(WireModel):
    path: str = Field(min_length=1, max_length=512)
    mode: Literal[420, 493]
    size: int = Field(ge=0, le=32 * 1024 * 1024)
    sha256: Digest

    @field_validator("path")
    @classmethod
    def canonical_relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute() or path.as_posix() != value
            or any(part in {"", ".", ".."} for part in path.parts)
            or "\x00" in value or len(value.encode("utf-8")) > 512
        ):
            raise ValueError("source bundle path is not canonical and relative")
        return value


class SourceBundleDigestManifest(WireModel):
    schema_version: Literal[1]
    files: tuple[SourceBundleFile, ...] = Field(max_length=4096)
    total_bytes: int = Field(ge=0, le=256 * 1024 * 1024)

    @model_validator(mode="after")
    def canonical_file_set(self) -> SourceBundleDigestManifest:
        paths = [item.path for item in self.files]
        if paths != sorted(set(paths), key=lambda value: value.encode("utf-8")):
            raise ValueError("source bundle files must be unique and canonically sorted")
        if self.total_bytes != sum(item.size for item in self.files):
            raise ValueError("source bundle total differs from its files")
        return self

    def digest(self) -> str:
        document = self.model_dump(mode="json", include=set(SourceBundleDigestManifest.model_fields))
        return hashlib.sha256(canonical_message(document)).hexdigest()


class SourceBundleManifest(SourceBundleDigestManifest):
    sha256: Digest

    @model_validator(mode="after")
    def binds_digest_document(self) -> SourceBundleManifest:
        if self.sha256 != self.digest():
            raise ValueError("source bundle manifest digest does not match")
        return self
