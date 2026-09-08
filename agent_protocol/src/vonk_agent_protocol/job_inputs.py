"""The exact input manifest shared by job staging and container adapters."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .recipe_jobs import MAX_INPUT_FILES, MAX_INPUT_TOTAL_BYTES, RecipeJobInputFile
from .wire_model import WireModel


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
