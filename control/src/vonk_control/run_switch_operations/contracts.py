"""Typed records owned by Run/Switch orchestration."""

from pydantic import BaseModel, ConfigDict
from vonk_agent_protocol import RouteState, RunState


class FinalVerificationExpiry(BaseModel):
    """Exact evidence logged when the accepted start deadline passes."""

    model_config = ConfigDict(extra="forbid", strict=True)
    operation_id: str
    run_id: str | None
    run_state: RunState | None
    route_state: RouteState | None
    accepted_start_deadline: str
    status_reason: str | None
