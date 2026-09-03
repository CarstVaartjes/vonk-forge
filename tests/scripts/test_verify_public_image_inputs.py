from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/verify-public-image-inputs"


def run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [SCRIPT, str(root)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_repository_public_image_inputs_are_clean() -> None:
    result = run(ROOT)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "public image inputs: PASS\n"


def test_live_token_pattern_is_rejected_without_echoing_value(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    source = repository / "control/src/vonk_control"
    source.mkdir(parents=True)
    value = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    (source / "leak.py").write_text(f'KEY = "{value}"\n')

    result = run(repository)

    assert result.returncode == 1
    assert "control/src/vonk_control/leak.py: github-token" in result.stderr
    assert value not in result.stderr


def test_private_key_header_is_rejected_without_echoing_content(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    source = repository / "control/src/vonk_control"
    source.mkdir(parents=True)
    value = "-----BEGIN OPENSSH PRIVATE KEY-----"
    (source / "leak.py").write_text(f'KEY = "{value}"\n')

    result = run(repository)

    assert result.returncode == 1
    assert "control/src/vonk_control/leak.py: private-key" in result.stderr
    assert value not in result.stderr


@pytest.mark.parametrize(
    "relative",
    (
        "pyproject.toml",
        "src/cluster_profiles/leak.py",
        "control/web/vite.config.ts",
    ),
)
def test_every_newly_covered_build_input_rejects_injected_secrets(
    tmp_path: Path,
    relative: str,
) -> None:
    repository = tmp_path / "repository"
    source = repository / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    value = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    source.write_text(f'SECRET = "{value}"\n')

    result = run(repository)

    assert result.returncode == 1
    assert f"{relative}: github-token" in result.stderr
    assert value not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "excluded_directory",
    ("node_modules", "dist", "test-results", "playwright-report"),
)
def test_dockerignored_web_outputs_are_not_scanned(
    tmp_path: Path,
    excluded_directory: str,
) -> None:
    repository = tmp_path / "repository"
    source = repository / "control/web" / excluded_directory / "generated.js"
    source.parent.mkdir(parents=True)
    source.write_text('SECRET = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"\n')

    result = run(repository)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "public image inputs: PASS\n"


def wheel_path(repository: Path) -> Path:
    path = repository / "inventory/wheels/vonk_agent_protocol-2.2.0-py3-none-any.whl"
    path.parent.mkdir(parents=True)
    return path


def test_secret_in_compressed_wheel_member_is_rejected(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    value = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    with zipfile.ZipFile(wheel_path(repository), "w", zipfile.ZIP_DEFLATED) as wheel:
        wheel.writestr("vonk_agent_protocol/leak.py", f'SECRET = "{value}"\n')

    result = run(repository)

    assert result.returncode == 1
    assert "inventory/wheels/vonk_agent_protocol-2.2.0-py3-none-any.whl!/" in result.stderr
    assert "vonk_agent_protocol/leak.py: github-token" in result.stderr
    assert value not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("member", "reason"),
    (
        ("../escape.py", "archive-unsafe-path"),
        ("folder\\escape.py", "archive-unsafe-path"),
    ),
)
def test_unsafe_wheel_member_is_rejected(
    tmp_path: Path, member: str, reason: str
) -> None:
    repository = tmp_path / "repository"
    with zipfile.ZipFile(wheel_path(repository), "w") as wheel:
        wheel.writestr(member, "safe = True\n")

    result = run(repository)

    assert result.returncode == 1
    assert reason in result.stderr


def test_malformed_wheel_is_rejected(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    wheel_path(repository).write_bytes(b"not a wheel")

    result = run(repository)

    assert result.returncode == 1
    assert "archive-invalid" in result.stderr
