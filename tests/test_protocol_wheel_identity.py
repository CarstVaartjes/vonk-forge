"""Catch hand-pinned wheel consumers that break after a protocol version bump."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from scripts.protocol_wheel import protocol_wheel_name

ROOT = Path(__file__).resolve().parents[1]


def test_wheel_name_follows_project_version_from_any_directory(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    project = root / "agent_protocol/pyproject.toml"
    project.parent.mkdir(parents=True)
    version = "9.8.7"
    project.write_text(
        f'[project]\nname = "vonk-agent-protocol"\nversion = "{version}"\n'
    )
    (root / "scripts").mkdir()
    for name in ("protocol-wheel-name", "protocol_wheel.py"):
        shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
    result = subprocess.run(
        [sys.executable, str(root / "scripts/protocol-wheel-name")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == protocol_wheel_name(root)
    assert result.stdout.strip() == f"vonk_agent_protocol-{version}-py3-none-any.whl"
