from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _fake_command(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/bin/sh\nset -eu\n" + body)
    path.chmod(0o755)


def _run_bootstrap(
    tmp_path: Path,
    kind: str,
    *,
    system: str,
    machine: str,
    arguments: tuple[str, ...] = (),
):
    commands = tmp_path / "commands"
    commands.mkdir()
    artifact = tmp_path / "published-installer"
    artifact.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$0|$*\" > \"$VONK_TEST_RECEIPT\"\n"
    )
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    rendered = tmp_path / f"install-{kind}"
    source = (ROOT / "install" / kind).read_text()
    for placeholder in (
        "@NAS_LINUX_AMD64_SHA256@",
        "@NAS_LINUX_ARM64_SHA256@",
        "@NAS_DARWIN_AMD64_SHA256@",
        "@NAS_DARWIN_ARM64_SHA256@",
        "@SPARK_LINUX_AMD64_SHA256@",
        "@SPARK_LINUX_ARM64_SHA256@",
    ):
        source = source.replace(placeholder, digest)
    rendered.write_text(source)
    _fake_command(
        commands,
        "uname",
        f'case "${{1:-}}" in -s) printf "%s\\n" "{system}" ;; -m) printf "%s\\n" "{machine}" ;; esac\n',
    )
    _fake_command(
        commands,
        "curl",
        'destination=\nurl=\nwhile [ "$#" -gt 0 ]; do\n'
        '  case "$1" in -o) destination=$2; shift 2 ;; -*) shift ;; *) url=$1; shift ;; esac\n'
        'done\ncp "$VONK_TEST_ARTIFACT" "$destination"\n',
    )
    forbidden = tmp_path / "forbidden-tools"
    for command in ("sudo", "docker", "git", "ssh"):
        _fake_command(
            commands,
            command,
            'printf "%s\\n" "$0" >> "$VONK_TEST_FORBIDDEN"\nexit 97\n',
        )
    receipt = tmp_path / "receipt"
    environment = {
        **os.environ,
        "PATH": f"{commands}:{os.environ['PATH']}",
        "TMPDIR": str(tmp_path),
        "VONK_INSTALL_BASE_URL": "https://install.example.test/artifacts",
        "VONK_TEST_ARTIFACT": str(artifact),
        "VONK_TEST_RECEIPT": str(receipt),
        "VONK_TEST_FORBIDDEN": str(forbidden),
    }
    result = subprocess.run(
        ["sh", str(rendered), *arguments],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, receipt, forbidden


@pytest.mark.parametrize(
    ("kind", "system", "machine", "arguments", "expected_arguments"),
    (
        ("nas", "Linux", "x86_64", ("--output", "chosen"), "--output chosen"),
        ("nas", "Darwin", "arm64", ("--output", "chosen"), "--output chosen"),
        ("spark", "Linux", "x86_64", (), ""),
        ("spark", "Linux", "aarch64", (), ""),
    ),
)
def test_curl_bootstrap_verifies_and_runs_the_native_installer(
    tmp_path: Path,
    kind: str,
    system: str,
    machine: str,
    arguments: tuple[str, ...],
    expected_arguments: str,
) -> None:
    result, receipt, forbidden = _run_bootstrap(
        tmp_path,
        kind,
        system=system,
        machine=machine,
        arguments=arguments,
    )

    assert result.returncode == 0, result.stderr
    assert receipt.read_text().rstrip().endswith(f"|{expected_arguments}")
    assert not forbidden.exists()


def test_spark_bootstrap_rejects_non_linux_before_downloading(tmp_path: Path) -> None:
    result, receipt, forbidden = _run_bootstrap(
        tmp_path, "spark", system="Darwin", machine="arm64"
    )

    assert result.returncode != 0
    assert "requires Linux" in result.stderr
    assert not receipt.exists()
    assert not forbidden.exists()


def test_bootstrap_never_executes_an_unpinned_download(tmp_path: Path) -> None:
    result, receipt, forbidden = _run_bootstrap(
        tmp_path, "spark", system="Linux", machine="x86_64"
    )
    assert result.returncode == 0
    receipt.unlink()

    rendered = tmp_path / "install-spark"
    digest = hashlib.sha256((tmp_path / "published-installer").read_bytes()).hexdigest()
    rendered.write_text(rendered.read_text().replace(digest, "0" * 64))
    rerun = subprocess.run(
        ["sh", str(rendered)],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{tmp_path / 'commands'}:{os.environ['PATH']}",
            "TMPDIR": str(tmp_path),
            "VONK_INSTALL_BASE_URL": "https://install.example.test/artifacts",
            "VONK_TEST_ARTIFACT": str(tmp_path / "published-installer"),
            "VONK_TEST_RECEIPT": str(receipt),
            "VONK_TEST_FORBIDDEN": str(forbidden),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert rerun.returncode != 0
    assert not receipt.exists()
