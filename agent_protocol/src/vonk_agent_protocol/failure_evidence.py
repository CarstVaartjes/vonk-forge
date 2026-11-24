"""Current bounded failure diagnostics shared by agent and Controller."""

import json
from datetime import datetime
from importlib.resources import files
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from vonk_agent_protocol.page_cache import PageCacheDropEvidence
from vonk_agent_protocol.wire_model import WireModel

MAX_DROPPED_COUNT = 2**64 - 1

#: The key of a process-exit fact naming the cause (Rust ``EXIT_CAUSE_FIELD``).
EXIT_CAUSE_FIELD = "exit_cause"
#: The Rust ``ExitCause::HostMemoryExhausted.token()``, from the shared vector
#: the Rust tests prove equal to it.
HOST_MEMORY_EXHAUSTED_TOKEN: str = json.loads(
    files(__package__).joinpath("vectors/exit-causes-v1.json").read_text("utf-8")
)["tokens"]["HostMemoryExhausted"]


class FailureLogPolicy(WireModel):
    """Exported with the wire graph: the Controller, the agent and the helper
    (Rust reads these through the generated ``FAILURE_LOG_POLICY``) bound every
    retained failure log with these two numbers."""

    # UTF-8 bytes retained per stream. Characters never exceed bytes, so the same
    # number bounds the wire text length.
    log_tail_bytes: Literal[16384] = 16384
    # A whole diagnostics document: both streams, JSON escaping and properties.
    document_bytes: Literal[65536] = 65536


FAILURE_LOG_POLICY = FailureLogPolicy()
LOG_TAIL_BYTES = FAILURE_LOG_POLICY.log_tail_bytes


def add_known_dropped_bytes(prior: int | None, additional: int) -> int | None:
    """Retain unknown stream counts, including an unrepresentable sum."""
    if prior is None:
        return None
    total = prior + additional
    return total if total <= MAX_DROPPED_COUNT else None


class FailureLogTail(WireModel):
    text: str = Field(max_length=LOG_TAIL_BYTES)
    truncated: bool
    # The collectors count physical buffers with usize/Py_ssize_t, rather
    # than accepting an arbitrary mathematical counter. Unknown remains null.
    dropped_bytes: Annotated[int, Field(ge=0, le=MAX_DROPPED_COUNT)] | None
    dropped_lines: Annotated[int, Field(ge=0, le=MAX_DROPPED_COUNT)] | None


class FailureProperty(WireModel):
    name: str = Field(min_length=1, max_length=64)
    value: str = Field(max_length=256)


class ModelStoreLayoutFault(WireModel):
    """Exact managed path and OS cause; not host-path-bearing prose."""

    path: str = Field(
        min_length=1, max_length=4096, pattern=r"^installations/[^/]+/models/.+"
    )
    errno: int | None = Field(default=None, ge=1, le=4095)


class FailureDiagnostics(WireModel):
    # Numeric subprocess status; absent when the process never started or was signalled.
    process_exit_code: int | None = Field(default=None, strict=True, ge=0, le=255)
    model_store_layout: ModelStoreLayoutFault | None = None
    # The page-cache drops the helper made while this workload started.
    page_cache: PageCacheDropEvidence | None = None
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

    @property
    def host_memory_exhausted(self) -> bool:
        """Whether the host memory guard stopped the workload (R1-R5).

        The guard writes the exit-cause fact (and no ``reason``) as a preflight
        property; its token is the Rust ``ExitCause::HostMemoryExhausted`` one,
        read from the shared vector.
        """
        return any(
            item.name == EXIT_CAUSE_FIELD and item.value == HOST_MEMORY_EXHAUSTED_TOKEN
            for item in self.preflight
        )

    @field_validator("collected_at")
    @classmethod
    def aware_timestamp(cls, value: str) -> str:
        if datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("diagnostic timestamp must include its timezone")
        return value

    @model_validator(mode="after")
    def bounded_document(self) -> "FailureDiagnostics":
        if (
            len(self.model_dump_json().encode("utf-8"))
            > FAILURE_LOG_POLICY.document_bytes
        ):
            raise ValueError("failure diagnostics exceed their document bound")
        return self
