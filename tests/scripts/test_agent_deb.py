from __future__ import annotations

import hashlib
import os
import stat
import struct
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUILD = ROOT / "scripts/build-agent-deb"
VERIFY = ROOT / "scripts/verify-agent-deb"
PREINST = ROOT / "packaging/debian/preinst"
POSTINST = ROOT / "packaging/debian/postinst"
PRERM = ROOT / "packaging/debian/prerm"
DOCKER_FIREWALL = ROOT / "packaging/bin/vonk-forge-docker-firewall"


def _aarch64_fixture(path: Path, marker: bytes) -> None:
    raw = bytearray(256)
    raw[:16] = b"\x7fELF\x02\x01\x01" + bytes(9)
    struct.pack_into("<H", raw, 16, 2)
    struct.pack_into("<H", raw, 18, 183)
    raw[64 : 64 + len(marker)] = marker
    path.write_bytes(raw)
    path.chmod(0o555)


def _release_key(path: Path) -> None:
    result = subprocess.run(
        ["/usr/bin/openssl", "genpkey", "-algorithm", "ED25519", "-out", path],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    path.chmod(0o600)


def _build(
    output: Path,
    binaries: Path,
    key: Path,
    version: str = "0.1.0",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            BUILD,
            "--version",
            version,
            "--release-private-key",
            key,
            "--binaries-dir",
            binaries,
            "--source-date-epoch",
            "1786060800",
            "--output-dir",
            output,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_preinst(tmp_path: Path, status: str) -> subprocess.CompletedProcess[str]:
    test_root = tmp_path / status
    state = test_root / "var/lib/vonk-forge/supervisor/state.json"
    state.parent.mkdir(parents=True)
    state.write_text(f'{{"status":"{status}"}}')
    state.chmod(0o644)
    script = tmp_path / f"preinst-{status}"
    script.write_text(PREINST.read_text().replace("@VERSION@", "0.1.1"))
    script.chmod(0o755)
    return subprocess.run(
        [script, "upgrade", "0.1.0"],
        env={
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "VONK_PACKAGE_TEST_ROOT": str(test_root),
        },
        capture_output=True,
        text=True,
        check=False,
    )


def _firewall_config(
    tmp_path: Path, replacement: tuple[str, str] | None = None
) -> Path:
    text = """VONK_NAS_MANAGEMENT_IP=192.168.1.231
VONK_NODE_MANAGEMENT_IP=192.168.1.211
VONK_NODE_FABRIC_IP=192.168.100.10
VONK_PEER_FABRIC_IP=192.168.100.11
VONK_ENDPOINT_HOST_PORTS=8000,8101
VONK_HOST_ENDPOINT_PORTS=8888
VONK_RENDEZVOUS_PORT=29500"""
    if replacement is not None:
        old, new = replacement
        text = text.replace(old, new)
    path = tmp_path / "docker-firewall.conf"
    path.write_text(text + "\n")
    path.chmod(0o600)
    return path


def _allocate_subid_range(
    tmp_path: Path,
    entries: str,
    *,
    minimum: int = 100_000,
    maximum: int = 600_100_000,
    count: int = 65_536,
) -> subprocess.CompletedProcess[str]:
    source = POSTINST.read_text()
    functions = source[
        source.index("login_value() {") : source.index("sub_uid_min=")
    ]
    script = tmp_path / "allocate-subid-range"
    script.write_text(
        "#!/bin/sh\nset -eu\n"
        + functions
        + '\nallocate_subid_range "$1" "$2" "$3" "$4"\n'
    )
    script.chmod(0o755)
    ranges = tmp_path / "subuid"
    ranges.write_text(entries)
    return subprocess.run(
        [script, ranges, str(minimum), str(maximum), str(count)],
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )


def test_preinst_refuses_to_unpack_over_nonstable_supervisor_state(
    tmp_path: Path,
) -> None:
    stable = _run_preinst(tmp_path, "stable")
    pending = _run_preinst(tmp_path, "pending")
    failed = _run_preinst(tmp_path, "failed")

    assert stable.returncode == 0, stable.stderr
    assert pending.returncode != 0
    assert failed.returncode != 0
    assert "supervisor state is not stable" in pending.stderr
    assert "supervisor state is not stable" in failed.stderr


@pytest.mark.parametrize(
    ("entries", "expected"),
    (
        ("", "100000-165535"),
        ("carst:100000:65536\n", "165536-231071"),
        ("first:165536:65536\nlast:100000:65536\n", "231072-296607"),
        ("first:100000:32768\nsecond:198304:65536\n", "132768-198303"),
    ),
)
def test_postinst_allocates_first_nonoverlapping_subid_range(
    tmp_path: Path, entries: str, expected: str
) -> None:
    result = _allocate_subid_range(tmp_path, entries)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_postinst_rejects_exhausted_subid_space(tmp_path: Path) -> None:
    result = _allocate_subid_range(
        tmp_path,
        "first:100000:65536\nsecond:165536:65536\n",
        maximum=231071,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_package_manages_the_rootless_podman_user_manager() -> None:
    postinst = POSTINST.read_text()
    prerm = PRERM.read_text()

    assert "/usr/bin/loginctl enable-linger vonk-agent" in postinst
    assert "/usr/bin/loginctl disable-linger vonk-agent" in prerm


def test_builder_produces_reproducible_verified_arm64_deb(tmp_path: Path) -> None:
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in ("vonk-agent", "vonk-agent-helper", "vonk-agent-supervisor"):
        _aarch64_fixture(binaries / name, name.encode())
    key = tmp_path / "release.pem"
    _release_key(key)
    first = tmp_path / "first"
    second = tmp_path / "second"

    first_result = _build(first, binaries, key)
    second_result = _build(second, binaries, key)

    assert first_result.returncode == 0, first_result.stderr
    assert second_result.returncode == 0, second_result.stderr
    package_name = "vonk-forge-agent_0.1.0_arm64.deb"
    first_deb = first / package_name
    second_deb = second / package_name
    assert first_deb.read_bytes() == second_deb.read_bytes()
    assert stat.S_IMODE(first_deb.stat().st_mode) == 0o644
    sidecar = (first / f"{package_name}.sha256").read_text().strip()
    assert (
        sidecar
        == f"{hashlib.sha256(first_deb.read_bytes()).hexdigest()}  {package_name}"
    )

    verified = subprocess.run(
        [VERIFY, "--json", first_deb],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr or verified.stdout
    assert '"ok": true' in verified.stdout

    control = tmp_path / "control"
    extracted = subprocess.run(
        ["/usr/bin/dpkg-deb", "--control", first_deb, control],
        capture_output=True,
        text=True,
        check=False,
    )
    assert extracted.returncode == 0, extracted.stderr
    postinst = (control / "postinst").read_text()
    preinst = (control / "preinst").read_text()
    assert "python" not in postinst
    assert "curl" not in postinst
    assert "wget" not in postinst
    assert "new_version='0.1.0'" in preinst
    assert os.access(control / "preinst", os.X_OK)
    fields = subprocess.run(
        ["/usr/bin/dpkg-deb", "--field", first_deb, "Depends"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "curl" in fields
    assert "podman" in fields
    assert "util-linux" in fields
    assert "uidmap" in fields
    assert "iptables" in fields
    assert "iproute2" in fields
    payload = tmp_path / "payload"
    subprocess.run(
        ["/usr/bin/dpkg-deb", "--extract", first_deb, payload], check=True
    )
    assert (payload / "etc/vonk-forge-agent/containers-storage.conf").is_file()
    unit = (payload / "lib/systemd/system/vonk-forge-agent.service").read_text()
    assert "Environment=HOME=/var/lib/vonk-forge-agent" in unit
    assert "Environment=XDG_RUNTIME_DIR=/run/vonk-forge-agent" in unit
    assert "ProtectControlGroups=yes" in unit
    assert "ProtectHome=no" in unit
    assert "InaccessiblePaths=/home /root -/run/docker.sock" in unit
    assert "BindReadOnlyPaths=/run/user" not in unit
    assert (
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK" in unit
    )
    assert "RestrictNamespaces=user mnt pid ipc uts cgroup net" in unit
    assert "ProtectProc=default" in unit
    assert "ProcSubset=all" in unit
    assert "DeviceAllow=/dev/fuse rw" in unit
    assert "DeviceAllow=char-231:* rw" in unit
    assert "BindPaths=-/dev/fuse" in unit
    assert "Delegate=yes" in unit
    assert "RestrictSUIDSGID=yes" not in unit
    assert "NoNewPrivileges=no" in unit
    assert (
        "CapabilityBoundingSet=CAP_DAC_OVERRIDE CAP_SETGID CAP_SETUID CAP_SYS_ADMIN"
        in unit
    )
    helper_socket = (
        payload / "lib/systemd/system/vonk-forge-package-helper.socket"
    ).read_text()
    assert (
        "ListenStream=/run/vonk-forge-package-helper/package-helper.sock"
        in helper_socket.splitlines()
    )
    assert "DirectoryMode=0711" in helper_socket.splitlines()
    firewall = payload / "usr/lib/vonk-forge/vonk-forge-docker-firewall"
    assert firewall.is_file()
    assert os.access(firewall, os.X_OK)
    firewall_unit = (
        payload / "lib/systemd/system/vonk-forge-docker-firewall.service"
    ).read_text()
    assert "Requires=docker.service" in firewall_unit
    assert "PartOf=docker.service" in firewall_unit
    assert "Before=vonk-forge-package-helper.service" in firewall_unit
    assert "CapabilityBoundingSet=CAP_NET_ADMIN" in firewall_unit
    assert "RestrictAddressFamilies=AF_UNIX AF_NETLINK" in firewall_unit
    assert "PrivateNetwork=yes" not in firewall_unit
    helper_unit = (
        payload / "lib/systemd/system/vonk-forge-package-helper.service"
    ).read_text()
    assert "Requires=vonk-forge-docker-firewall.service" in helper_unit
    assert (
        "After=" in helper_unit and "vonk-forge-docker-firewall.service" in helper_unit
    )
    assert "usermod --add-subuids" in postinst
    assert "usermod --add-subgids" in postinst
    assert "SUB_UID_MIN" in postinst
    assert "SUB_GID_MIN" in postinst
    assert "sed -i '/^vonk-agent:/d' /etc/subuid" not in postinst
    assert "sed -i '/^vonk-agent:/d' /etc/subgid" not in postinst
    assert (
        "install -d -o root -g vonk-agent -m 0750 /var/lib/vonk-forge/supervisor"
    ) in postinst
    assert 'ignore_chown_errors' not in (
        payload / "etc/vonk-forge-agent/containers-storage.conf"
    ).read_text()


def test_docker_firewall_rejects_missing_site_configuration(tmp_path: Path) -> None:
    result = subprocess.run(
        [DOCKER_FIREWALL, "--config", tmp_path / "missing.conf", "check"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "site configuration" in result.stderr


def test_docker_firewall_requires_explicit_host_endpoint_authority(
    tmp_path: Path,
) -> None:
    config = _firewall_config(tmp_path)

    accepted = subprocess.run(
        [DOCKER_FIREWALL, "--config", config, "check-host-port", "8888"],
        capture_output=True,
        text=True,
        check=False,
    )
    rejected = subprocess.run(
        [DOCKER_FIREWALL, "--config", config, "check-host-port", "9999"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert accepted.returncode != 64
    assert rejected.returncode != 0
    assert "not authorized" in rejected.stderr


@pytest.mark.parametrize(
    ("old", "new", "message"),
    (
        ("192.168.1.211", "192.168.001.211", "invalid IPv4"),
        ("8000,8101", "8000,8000", "must be unique"),
        ("VONK_RENDEZVOUS_PORT=29500", "VONK_RENDEZVOUS_PORT=8000", "must differ"),
        (
            "VONK_ENDPOINT_HOST_PORTS=8000,8101",
            "VONK_UNKNOWN=1",
            "unknown or malformed",
        ),
    ),
)
def test_docker_firewall_rejects_noncanonical_site_policy(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    config = _firewall_config(tmp_path, (old, new))

    result = subprocess.run(
        [DOCKER_FIREWALL, "--config", config, "check"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert message in result.stderr


def test_verifier_rejects_tampered_release_sidecar(tmp_path: Path) -> None:
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in ("vonk-agent", "vonk-agent-helper", "vonk-agent-supervisor"):
        _aarch64_fixture(binaries / name, name.encode())
    key = tmp_path / "release.pem"
    _release_key(key)
    output = tmp_path / "dist"
    result = _build(output, binaries, key)
    assert result.returncode == 0, result.stderr
    deb = output / "vonk-forge-agent_0.1.0_arm64.deb"
    (output / f"{deb.name}.sha256").write_text(f"{'0' * 64}  {deb.name}\n")

    verified = subprocess.run(
        [VERIFY, "--json", deb],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert verified.returncode == 1
    assert "sidecar is invalid" in verified.stdout


def test_builder_accepts_cargo_hardlinked_release_binary(tmp_path: Path) -> None:
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in ("vonk-agent", "vonk-agent-helper", "vonk-agent-supervisor"):
        _aarch64_fixture(binaries / name, name.encode())
    # Cargo can materialize release outputs as hardlinks on the runner's
    # filesystem. The builder must bind and verify the inode, not reject a
    # valid native build because its link count is greater than one.
    alias = tmp_path / "cargo-artifact-cache.bin"
    os.link(binaries / "vonk-agent", alias)
    key = tmp_path / "release.pem"
    _release_key(key)

    result = _build(tmp_path / "dist", binaries, key)

    assert result.returncode == 0, result.stderr


def test_builder_rejects_symlinked_release_key(tmp_path: Path) -> None:
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in ("vonk-agent", "vonk-agent-helper", "vonk-agent-supervisor"):
        _aarch64_fixture(binaries / name, name.encode())
    key = tmp_path / "release.pem"
    _release_key(key)
    linked_key = tmp_path / "linked-release.pem"
    linked_key.symlink_to(key)

    result = _build(tmp_path / "dist", binaries, linked_key)

    assert result.returncode == 2
    assert "private key permissions are unsafe" in result.stderr


def test_builder_accepts_exact_derived_development_version(tmp_path: Path) -> None:
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in ("vonk-agent", "vonk-agent-helper", "vonk-agent-supervisor"):
        _aarch64_fixture(binaries / name, name.encode())
    key = tmp_path / "release.pem"
    _release_key(key)
    version = "0.1.0~dev.1786300000+g0123456789ab"

    result = _build(tmp_path / "dist", binaries, key, version)

    assert result.returncode == 0, result.stderr
    package = tmp_path / "dist" / f"vonk-forge-agent_{version}_arm64.deb"
    assert package.is_file()
    verified = subprocess.run(
        [VERIFY, "--json", package],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert verified.returncode == 0, verified.stderr


@pytest.mark.parametrize(
    "version",
    (
        "0.1.0-rc.1",
        "0.1.0+build.1",
        "0.1.0~devX1786300000+g0123456789ab",
        "0.1.0~dev.1786300000+g0123456789abc",
        "0.1.0~dev.1786300000+g0123456789AB",
        "0.1.0~dev.4102444801+g0123456789ab",
    ),
)
def test_builder_rejects_noncanonical_package_versions(
    tmp_path: Path, version: str
) -> None:
    binaries = tmp_path / "binaries"
    binaries.mkdir()
    for name in ("vonk-agent", "vonk-agent-helper", "vonk-agent-supervisor"):
        _aarch64_fixture(binaries / name, name.encode())
    key = tmp_path / "release.pem"
    _release_key(key)

    result = _build(tmp_path / "dist", binaries, key, version)

    assert result.returncode == 2
    assert "version is not canonical package version" in result.stderr
