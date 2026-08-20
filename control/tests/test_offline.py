import json
import os
from contextlib import nullcontext
from pathlib import Path

import pytest
from vonk_control import offline
from vonk_control.offline import (
    OfflineConflict,
    main,
    require_offline,
)


def test_offline_mutation_refuses_healthy_api(tmp_path: Path) -> None:
    with pytest.raises(OfflineConflict, match="control plane is running"):
        require_offline(tmp_path, probe=lambda: True, owner_uid=os.geteuid())


def test_init_creates_only_host_upgrade_state_without_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(offline, "require_offline", lambda *_args, **_kwargs: nullcontext())
    state = tmp_path / "state"
    assert main(["--state-path", str(state), "init"]) == 0
    assert state.is_dir()


def test_installed_updater_exposes_only_allowlisted_maintenance_actions(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as stopped:
        main(["maintenance", "--help"])

    assert stopped.value.code == 0
    output = capsys.readouterr().out
    for action in (
        "status",
        "logs",
        "tailscale-status",
        "tailscale-serve-status",
        "tailscale-serve-config",
        "step-ca-health",
    ):
        assert action in output
    assert "hermes-setup" not in output


def test_installed_updater_routes_maintenance_without_loading_release_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[object, ...]] = []

    class Store:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def load_active(self) -> object:
            return type("Selected", (), {"generation_id": "gen-" + "a" * 24})()

    class Boundary:
        def maintenance(self, *args: object, **kwargs: object) -> dict[str, object]:
            calls.append((*args, kwargs))
            return {"action": "step-ca-health", "mode": "diagnostic"}

    monkeypatch.setattr(offline, "HostGenerationStore", Store)
    monkeypatch.setattr(offline, "HostUpgradeBoundary", lambda **_kwargs: Boundary())
    monkeypatch.setattr(
        offline,
        "_load_release_source",
        lambda _path: pytest.fail("maintenance must not load release authority"),
    )

    result = main(
        [
            "--state-path",
            str(tmp_path / "state"),
            "--identity-path",
            str(tmp_path / "identity"),
            "maintenance",
            "step-ca-health",
        ]
    )

    assert result == 0
    assert calls == [
        (
            "step-ca-health",
            {
                "service": None,
                "since_minutes": 30,
                "apply": False,
                "expected_generation": None,
            },
        )
    ]
    assert json.loads(capsys.readouterr().out)["mode"] == "diagnostic"
