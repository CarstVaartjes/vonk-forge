"""The protocol project's metadata owns its wheel identity."""

from __future__ import annotations

import tomllib
from pathlib import Path


def protocol_wheel_name(root: Path) -> str:
    project = tomllib.loads(
        (root / "agent_protocol/pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    return f"vonk_agent_protocol-{project['version']}-py3-none-any.whl"
