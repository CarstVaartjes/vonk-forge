"""The exact input manifest shared by job staging and container adapters."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from vonk_agent_protocol.job_limits import (
    MAX_INPUT_FILE_BYTES,
    MAX_INPUT_FILES,
    MAX_INPUT_TOTAL_BYTES,
)
from vonk_agent_protocol.job_wire import (
    ArtifactName,
    ArtifactSlot,
    Digest,
    MediaType,
    _parse_model,
    _RecipeJobModel,
)
from vonk_agent_protocol.wire_model import AgentProtocolError, WireModel


class RecipeJobInputRequest(_RecipeJobModel):
    """What a client declares for one input: the Controller derives its type."""

    slot: ArtifactSlot
    name: ArtifactName
    size_bytes: int = Field(ge=0, le=MAX_INPUT_FILE_BYTES)
    sha256: Digest

    @model_validator(mode="after")
    def name_is_safe(self) -> RecipeJobInputRequest:
        if self.name == "manifest.json":
            raise ValueError("artifact name is invalid")
        return self


class RecipeJobInputFile(RecipeJobInputRequest):
    media_type: MediaType

    @classmethod
    def parse(cls, raw: Any, *, maximum_bytes: int) -> RecipeJobInputFile:
        parsed = _parse_model(cls, raw, label="artifact input file")
        if parsed.size_bytes > maximum_bytes:
            raise AgentProtocolError("artifact size_bytes is invalid")
        return parsed


class RecipeJobInputManifest(WireModel):
    """Declared user files; manifest.json itself is platform metadata."""

    schema_version: Literal[1]
    total_bytes: int = Field(ge=0, le=MAX_INPUT_TOTAL_BYTES)
    files: list[RecipeJobInputFile] = Field(max_length=MAX_INPUT_FILES)

    @model_validator(mode="after")
    def files_match_totals(self) -> RecipeJobInputManifest:
        names = [file.name for file in self.files]
        if names != sorted(set(names), key=lambda name: name.encode("utf-8")):
            raise ValueError("input filenames must be unique and canonically sorted")
        if self.total_bytes != sum(file.size_bytes for file in self.files):
            raise ValueError("input total_bytes does not match declared files")
        return self
