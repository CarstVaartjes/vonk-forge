"""Typed decoding of the external Pyright JSON protocol and local findings."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CheckerPosition(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    line: int = Field(ge=0)
    character: int = Field(ge=0)


class CheckerRange(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    start: CheckerPosition
    end: CheckerPosition | None = None


class CheckerDiagnostic(BaseModel):
    # Pyright owns this protocol; additional external fields are permitted.
    model_config = ConfigDict(extra="ignore", strict=True)

    file: str = Field(min_length=1)
    severity: Literal["error", "warning", "information"]
    message: str
    range: CheckerRange
    rule: str | None = None


class CheckerReport(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    generalDiagnostics: list[CheckerDiagnostic]


class CheckerFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    file: str
    rule: str
    message: str
    line: int = Field(ge=1)
    column: int = Field(ge=1)


class CheckerException(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    file: str = Field(min_length=1)
    rule: str = Field(min_length=1)
    count: int = Field(ge=1)
    reason: str = ""


class CheckerBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1]
    exceptions: list[CheckerException]

    @model_validator(mode="after")
    def distinct_entries(self) -> CheckerBaseline:
        keys = [(entry.file, entry.rule) for entry in self.exceptions]
        if len(set(keys)) != len(keys):
            raise ValueError("type-check baseline contains conflicting entries")
        return self
