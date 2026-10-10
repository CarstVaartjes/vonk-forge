"""CI package acquisition cannot bypass bounded mirror recovery."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRECT_APT = re.compile(r"\bapt-get\b[^\n;&|]*\b(?:update|install)\b")


def direct_apt_calls(root: Path) -> list[str]:
    problems = []
    paths = [*root.glob(".github/**/*.yml"), *root.glob(".github/**/*.yaml")]
    # Include shell entry points invoked by workflows/actions, and their callees.
    # Scanning all shell scripts prevents a new indirection from evading the guard.
    for directory in ("scripts", "tests", "tools", "install", "packaging", "deploy"):
        for path in (root / directory).rglob("*"):
            if (
                path.is_file()
                and (path.suffix == ".sh" or not path.suffix)
                and path.read_bytes().startswith(b"#!/")
            ):
                paths.append(path)
    for path in paths:
        if path == root / "scripts/ci-apt-install":
            continue
        for number, line in enumerate(
            path.read_text().replace("\\\n", " ").splitlines(), 1
        ):
            if line.lstrip().startswith(("#", "description:")):
                continue
            if DIRECT_APT.search(line):
                problems.append(f"{path.relative_to(root)}:{number}")
    return problems


def test_ci_apt_calls_use_shared_recovery() -> None:
    assert direct_apt_calls(ROOT) == []


def test_guard_catches_composite_and_indirect_bypasses(tmp_path: Path) -> None:
    for name, command in (
        (".github/workflows/example.yml", "timeout 300 sudo apt-get update"),
        (".github/actions/example/action.yml", "sudo apt-get -y install curl"),
        ("scripts/nested.sh", "#!/bin/bash\napt-get \\\n  install curl"),
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(command)
    assert len(direct_apt_calls(tmp_path)) == 3


# Exercise the real shell retry loop and source rewrite, replacing only host
# commands so fault injection never touches the developer's apt configuration.
def test_mirror_recovery_is_bounded_and_a_fresh_request_can_succeed(
    tmp_path: Path,
) -> None:
    import os
    import subprocess

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sources = tmp_path / "apt"
    (sources / "sources.list.d").mkdir(parents=True)
    legacy = sources / "sources.list"
    deb822 = sources / "sources.list.d/ubuntu.sources"
    vendor = sources / "sources.list.d/vendor.list"
    vendor.write_text("deb https://vendor.example/ubuntu stable main\n")
    log = tmp_path / "commands"
    mode = tmp_path / "mode"
    count = tmp_path / "count"
    executables = {
        "sudo": '#!/bin/bash\nshift\nexec "$@"\n',
        "timeout": '#!/bin/bash\necho "timeout $*" >> "$LOG"\nshift 2\nexec "$@"\n',
        "sleep": '#!/bin/bash\necho "sleep $*" >> "$LOG"\n',
        "dpkg": '#!/bin/bash\nif [[ "$1" == --print-architecture ]]; then echo "$ARCH"; fi\n',
        "apt-get": """#!/bin/bash
n=$(cat "$COUNT" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$COUNT"
mode=$(cat "$MODE")
case "$mode:$n:$*" in
  exhausted:*|update-timeout:1:*) exit 124 ;;
  update-error:1:*|install-error:2:*) exit 100 ;;
esac
""",
    }
    for name, content in executables.items():
        path = bin_dir / name
        path.write_text(content)
        path.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "LOG": str(log),
        "MODE": str(mode),
        "COUNT": str(count),
        "CI_APT_SOURCES_DIR": str(sources),
    }
    for architecture in ("amd64", "arm64"):
        env["ARCH"] = architecture
        mirror = (
            "https://ports.ubuntu.com/ubuntu-ports"
            if architecture == "arm64"
            else "https://archive.ubuntu.com/ubuntu"
        )
        for failure in (
            "success",
            "update-timeout",
            "update-error",
            "install-error",
            "exhausted",
        ):
            legacy.write_text("deb http://azure.archive.ubuntu.com/ubuntu noble main\n")
            deb822.write_text(
                "Types: deb\nURIs: http://ports.ubuntu.com/ubuntu-ports\n"
                "Suites: noble noble-updates\nComponents: main\nSigned-By: /keyring.gpg\n"
            )
            mode.write_text(failure)
            count.write_text("0")
            log.write_text("")
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/ci-apt-install"), "curl"],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert result.returncode == (1 if failure == "exhausted" else 0), (
                result.stderr
            )
            if failure == "success":
                assert "azure.archive" in legacy.read_text()
                assert "sleep" not in log.read_text()
            else:
                assert mirror in legacy.read_text()
                assert mirror in deb822.read_text()
                assert "Signed-By: /keyring.gpg" in deb822.read_text()
                assert "Suites: noble noble-updates" in deb822.read_text()
                assert "sleep 2" in log.read_text()
            assert (
                vendor.read_text() == "deb https://vendor.example/ubuntu stable main\n"
            )
            assert "Acquire::Retries=2" in log.read_text()
            assert "APT::Update::Error-Mode=any" in log.read_text()
            if failure == "exhausted":
                assert count.read_text().strip() == "3"
                assert "sleep 4" in log.read_text()
                mode.write_text("success")
                fresh = subprocess.run(
                    ["bash", str(ROOT / "scripts/ci-apt-install"), "curl"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                assert fresh.returncode == 0, fresh.stderr
