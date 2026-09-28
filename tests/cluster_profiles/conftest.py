from __future__ import annotations

from pathlib import Path

import pytest

from cluster_profiles import cli


@pytest.fixture(autouse=True)
def no_update_notice_network(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Update notices are on by default; keep them off the network and home cache."""

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "update-notice-cache"))
    monkeypatch.setattr(cli, "begin_interactive_update_check", lambda: None)
