"""Catch hand-pinned wheel consumers that break after a protocol version bump."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.protocol_wheel import protocol_wheel_name

ROOT = Path(__file__).resolve().parents[1]
LITERAL_WHEEL = re.compile(r"vonk_agent_protocol-\d[^\s\"'<>/]*\.whl")


def literal_wheel_names(root: Path, paths: list[str]) -> list[str]:
    return [
        f"{relative}:{number}"
        for relative in paths
        # uv owns generated resolution records, including exact filenames/hashes.
        if Path(relative).name != "uv.lock"
        for number, line in enumerate(
            (root / relative)
            .read_text(encoding="utf-8", errors="replace")
            .splitlines(),
            start=1,
        )
        if LITERAL_WHEEL.search(line)
    ]


def test_no_literal_protocol_wheel_names_in_build_inputs() -> None:
    inputs = []
    for directory, names, files in os.walk(ROOT):
        # Source scans must be independent of Git and local build/cache outputs.
        names[:] = [
            name
            for name in names
            if (not name.startswith(".") or name in {".github", ".githooks"})
            and name
            not in {
                "tests",
                "node_modules",
                "__pycache__",
                "target",
                "build",
                "dist",
                "generated_control",
            }
        ]
        inputs.extend(
            str((Path(directory) / name).relative_to(ROOT))
            for name in files
            if not (Path(directory) / name).is_symlink()
        )
    findings = literal_wheel_names(ROOT, inputs)
    assert not findings, (
        "Derive protocol wheel names with scripts/protocol-wheel-name: "
        + ", ".join(findings)
    )


@pytest.mark.parametrize(
    "relative",
    (
        ".github/workflows/ci.yml",
        ".githooks/pre-commit",
        "scripts/build",
        "packaging/build.toml",
        "control/Dockerfile",
        ".dockerignore",
        "docs/runbook.md",
    ),
)
def test_guard_rejects_injected_literal(tmp_path: Path, relative: str) -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("copy vonk_agent_protocol-9.8.7-py3-none-any.whl\n")
    assert literal_wheel_names(tmp_path, [relative]) == [f"{relative}:1"]


def test_wheel_name_follows_project_version_from_any_directory(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    project = root / "agent_protocol/pyproject.toml"
    project.parent.mkdir(parents=True)
    project.write_text('[project]\nname = "vonk-agent-protocol"\nversion = "9.8.7"\n')
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
    assert result.stdout.strip() == "vonk_agent_protocol-9.8.7-py3-none-any.whl"
