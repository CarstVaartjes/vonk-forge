"""Typed current agent facts retained as inventory items, never command history."""

from enum import StrEnum

from pydantic import Field
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.runtime_preflight import RuntimePreflightResult
from vonk_agent_protocol.wire_model import Digest

from .strict_json import StrictModel


class InventorySnapshotKind(StrEnum):
    REPORT = "report"
    ITEMS = "items"


class PreflightInventoryItem(StrictModel):
    requirements_sha256: Digest
    result: RuntimePreflightResult


class AgentInventoryItems(StrictModel):
    runtime_identity: AgentRuntimeIdentity | None = None
    hostname: str | None = Field(default=None, max_length=255)
    preflight_fingerprint: Digest | None = None
    preflights: list[PreflightInventoryItem] = Field(default_factory=list)
