"""Agent-facing and background write paths never lock agent_nodes.

Admission and publication own the identity/intent row; a lock taken to protect
a child insert refuses admission and queues every foreign-key check behind it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from . import optimistic_boundaries as optimistic
from .added_line_guards import check_source

SRC = Path(__file__).resolve().parents[1] / "src" / "vonk_control"
AGENT_FACING = (
    *sorted(SRC.glob("telemetry*.py")),
    SRC / "inventory_repository.py",
    SRC / "agent_contacts.py",
    SRC / "installed_bytes.py",
    *sorted((SRC / "agent_api").glob("*.py")),
)


@pytest.mark.parametrize("path", AGENT_FACING, ids=lambda path: path.name)
def test_no_agent_node_lock_in_agent_facing_modules(path: Path) -> None:
    assert optimistic.scan_agent_node_locks(path.read_text()) == []


@pytest.mark.parametrize(
    "source",
    [
        "def f(s):\n    s.scalar(select(AgentNode).with_for_update(of=AgentNode))\n",
        "def f(s):\n    s.scalar(select(AgentNode).with_for_update(skip_locked=True))\n",
        "def f(s):\n    s.get(AgentNode, 'n', with_for_update=True)\n",
        "def f(s):\n    s.execute(text('SELECT 1 FROM agent_nodes FOR UPDATE'))\n",
        "def f(s):\n    s.execute(text('SELECT 1 FROM agent_nodes FOR NO KEY UPDATE'))\n",
        "def f(s):\n    s.execute(text('LOCK TABLE agent_nodes IN EXCLUSIVE MODE'))\n",
    ],
)
def test_a_new_agent_node_lock_outside_admission_is_refused(source: str) -> None:
    path = "control/src/vonk_control/telemetry_new.py"
    hits = check_source(path, source, {1, 2}, modes=("optimistic",))
    assert any("agent_nodes lock" in hit for hit in hits)


def test_the_security_edge_may_lock_agent_nodes() -> None:
    source = (
        "def f(s):\n    s.scalar(select(AgentNode).with_for_update(of=AgentNode))\n"
    )
    path = "control/src/vonk_control/security/identity.py"
    hits = check_source(path, source, {1, 2}, modes=("optimistic",))
    assert not any("agent_nodes lock" in hit for hit in hits)
