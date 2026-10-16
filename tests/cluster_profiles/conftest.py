from __future__ import annotations

import random
from pathlib import Path

import pytest

from cluster_profiles import cli
from cluster_profiles.control_client import common


@pytest.fixture(autouse=True)
def no_update_notice_network(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Update notices are on by default; keep them off the network and home cache."""

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "update-notice-cache"))
    monkeypatch.setattr(cli, "begin_interactive_update_check", lambda: None)


@pytest.fixture(autouse=True)
def deterministic_observation_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seed observation backoff jitter so retries per budget never vary by run.

    Observation retries end at a time budget, so with unseeded jitter the number
    of attempts that fit varies; any count assertion would then flake. Assert the
    budget (fake clock) instead; this seed keeps any remaining count deterministic.
    """

    monkeypatch.setattr(common, "random", random.Random(0))
