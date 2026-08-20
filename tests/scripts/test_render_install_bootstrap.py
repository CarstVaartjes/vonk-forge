from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/render-install-bootstrap"


def _artifacts(tmp_path: Path, kind: str) -> dict[str, Path]:
    platforms = {
        "nas": ("linux-amd64", "linux-arm64", "darwin-amd64", "darwin-arm64"),
        "spark": ("linux-amd64", "linux-arm64"),
    }[kind]
    result = {}
    for platform in platforms:
        artifact = tmp_path / f"{kind}-{platform}"
        artifact.write_bytes(f"native installer for {platform}\n".encode())
        result[platform] = artifact
    return result


@pytest.mark.parametrize("kind", ("nas", "spark"))
def test_renderer_pins_every_supported_native_installer(
    tmp_path: Path, kind: str
) -> None:
    artifacts = _artifacts(tmp_path, kind)
    payload = tmp_path / "payload.json"
    payload.write_text('{"schema_version":1}\n')
    output = tmp_path / kind
    command = [
        sys.executable,
        str(SCRIPT),
        "--kind",
        kind,
        "--output",
        str(output),
    ]
    for platform, artifact in artifacts.items():
        command.extend(("--artifact", f"{platform}={artifact}"))
    if kind == "nas":
        command.extend(("--payload", str(payload)))

    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    rendered = output.read_text()
    assert re.search(r"@[A-Z0-9_]+_SHA256@", rendered) is None
    for artifact in artifacts.values():
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() in rendered
    if kind == "nas":
        assert hashlib.sha256(payload.read_bytes()).hexdigest() in rendered
    assert result.stdout == f"sha256:{hashlib.sha256(output.read_bytes()).hexdigest()}\n"


def test_renderer_fails_closed_on_missing_duplicate_or_unsafe_inputs(
    tmp_path: Path,
) -> None:
    artifacts = _artifacts(tmp_path, "spark")
    first = artifacts["linux-amd64"]
    unsafe = tmp_path / "unsafe"
    unsafe.symlink_to(first)

    for arguments in (
        ["--artifact", f"linux-amd64={first}"],
        [
            "--artifact",
            f"linux-amd64={first}",
            "--artifact",
            f"linux-amd64={first}",
        ],
        [
            "--artifact",
            f"linux-amd64={unsafe}",
            "--artifact",
            f"linux-arm64={artifacts['linux-arm64']}",
        ],
    ):
        output = tmp_path / f"failed-{len(arguments)}-{arguments[-1].replace('/', '_')}"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--kind",
                "spark",
                "--output",
                str(output),
                *arguments,
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 2
        assert not output.exists()
