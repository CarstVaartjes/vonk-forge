"""Current bounded failure diagnostics shared by agent and Controller."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .wire_model import WireModel


class FailureLogTail(WireModel):
    text: str = Field(max_length=2048)
    truncated: bool
    dropped_bytes: int | None = Field(ge=0)
    dropped_lines: int | None = Field(ge=0)


class FailureProperty(WireModel):
    name: str = Field(min_length=1, max_length=64)
    value: str = Field(max_length=256)


class FailureDiagnostics(WireModel):
    schema_version: Literal[1] = 1
    collected_at: str = Field(min_length=1, max_length=64)
    phase: str = Field(min_length=1, max_length=80)
    category: Literal[
        "platform-policy",
        "capacity",
        "network",
        "digest",
        "timeout",
        "runtime",
        "unknown",
    ]
    stdout: FailureLogTail
    stderr: FailureLogTail
    versions: list[FailureProperty] = Field(max_length=8)
    sandbox: list[FailureProperty] = Field(max_length=12)
    storage: list[FailureProperty] = Field(max_length=8)
    preflight: list[FailureProperty] = Field(max_length=8)
    collector_errors: list[Annotated[str, Field(max_length=256)]] = Field(max_length=8)

    @field_validator("collected_at")
    @classmethod
    def aware_timestamp(cls, value: str) -> str:
        if datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("diagnostic timestamp must include its timezone")
        return value

    @model_validator(mode="after")
    def bounded_document(self) -> "FailureDiagnostics":
        if len(self.model_dump_json().encode("utf-8")) > 16 * 1024:
            raise ValueError("failure diagnostics exceed 16 KiB")
        return self
