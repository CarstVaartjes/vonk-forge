from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import shutil
import struct
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ROOT = Path(__file__).resolve().parents[2]
BUILDER = ROOT / "agent/tools/build-slot-artifact"
PROTOCOL = ROOT / "inventory/wheels/vonk_agent_protocol-2.1.0-py3-none-any.whl"


def test_rust_slot_manifest_fixture_is_canonical_and_domain_signed() -> None:
    raw = (ROOT / "agent_protocol/fixtures/slot-manifest.json").read_bytes().rstrip(
        b"\n"
    )
    document = json.loads(raw)
    assert json.dumps(
        document, sort_keys=True, separators=(",", ":")
    ).encode() == raw
    claims = json.dumps(
        document["claims"], sort_keys=True, separators=(",", ":")
    ).encode()
    public_key = bytes.fromhex(
        "66cd608b928b88e50e0efeaa33faf1c43cefe07294b0b87e9fe0aba6a3cf7633"
    )
    assert hashlib.sha256(public_key).hexdigest() == document["signature"]["key_id"]
    Ed25519PublicKey.from_public_bytes(public_key).verify(
        bytes.fromhex(document["signature"]["value"]),
        b"VONK-AGENT-SLOT-MANIFEST-V1\x00" + claims,
    )


def _architecture() -> str:
    return "aarch64" if platform.machine() in {"aarch64", "arm64"} else "x86_64"


def test_builder_output_limit_matches_the_root_supervisor_limit() -> None:
    assert "MAX_ARTIFACT = 256 * 1024 * 1024" in BUILDER.read_text()


def test_builder_reviewed_lock_digest_matches_committed_agent_lock() -> None:
    specification = importlib.util.spec_from_loader(
        "vonk_slot_builder_lock", SourceFileLoader("vonk_slot_builder_lock", str(BUILDER))
    )
    assert specification and specification.loader
    builder = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(builder)

    assert builder.LOCK_SHA256 == hashlib.sha256(
        (ROOT / "agent/uv.lock").read_bytes()
    ).hexdigest()


@pytest.fixture(scope="session")
def slot_wheels(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    distribution = tmp_path_factory.mktemp("slot-distribution")
    for project in (ROOT / "agent", ROOT):
        subprocess.run(
            [
                "uv",
                "build",
                "--project",
                str(project),
                "--wheel",
                "--out-dir",
                str(distribution),
            ],
            check=True,
        )
    return (
        next(distribution.glob("vonk_forge_agent-*.whl")),
        next(distribution.glob("vonk_cluster_profiles-*.whl")),
    )


def test_builder_rejects_missing_inputs_cross_architecture_and_output_symlink(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.whl"
    output = tmp_path / "vonk-forge-agent"
    cross = "aarch64" if _architecture() == "x86_64" else "x86_64"

    for argv in (
        (missing, PROTOCOL, output, _architecture()),
        (PROTOCOL, PROTOCOL, output, cross),
    ):
        result = subprocess.run(
            [
                str(BUILDER),
                "--agent-wheel",
                str(argv[0]),
                "--protocol-wheel",
                str(argv[1]),
                "--platform-wheel",
                str(PROTOCOL),
                "--output",
                str(argv[2]),
                "--architecture",
                argv[3],
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode != 0

    output.symlink_to(tmp_path / "elsewhere")
    result = subprocess.run(
        [
            str(BUILDER),
            "--agent-wheel",
            str(PROTOCOL),
            "--protocol-wheel",
            str(PROTOCOL),
            "--platform-wheel",
            str(PROTOCOL),
            "--output",
            str(output),
            "--architecture",
            _architecture(),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0


def test_builds_one_self_contained_native_elf_with_isolated_module_smoke(
    tmp_path: Path,
    slot_wheels: tuple[Path, Path],
) -> None:
    wheel, platform_wheel = slot_wheels
    artifact = tmp_path / "outside/vonk-forge-agent"
    artifact.parent.mkdir()
    subprocess.run(
        [
            str(BUILDER),
            "--agent-wheel",
            str(wheel),
            "--protocol-wheel",
            str(PROTOCOL),
            "--platform-wheel",
            str(platform_wheel),
            "--output",
            str(artifact),
            "--architecture",
            _architecture(),
        ],
        check=True,
    )

    raw = artifact.read_bytes()[:64]
    assert raw[:7] == b"\x7fELF\x02\x01\x01"
    assert (
        struct.unpack_from("<H", raw, 18)[0]
        == {"x86_64": 62, "aarch64": 183}[_architecture()]
    )
    assert [item.name for item in artifact.parent.iterdir()] == ["vonk-forge-agent"]

    isolated_home = tmp_path / "empty-home"
    isolated_home.mkdir()
    environment = {
        "HOME": str(isolated_home),
        "LANG": "C.UTF-8",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONHOME": "/nonexistent",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": "/nonexistent",
    }
    for arguments, expected in (
        (["--help"], None),
        (["--packaged-module-smoke"], "packaged-agent-modules-ok\n"),
        (["--package-helper", "--help"], None),
    ):
        result = subprocess.run(
            [str(artifact), *arguments],
            cwd=isolated_home,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        if expected is not None:
            assert result.stdout == expected
    assert shutil.which("vonk-forge-agent", path=str(artifact.parent)) == str(artifact)


def test_builder_retries_transient_packaging_failure_and_smokes_before_publish(
    tmp_path: Path,
    slot_wheels: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient PyInstaller crash must never publish an unverified slot."""

    specification = importlib.util.spec_from_loader(
        "vonk_slot_builder", SourceFileLoader("vonk_slot_builder", str(BUILDER))
    )
    assert specification and specification.loader
    builder = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(builder)

    original_run = builder.subprocess.run
    pyinstaller_failures = 0
    runtime_smokes: list[tuple[str, ...]] = []

    def run(command: object, *args: object, **kwargs: object) -> object:
        nonlocal pyinstaller_failures
        if isinstance(command, list) and "PyInstaller" in command:
            if pyinstaller_failures == 0:
                pyinstaller_failures += 1
                raise subprocess.CalledProcessError(-11, command)
        elif isinstance(command, list) and command and str(command[0]).endswith(
            "vonk-forge-agent"
        ):
            runtime_smokes.append(tuple(str(value) for value in command[1:]))
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(builder.subprocess, "run", run)
    wheel, platform_wheel = slot_wheels
    artifact = tmp_path / "vonk-forge-agent"
    builder.build(
        wheel,
        PROTOCOL,
        platform_wheel,
        artifact,
        _architecture(),
    )

    assert pyinstaller_failures == 1
    assert runtime_smokes == [
        ("--help",),
        ("--packaged-module-smoke",),
        ("--package-helper", "--help"),
    ]
    assert artifact.read_bytes().startswith(b"\x7fELF")


def test_builder_snapshots_wheels_and_ignores_hostile_path_network_and_empty_cache(
    tmp_path: Path,
    slot_wheels: tuple[Path, Path],
) -> None:
    wheel, platform_wheel = slot_wheels
    hostile = tmp_path / "hostile"
    hostile.mkdir()
    invoked = tmp_path / "hostile-uv-invoked"
    fake_uv = hostile / "uv"
    fake_uv.write_text(f"#!/bin/sh\ntouch {invoked}\nexit 91\n")
    fake_uv.chmod(0o755)
    output = tmp_path / "vonk-forge-agent"
    environment = {
        **os.environ,
        "PATH": f"{hostile}:/usr/bin:/bin",
        "UV_CACHE_DIR": str(tmp_path / "empty-cache"),
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "VONK_SLOT_SUBSTITUTE_INPUT_TEST": "1",
    }

    built = subprocess.run(
        [
            str(BUILDER),
            "--agent-wheel",
            str(wheel),
            "--protocol-wheel",
            str(PROTOCOL),
            "--platform-wheel",
            str(platform_wheel),
            "--output",
            str(output),
            "--architecture",
            _architecture(),
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )

    assert built.returncode == 0, built.stderr
    assert not invoked.exists()
    assert output.read_bytes().startswith(b"\x7fELF")
