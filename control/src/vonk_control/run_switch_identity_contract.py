"""Run/Switch identity and cancellation values independent of ORM and workers."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import StringConstraints

from .strict_json import StrictModel

_UUID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_NODE_PATTERN = r"^spk_[0-9a-f]{32}$"
UuidId = Annotated[str, StringConstraints(pattern=_UUID_PATTERN)]
NodeId = Annotated[str, StringConstraints(pattern=_NODE_PATTERN)]


class RunSwitchCancellation(StrictModel):
    request_key: UuidId
    actor: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    reason: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    requested_at: datetime
