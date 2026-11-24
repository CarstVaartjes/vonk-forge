"""Find a shared id pattern spelled again instead of imported.

``vonk_agent_protocol.identifiers`` owns the digest, OCI digest, node id and
UUID patterns (``vonk_control.identifiers`` the Controller UUID). A string that
equals one of them is a second definition of that id type: import the pattern
constant or the ``Annotated`` type instead.
"""

from __future__ import annotations

import ast

from vonk_agent_protocol import identifiers as protocol
from vonk_control import identifiers as controller

KIND = "shared-id-pattern-respelled"
PATTERNS = frozenset(
    {
        protocol.DIGEST_PATTERN,
        protocol.OCI_DIGEST_PATTERN,
        protocol.NODE_PATTERN,
        protocol.UUID_PATTERN,
        protocol.UUID4_PATTERN,
        controller.UUID_PATTERN,
    }
)
# The owners, and the file the LiteLLM image loads with no package around it.
OWNERS = (
    "agent_protocol/src/vonk_agent_protocol/identifiers.py",
    "control/src/vonk_control/identifiers.py",
    "agent_protocol/src/vonk_agent_protocol/route_activation.py",
)


def scan_source(path: str, source: str) -> list[int]:
    if path.endswith(OWNERS):
        return []
    return sorted(
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and node.value in PATTERNS
    )
