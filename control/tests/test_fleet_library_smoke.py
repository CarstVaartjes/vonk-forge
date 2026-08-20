from __future__ import annotations

import sys
from pathlib import Path

CONTROL_SRC = Path(__file__).resolve().parents[2] / "control" / "src"
if str(CONTROL_SRC) not in sys.path:
    sys.path.insert(0, str(CONTROL_SRC))

from .fleet_library_smoke import run_fresh_fleet_library_smoke


def test_fresh_fleet_library_smoke() -> None:
    report = run_fresh_fleet_library_smoke()

    assert report["initial_fleet_nodes"] == []
    assert report["active_fleet_nodes"] == ["spk_" + "c" * 32]
    assert report["active_node_occurrences"] == 1
    assert report["revoked_fleet_nodes"] == []
    assert report["audit_actions"] == [
        "agent.enrollment.grant.create",
        "agent.enrollment.submit.approved",
        "agent.node.revoke",
    ]
    assert report["identity_history_revoked"] is True
    assert report["library_recipe_count"] == 0
