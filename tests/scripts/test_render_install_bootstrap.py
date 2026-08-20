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


def _package(tmp_path: Path, platform: str) -> Path:
    architecture = {"linux-amd64": "amd64", "linux-arm64": "arm64"}[platform]
    root = tmp_path / f"package-root-{architecture}"
    (root / "DEBIAN").mkdir(parents=True)
    (root / "DEBIAN/control").write_text(
        "Package: vonk-forge-agent\n"
        "Version: 1.2.3~dev.4+g0123456789ab\n"
        f"Architecture: {architecture}\n"
        "Maintainer: test <test@example.test>\n"
        "Description: test\n"
    )
    package = tmp_path / f"agent-{platform}.deb"
    subprocess.run(
        ["/usr/bin/dpkg-deb", "--build", "--root-owner-group", root, package],
        check=True,
        capture_output=True,
    )
    return package


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
    else:
        for platform in artifacts:
            package = _package(tmp_path, platform)
            command.extend(("--package", f"{platform}={package}"))

    result = subprocess.run(
        command, cwd=ROOT, text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr
    rendered = output.read_text()
    assert re.search(r"@[A-Z0-9_]+_SHA256@", rendered) is None
    for artifact in artifacts.values():
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() in rendered
    if kind == "nas":
        assert hashlib.sha256(payload.read_bytes()).hexdigest() in rendered
    else:
        for platform in artifacts:
            package = tmp_path / f"agent-{platform}.deb"
            assert hashlib.sha256(package.read_bytes()).hexdigest() in rendered
        assert "@SPARK_" not in rendered
        assert rendered.count("1.2.3~dev.4+g0123456789ab") == 2
    assert (
        result.stdout == f"sha256:{hashlib.sha256(output.read_bytes()).hexdigest()}\n"
    )


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
            check=False,
        )
        assert result.returncode == 2
        assert not output.exists()


def test_renderer_binds_downloads_to_one_immutable_release_base(
    tmp_path: Path,
) -> None:
    artifacts = _artifacts(tmp_path, "nas")
    payload = tmp_path / "payload.json"
    payload.write_text('{"schema_version":1}\n')
    output = tmp_path / "nas"
    base_url = "https://install.vonkforge.ai/artifacts/stable/releases/" + "a" * 64
    command = [
        sys.executable,
        str(SCRIPT),
        "--kind",
        "nas",
        "--payload",
        str(payload),
        "--base-url",
        base_url,
        "--output",
        str(output),
    ]
    for platform, artifact in artifacts.items():
        command.extend(("--artifact", f"{platform}={artifact}"))

    result = subprocess.run(
        command, cwd=ROOT, text=True, capture_output=True, check=False
    )

    assert result.returncode == 0, result.stderr
    rendered = output.read_text()
    assert f"base_url=${{VONK_INSTALL_BASE_URL:-{base_url}}}" in rendered
    assert "https://install.vonkforge.ai/artifacts}" not in rendered
