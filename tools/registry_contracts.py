"""Current local inventory publication records, shared by reader and writer."""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class RegistryManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    shards: dict[str, str]

    @field_validator("shards")
    @classmethod
    def valid_shards(cls, shards: dict[str, str]) -> dict[str, str]:
        if "_global.json" not in shards:
            raise ValueError("registry inventory lacks its global shard")
        for name, digest in shards.items():
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or not name.endswith(".json"):
                raise ValueError("registry shard escapes its owner")
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("registry shard fingerprint is malformed")
        return shards


class RegistryPublication(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    previous: dict[str, str]
    desired: dict[str, str]
    previous_manifest: RegistryManifest | None
    desired_manifest: RegistryManifest

    @model_validator(mode="after")
    def complete_snapshots(self) -> RegistryPublication:
        for fragments, manifest in (
            (self.previous, self.previous_manifest),
            (self.desired, self.desired_manifest),
        ):
            if manifest is None:
                if fragments:
                    raise ValueError("unpublished registry has visible fragments")
                continue
            measured = {
                name: hashlib.sha256(text.encode("utf-8")).hexdigest()
                for name, text in fragments.items()
            }
            if measured != manifest.shards:
                raise ValueError("registry publication snapshot is incomplete")
        return self
