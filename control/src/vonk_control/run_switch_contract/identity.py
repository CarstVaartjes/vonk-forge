"""Run/Switch identity and cancellation values independent of ORM and workers."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import StringConstraints

from vonk_control.identifiers import UuidId
from vonk_control.strict_json import StrictModel


class RunSwitchCancellation(StrictModel):
    request_key: UuidId
    actor: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    reason: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    requested_at: datetime
