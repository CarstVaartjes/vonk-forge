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
        "#!/bin/sh\n"
        "printf '%s\\n' \"$0|$*\" > \"$VONK_TEST_RECEIPT\"\n"
        "if [ \"${1:-}\" = --template ]; then printf 'payload=%s\\n' \"$(cat \"$2\")\" >> \"$VONK_TEST_RECEIPT\"; fi\n"
    )
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    package = tmp_path / "vonk-forge-agent.deb"
    package.write_bytes(b"exact published Debian package\n")
    package_digest = hashlib.sha256(package.read_bytes()).hexdigest()
    payload = tmp_path / "published-nas-payload.json"
    payload.write_text('{"schema_version":1}\n')
    payload_digest = hashlib.sha256(payload.read_bytes()).hexdigest()
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
    for placeholder in (
        "@SPARK_LINUX_AMD64_PACKAGE_SHA256@",
        "@SPARK_LINUX_ARM64_PACKAGE_SHA256@",
    ):
        source = source.replace(placeholder, package_digest)
    source = source.replace("@NAS_PAYLOAD_SHA256@", payload_digest)
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
        'done\ncase "$url" in */payload.json) source=$VONK_TEST_PAYLOAD ;; *.deb) source=$VONK_TEST_PACKAGE ;; *) source=$VONK_TEST_ARTIFACT ;; esac\n'
        'cp "$source" "$destination"\n',
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
        "VONK_TEST_PACKAGE": str(package),
        "VONK_TEST_PAYLOAD": str(payload),
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
        ("spark", "Linux", "x86_64", (), "--package"),
        ("spark", "Linux", "aarch64", (), "--package"),
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
    invocation = receipt.read_text().rstrip().split("|", 1)[1].split()
    assert invocation[0] == expected_arguments
    if kind == "spark":
        assert invocation[2] == "--package-sha256"
        assert invocation[3] == hashlib.sha256(
            (tmp_path / "vonk-forge-agent.deb").read_bytes()
        ).hexdigest()
    assert not forbidden.exists()


@pytest.mark.parametrize(
    ("system", "machine"),
    (("Linux", "x86_64"), ("Darwin", "arm64")),
)
def test_nas_bootstrap_needs_no_arguments_and_supplies_verified_payload(
    tmp_path: Path, system: str, machine: str
) -> None:
    result, receipt, forbidden = _run_bootstrap(
        tmp_path, "nas", system=system, machine=machine
    )

    assert result.returncode == 0, result.stderr
    lines = receipt.read_text().splitlines()
    invocation = lines[0].split("|", 1)[1].split()
    assert invocation[0] == "--template"
    assert lines[1] == 'payload={"schema_version":1}'
    assert invocation[2:] == []
    assert not forbidden.exists()


def test_nas_bootstrap_reuses_the_same_command_for_upgrade(tmp_path: Path) -> None:
    (tmp_path / "vonk-forge").mkdir()

    result, receipt, forbidden = _run_bootstrap(
        tmp_path, "nas", system="Linux", machine="x86_64"
    )

    assert result.returncode == 0, result.stderr
    invocation = receipt.read_text().splitlines()[0].split("|", 1)[1].split()
    assert invocation[0] == "--template"
    assert invocation[2:] == ["--upgrade"]
    assert not forbidden.exists()


def test_spark_bootstrap_rejects_non_linux_before_downloading(tmp_path: Path) -> None:
    result, receipt, forbidden = _run_bootstrap(
        tmp_path, "spark", system="Darwin", machine="arm64"
    )

    assert result.returncode != 0
    assert "requires Linux" in result.stderr
    assert not receipt.exists()
    assert not forbidden.exists()


def test_spark_bootstrap_rejects_user_arguments(tmp_path: Path) -> None:
    result, receipt, forbidden = _run_bootstrap(
        tmp_path,
        "spark",
        system="Linux",
        machine="x86_64",
        arguments=("--package", "/tmp/untrusted.deb"),
    )

    assert result.returncode != 0
    assert "does not accept arguments" in result.stderr
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
            "VONK_TEST_PACKAGE": str(tmp_path / "vonk-forge-agent.deb"),
            "VONK_TEST_RECEIPT": str(receipt),
            "VONK_TEST_FORBIDDEN": str(forbidden),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert rerun.returncode != 0
    assert not receipt.exists()
