"""Typed Pyright diagnostics and reviewed source-site exceptions for CI tooling."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class PyrightSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFORMATION = "information"


class PyrightPosition(BaseModel):
    # Pyright owns the external report; fields beyond the consumed projection
    # do not grant authority or change the source-site identity.
    model_config = ConfigDict(extra="ignore")
    line: int
    character: int


class PyrightRange(BaseModel):
    model_config = ConfigDict(extra="ignore")
    start: PyrightPosition


class PyrightDiagnostic(BaseModel):
    model_config = ConfigDict(extra="ignore")
    file: str
    severity: PyrightSeverity
    rule: str | None = None
    message: str
    range: PyrightRange


class PyrightReport(BaseModel):
    model_config = ConfigDict(extra="ignore")
    general_diagnostics: list[PyrightDiagnostic] = Field(alias="generalDiagnostics")


class TypecheckSourceSite(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    file: str
    rule: str
    source: str


class TypecheckDiagnostic(TypecheckSourceSite):
    message: str
    line: int
    column: int


class TypecheckException(TypecheckSourceSite):
    reason: str


class TypecheckExceptionRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: int
    exceptions: list[TypecheckException]
