"""Local retention delegation; no arbitrary privileged filesystem paths."""

from typing import Annotated
from uuid import UUID

from pydantic import Field

from .wire_model import WireEnum, WireModel


class AgentRetainedStore(WireEnum):
    RUNS = "runs"
    INSTALLATIONS = "installations"


class AgentStoreRemovalRequest(WireModel):
    """An already eligible agent-owned scope, bound to its filesystem identity.

    The local agent peer may remove disposable store bytes, never a container.
    The helper independently refuses mounted scopes and concurrent launches.
    """

    request_id: UUID
    store: AgentRetainedStore
    scope_id: UUID
    device: Annotated[int, Field(ge=0, le=2**64 - 1)]
    inode: Annotated[int, Field(ge=0, le=2**64 - 1)]


class StoreRetentionFailure(WireModel):
    """The first unresolved removal in a pass; other stores keep progressing."""

    path: Annotated[str, Field(min_length=1, max_length=4096)]
    diagnostic: Annotated[str, Field(min_length=1, max_length=1024)]
    retry_after_seconds: Annotated[int, Field(gt=0, le=3600)] = 60
