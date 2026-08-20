from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests/acceptance/test_fresh_nas_install.py"


def _acceptance_module():
    spec = importlib.util.spec_from_file_location("fresh_nas_acceptance", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_only_hermes_bundle_receives_the_expensive_reference_rollout(
    tmp_path: Path,
) -> None:
    acceptance = _acceptance_module()
    default = tmp_path / "default"
    hermes = tmp_path / "hermes"

    assert acceptance.reference_rollout_bundles(default, hermes) == (hermes,)
