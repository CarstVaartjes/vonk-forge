"""Current bounded failure diagnostics shared by agent and Controller."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .wire_model import WireModel

MAX_DROPPED_COUNT = 2**64 - 1


def add_known_dropped_bytes(prior: int | None, additional: int) -> int | None:
    """Retain unknown stream counts, including an unrepresentable sum."""
    if prior is None:
        return None
    total = prior + additional
    return total if total <= MAX_DROPPED_COUNT else None


class FailureLogTail(WireModel):
    text: str = Field(max_length=2048)
    truncated: bool
    # The collectors count physical buffers with usize/Py_ssize_t, rather
    # than accepting an arbitrary mathematical counter. Unknown remains null.
    dropped_bytes: Annotated[int, Field(ge=0, le=MAX_DROPPED_COUNT)] | None
    dropped_lines: Annotated[int, Field(ge=0, le=MAX_DROPPED_COUNT)] | None


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
