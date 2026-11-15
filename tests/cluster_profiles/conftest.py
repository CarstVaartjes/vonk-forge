from __future__ import annotations

import json
import random
from collections.abc import Callable
from pathlib import Path
from typing import Any

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


VonkctlJson = Callable[..., tuple[int, dict[str, Any]]]


@pytest.fixture
def vonkctl_json(capsys: pytest.CaptureFixture[str]) -> VonkctlJson:
    """Run ``vonkctl --json ARGV`` and return its exit status and document.

    The ``--json`` document is the public client contract, so a command test
    decides on it (with the status and the requests sent), never on printed
    text; ``tests/cluster_profiles/test_cli_wording.py`` owns the wording.
    """

    def run(
        *argv: str,
        control_client: object | None = None,
        request_id_factory: Callable[[], str] | None = None,
        root: Path | None = None,
    ) -> tuple[int, dict[str, Any]]:
        capsys.readouterr()
        status = cli.main(
            ("--json", *argv),
            root=root,
            control_client=control_client,
            request_id_factory=request_id_factory,
        )
        return status, json.loads(capsys.readouterr().out)

    return run
