"""Refuse a new comparison or assignment of ``.state`` to a quoted state word.

State words have one vocabulary: the ``WireEnum`` state machines in
``vonk_agent_protocol.state_machines``. A quoted ``"active"`` beside a ``.state``
attribute is a second spelling of an enum member; use the member.
"""

from __future__ import annotations

import inspect
import re

from vonk_agent_protocol import state_machines
from vonk_agent_protocol.wire_model import WireEnum

KIND = "quoted state word on a .state attribute; use the state-machine enum member"
WORDS = frozenset(
    str(member.value)
    for _, enum in inspect.getmembers(state_machines, inspect.isclass)
    if issubclass(enum, WireEnum) and enum is not WireEnum
    for member in enum
    if re.fullmatch(r"[a-z_]+", str(member.value))
)
PATTERN = re.compile(
    r"\.state\s*(?:==|!=|=)\s*[\"'](?:" + "|".join(sorted(WORDS)) + r")[\"']"
)


def in_scope(path: str) -> bool:
    return path.startswith("control/src/vonk_control/") and path.endswith(".py")


def scan_source(source: str) -> list[int]:
    return [
        number
        for number, text in enumerate(source.splitlines(), 1)
        if PATTERN.search(text)
    ]
