"""Shared recipe-image identity and builder handoff contracts."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BeforeValidator, ConfigDict, Field
from vonk_agent_protocol import LifecycleState, LifecycleSubject, state_adopter
from vonk_agent_protocol.build_import import RecipeBuildEvidence

from .strict_json import StrictJSONModel

RecipeImageAvailabilityState = Annotated[
    Literal[
        LifecycleState.QUEUED,
        LifecycleState.RUNNING,
        LifecycleState.BACKOFF,
        LifecycleState.OBSERVING,
        LifecycleState.SUCCEEDED,
        LifecycleState.FAILED,
        LifecycleState.CANCELLED,
    ],
    # A row written before the rename may still say ``partial`` or ``cancelling``.
    BeforeValidator(state_adopter(LifecycleSubject.JOB)),
]


class RecipeImageAvailabilityArtifact(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    key: str = Field(min_length=1, max_length=256)
    id: str = Field(min_length=1, max_length=256)
    path: str = Field(min_length=1, max_length=1024)
    kind: str = Field(min_length=1, max_length=64)
    repository: str | None = None
    source: str = Field(min_length=1, max_length=1024)
    revision: str | None = Field(default=None, max_length=256)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    download_bytes: int = Field(ge=0)
    roles: list[str]
    model_content_sha256: str | None = None


class AvailabilityBuildReceipt(RecipeBuildEvidence):
    """The builder's verified archive and the exact execution that produced it."""

    state: Literal[LifecycleState.SUCCEEDED]
    build_id: str = Field(min_length=1, max_length=128)
    builder_node_id: str | None = Field(default=None, min_length=1, max_length=128)
    build_input_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
