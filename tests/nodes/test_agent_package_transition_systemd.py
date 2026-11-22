#!/usr/bin/env python3
"""Promoted package to candidate, through the promoted helper's own socket.

Runs only on a disposable ARM64 systemd machine (a CI runner, never a Spark).
The baseline is the currently promoted release package, resealed with a
throwaway release key so baseline and candidates share one key. Its own helper
runs under its own units and sandbox and receives signed grants on its real
socket; its own rollback watchdog is the only recovery under test.

The candidates come from this tree's package. The fault candidates ship no
preinst, so no forward recovery capsule can arm: the capsule's intent and its
arming breadcrumbs are watched throughout and must never appear. Each fault
holds dpkg in its payload extraction (a pass-through dpkg-deb on dpkg's PATH),
then kills the helper's cgroup, dpkg alone, or every package unit (the reboot
stand-in). A runner cannot reboot inside a job, so that stand-in kills and
stops the package's units, waits past the activation deadline and starts the
units a boot starts; a kernel boot and unsynced writes are not reproduced.
No Controller or GPU acceptance is claimed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NODE = "spk_" + "2" * 32
STATE = Path("/var/lib/vonk-forge")
TRANSACTION = STATE / "package-rollback/transaction.json"
RECEIPT = STATE / "package-activation.receipt.json"
AGENT_DATA = Path("/var/lib/vonk-forge-agent")
CONFIG = Path("/etc/vonk-forge-agent")
AGENT = "vonk-forge-agent.service"
HELPER = "vonk-forge-package-helper.service"
SOCKET = "vonk-forge-package-helper.socket"
WATCHDOG = "vonk-forge-package-rollback.service"
FIREWALL = Path("/run/systemd/system/vonk-forge-docker-firewall.service")
# The helper runs dpkg with /usr/local/sbin first on PATH, and dpkg finds
# dpkg-deb on PATH for the payload extraction. Nothing else resolves it there.
HOLD = Path("/run/vonk-transition-hold")
HOLD_WRAPPER = Path("/usr/local/sbin/dpkg-deb")
# The probe runs as the agent account, which cannot traverse a build workspace.
PROBE = Path("/usr/local/bin/vonk-transition-grant-probe")
# Everything a forward capsule leaves when it arms, including the breadcrumb
# that outlives the capsule's own retirement.
ARMING = (
    STATE / "package-upgrade",
    STATE / "package-upgrade.preflight.trace",
    STATE / "package-upgrade.preflight.status",
    STATE / "package-upgrade.status",
    Path("/lib/systemd/system/vonk-forge-package-upgrade-recover-capsule.service"),
    Path(
        "/lib/systemd/system/vonk-forge-agent.service.d/10-package-upgrade-capsule.conf"
    ),
)


def run(
    *args: str, check: bool = True, input: str | None = None, env=None, timeout=180
):
    result = subprocess.run(
        args,
        text=True,
        input=input,
        capture_output=True,
        env=env,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode:
        raise AssertionError(f"{' '.join(args)} failed: {result.stderr[-2500:]}")
    return result


def sha(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def wait(predicate, message: str, seconds: float = 90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(message)


def offline_install(package: Path) -> None:
    run(
        "/usr/bin/dpkg",
        "--install",
        str(package),
        env=dict(os.environ, SYSTEMD_OFFLINE="1"),
    )


def installed() -> str:
    return run(
        "/usr/bin/dpkg-query",
        "-W",
        "-f=${db:Status-Abbrev}|${Version}",
        "vonk-forge-agent",
        check=False,
    ).stdout


def running_digest(unit: str) -> str | None:
    pid = run(
        "/usr/bin/systemctl", "show", "--property=MainPID", "--value", unit
    ).stdout
    try:
        return sha(Path(f"/proc/{pid.strip()}/exe")) if pid.strip() != "0" else None
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        # The supervised process can restart between the PID and exe reads.
        return None


def receipt(nonce: str, phase: str):
    try:
        value = json.loads(RECEIPT.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    return (
        value if value["attempt_nonce"] == nonce and value["phase"] == phase else None
    )


@dataclass(frozen=True)
class Package:
    path: Path
    sha256: str
    signature: str
    version: str
    agent_sha256: str
    helper_sha256: str

    def source(self) -> dict:
        return {
            "package_sha256": self.sha256,
            "package_signature": self.signature,
            "package_version": self.version,
            "binary_sha256": self.agent_sha256,
            "helper_sha256": self.helper_sha256,
        }

    def runs(self) -> bool:
        return (
            installed() == f"ii |{self.version}"
            and running_digest(AGENT) == self.agent_sha256
        )


def unseal(sealed: Path, output: Path) -> Path:
    return Path(
        run(
            "/usr/bin/python3",
            str(ROOT / "scripts/seal-agent-deb"),
            "--sealed",
            str(sealed),
            "--output-dir",
            str(output),
        ).stdout.strip()
    )


def repack(unsealed: Path, output: Path, edit) -> Path:
    stage = output / "stage"
    run("/usr/bin/dpkg-deb", "-R", str(unsealed), str(stage))
    edit(stage / "DEBIAN")
    package = output / unsealed.name
    # Sealing rebuilds it reproducibly; this intermediate only needs speed.
    run(
        "/usr/bin/dpkg-deb",
        "--build",
        "-Zgzip",
        "-z1",
        "--root-owner-group",
        str(stage),
        str(package),
    )
    shutil.rmtree(stage)
    return package


def seal(unsealed: Path, key: Path, output: Path) -> Package:
    path = Path(
        run(
            "/usr/bin/python3",
            str(ROOT / "scripts/seal-agent-deb"),
            "--unsealed",
            str(unsealed),
            "--release-private-key",
            str(key),
            "--output-dir",
            str(output),
        ).stdout.strip()
    )
    payload = output / "payload"
    run("/usr/bin/dpkg-deb", "--extract", str(path), str(payload))
    package = Package(
        path=path,
        sha256=sha(path),
        signature=Path(f"{path}.host.sig").read_text().strip(),
        version=run(
            "/usr/bin/dpkg-deb", "--field", str(path), "Version"
        ).stdout.strip(),
        agent_sha256=sha(payload / "usr/lib/vonk-forge/vonk-agent"),
        helper_sha256=sha(payload / "usr/lib/vonk-forge/vonk-agent-helper"),
    )
    shutil.rmtree(payload)
    return package


class ArmingWatch:
    """Record any forward-capsule state that appears while the watch runs."""

    def __init__(self) -> None:
        self.seen: set[str] = set()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._poll, daemon=True)

    def _poll(self) -> None:
        while not self._stop.is_set():
            self.seen.update(str(path) for path in ARMING if os.path.lexists(path))
            time.sleep(0.05)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._stop.set()
        self._thread.join()


class Lane:
    def __init__(self, probe: Path, promoted: Package) -> None:
        self.probe = probe
        self.promoted = promoted
        self.evidence: list[dict] = []

    def send(self, operation: dict) -> dict | None:
        command = (
            "/usr/sbin/runuser",
            "-u",
            "vonk-agent",
            "--",
            str(self.probe),
            "send",
            NODE,
        )
        # No reply means the upgrade restarted the helper serving the request.
        reply = run(*command, input=json.dumps(operation)).stdout
        return json.loads(reply) if reply.strip() else None

    def install(self, candidate: Package, deadline: int) -> tuple[str, int, dict]:
        """A signed install of the candidate, with the promoted package as source."""
        nonce = os.urandom(32).hex()
        until = int(time.time()) + deadline
        operation = {
            "type": "install-vonk-deb",
            "package_sha256": candidate.sha256,
            "package_signature": candidate.signature,
            "rollback": {
                "source": self.promoted.source(),
                "attempt_nonce": nonce,
                "activation_deadline": until,
            },
        }
        return nonce, until, operation

    def baseline(self, packages: list[Package]) -> None:
        # A healed fault already runs the promoted package; reinstalling only
        # when it does not keeps each next install a fresh admission over the
        # journal the previous case left.
        if not self.promoted.runs():
            offline_install(self.promoted.path)
            run("/usr/bin/systemctl", "daemon-reload")
            run(
                "/usr/bin/systemctl",
                "reset-failed",
                AGENT,
                HELPER,
                WATCHDOG,
                check=False,
            )
            run("/usr/bin/systemctl", "restart", SOCKET, HELPER, AGENT)
        wait(self.promoted.runs, "promoted package does not run")
        for package in packages:
            target = STATE / "incoming" / f"{package.sha256}.deb"
            shutil.copyfile(package.path, target)
            shutil.chown(target, "vonk-agent", "vonk-agent")
            target.chmod(0o600)

    def activates(self, label: str, candidate: Package, seconds: float) -> None:
        nonce, _, operation = self.install(candidate, deadline=600)
        reply = self.send(operation)
        wait(
            lambda: (
                candidate.runs() and running_digest(HELPER) == candidate.helper_sha256
            ),
            f"{label}: candidate helper and agent did not restart",
            seconds,
        )
        confirmed = self.send(
            {
                "type": "confirm-package-activation",
                "package_sha256": candidate.sha256,
                "attempt_nonce": nonce,
            }
        )
        assert confirmed and confirmed["status"] == "package-activation-confirmed", (
            confirmed
        )
        assert receipt(nonce, "acknowledged"), f"{label}: not acknowledged"
        self.evidence.append(
            {
                "case": label,
                "install_reply": reply and reply.get("status"),
                "version": candidate.version,
                "phase": "acknowledged",
            }
        )

    def restored(self, label: str, nonce: str, **observed) -> None:
        wait(
            lambda: receipt(nonce, "rolled_back"),
            f"{label}: promoted not restored",
            300,
        )
        wait(self.promoted.runs, f"{label}: restored promoted agent does not run")
        assert (
            sha(Path("/usr/lib/vonk-forge/vonk-agent-helper"))
            == self.promoted.helper_sha256
        )
        self.evidence.append(
            {
                "case": label,
                "phase": "rolled_back",
                "version": self.promoted.version,
                **observed,
            }
        )


def held_extraction() -> int | None:
    """The dpkg whose payload extraction the wrapper holds."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes()
            parent = int((entry / "stat").read_text().rsplit(")", 1)[1].split()[1])
        except (FileNotFoundError, ProcessLookupError, IndexError, ValueError):
            continue
        if f"{HOLD_WRAPPER}\0--fsys-tarfile\0".encode() in command:
            return parent
    return None


def prepare_host(probe: Path, promoted: Package) -> None:
    # The firewall has its own namespace acceptance test; the helper only
    # requires its unit to be active.
    FIREWALL.write_text(
        "[Service]\nType=oneshot\nExecStart=/bin/true\nRemainAfterExit=yes\n"
    )
    HOLD_WRAPPER.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = --fsys-tarfile ]; then while [ -e {HOLD} ]; do sleep 0.2; done; fi\n'
        'exec /usr/bin/dpkg-deb "$@"\n'
    )
    HOLD_WRAPPER.chmod(0o755)
    # Earlier lanes on this disposable runner may leave capsule history behind.
    for path in ARMING:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    offline_install(promoted.path)
    temporary = Path(tempfile.mkdtemp(prefix="pki-", dir="/var/tmp"))
    openssl = "/usr/bin/openssl"
    run(
        openssl,
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "1",
        "-subj",
        "/CN=vonk-transition-ca",
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
        "-keyout",
        str(temporary / "ca-key.pem"),
        "-out",
        str(temporary / "ca.pem"),
    )
    run(openssl, "genpkey", "-algorithm", "ED25519", "-out", str(temporary / "key.pem"))
    run(
        openssl,
        "req",
        "-new",
        "-key",
        str(temporary / "key.pem"),
        "-subj",
        f"/CN={NODE}",
        "-out",
        str(temporary / "identity.csr"),
    )
    (temporary / "ext.conf").write_text(
        "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n"
        f"extendedKeyUsage=clientAuth\nsubjectAltName=URI:spiffe://vonkforge.ai/agent/{NODE}\n"
    )
    run(
        openssl,
        "x509",
        "-req",
        "-days",
        "1",
        "-in",
        str(temporary / "identity.csr"),
        "-CA",
        str(temporary / "ca.pem"),
        "-CAkey",
        str(temporary / "ca-key.pem"),
        "-CAcreateserial",
        "-extfile",
        str(temporary / "ext.conf"),
        "-out",
        str(temporary / "certificate.pem"),
    )
    run(
        openssl,
        "x509",
        "-in",
        str(temporary / "ca.pem"),
        "-outform",
        "DER",
        "-out",
        str(temporary / "ca.der"),
    )
    credentials = AGENT_DATA / "credentials"
    credentials.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, source in (
        ("private-key.pem", "key.pem"),
        ("certificate.pem", "certificate.pem"),
        ("chain.pem", "ca.pem"),
    ):
        shutil.copyfile(temporary / source, credentials / name)
        (credentials / name).chmod(0o600)
    for path in (AGENT_DATA, credentials, *credentials.iterdir()):
        shutil.chown(path, "vonk-agent", "vonk-agent")
    shutil.copyfile(temporary / "ca.pem", CONFIG / "controller-ca.pem")
    (CONFIG / "agent.toml").write_text(
        'enrollment_url = "https://127.0.0.1:9/"\n'
        'controller_url = "https://127.0.0.1:9/"\n'
        f'ca_path = "{CONFIG / "controller-ca.pem"}"\n'
        f'ca_sha256 = "{sha(temporary / "ca.der")}"\n'
        f'data_dir = "{AGENT_DATA}"\n'
        f'node_id = "{NODE}"\n'
    )
    shutil.rmtree(temporary)
    # The grant key is the one root key file this lane adds; the release key
    # is the resealed package's own keyring.
    (CONFIG / "host-helper-authority.pub").write_text(
        run(str(probe), "public-key").stdout.strip()
    )
    for name in ("agent.toml", "controller-ca.pem", "host-helper-authority.pub"):
        (CONFIG / name).chmod(0o644)
    run("/usr/bin/systemctl", "daemon-reload")
    # A boot restores through this enablement; postinst established it.
    assert run("/usr/bin/systemctl", "is-enabled", WATCHDOG).stdout.strip() == "enabled"
    run("/usr/bin/systemctl", "reset-failed", AGENT, HELPER, WATCHDOG, check=False)
    run("/usr/bin/systemctl", "restart", SOCKET, HELPER, AGENT)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--disposable-systemd", action="store_true", required=True)
    parser.add_argument("--promoted", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--probe", type=Path, required=True)
    args = parser.parse_args()
    assert os.geteuid() == 0 and Path("/run/systemd/system").is_dir()
    assert run("/usr/bin/dpkg", "--print-architecture").stdout.strip() == "arm64"
    work = Path(tempfile.mkdtemp(prefix="vonk-transition-", dir="/var/tmp"))
    try:
        key = work / "release.pem"
        run("/usr/bin/openssl", "genpkey", "-algorithm", "ED25519", "-out", str(key))
        variants = {
            name: work / name for name in ("promoted", "tree", "no-preinst", "failing")
        }
        for directory in variants.values():
            directory.mkdir()
        promoted = seal(
            unseal(args.promoted, variants["promoted"]), key, variants["promoted"]
        )
        tree_unsealed = unseal(args.candidate, variants["tree"])
        tree = seal(tree_unsealed, key, variants["tree"])

        def drop_preinst(debian: Path) -> None:
            (debian / "preinst").unlink()

        def failing_postinst(debian: Path) -> None:
            drop_preinst(debian)
            postinst = debian / "postinst"
            shebang, rest = postinst.read_text().split("\n", 1)
            postinst.write_text(f"{shebang}\nexit 44\n{rest}")

        no_preinst = seal(
            repack(tree_unsealed, variants["no-preinst"], drop_preinst),
            key,
            variants["no-preinst"],
        )
        failing = seal(
            repack(tree_unsealed, variants["failing"], failing_postinst),
            key,
            variants["failing"],
        )
        packages = [promoted, tree, no_preinst, failing]
        shutil.copyfile(args.probe, PROBE)
        PROBE.chmod(0o755)
        prepare_host(PROBE, promoted)
        lane = Lane(PROBE, promoted)

        with ArmingWatch() as watch:
            lane.baseline(packages)
            lane.activates("no-preinst-finisher-and-acknowledgement", no_preinst, 120)

            lane.baseline(packages)
            nonce, _, operation = lane.install(failing, deadline=600)
            reply = lane.send(operation)
            assert reply is None or reply.get("status") != "package-installed", reply
            lane.restored("failed-postinst", nonce)

            for fault in ("helper-cgroup-killed", "dpkg-killed", "reboot"):
                lane.baseline(packages)
                HOLD.touch()
                nonce, deadline, operation = lane.install(no_preinst, deadline=30)
                client = threading.Thread(target=lane.send, args=(operation,))
                client.start()
                dpkg = wait(held_extraction, f"{fault}: extraction not held", 25)
                assert time.time() < deadline, f"{fault}: held only after the deadline"
                held_state = installed()
                if fault == "helper-cgroup-killed":
                    run(
                        "/usr/bin/systemctl",
                        "kill",
                        "--kill-whom=all",
                        "--signal=SIGKILL",
                        HELPER,
                    )
                elif fault == "dpkg-killed":
                    os.kill(dpkg, signal.SIGKILL)
                else:
                    units = (HELPER, AGENT, "vonk-forge-monitor.service", WATCHDOG)
                    for unit in units:
                        run(
                            "/usr/bin/systemctl",
                            "kill",
                            "--kill-whom=all",
                            "--signal=SIGKILL",
                            unit,
                            check=False,
                        )
                    run("/usr/bin/systemctl", "stop", SOCKET, *units, check=False)
                    run("/usr/bin/systemctl", "reset-failed", *units, check=False)
                HOLD.unlink()
                client.join(timeout=60)
                if fault == "reboot":
                    run("/usr/bin/systemctl", "daemon-reload")
                    time.sleep(max(0.0, deadline - time.time() + 2))
                    enabled = run(
                        "/usr/bin/systemctl",
                        "list-unit-files",
                        "--state=enabled",
                        "--no-legend",
                        "vonk-forge-*",
                    ).stdout.split()
                    boot = [name for name in enabled if name.startswith("vonk-forge-")]
                    assert WATCHDOG in boot, boot
                    run("/usr/bin/systemctl", "start", "--no-block", *boot)
                lane.restored(
                    f"{fault}-mid-unpack",
                    nonce,
                    held_state=held_state,
                    killed_pid=dpkg if fault == "dpkg-killed" else None,
                )
            assert not watch.seen, f"a forward capsule armed: {sorted(watch.seen)}"

        # Today's production path: the unmodified candidate arms its capsule.
        lane.baseline(packages)
        lane.activates("tree-candidate-with-preinst", tree, 300)
        trace = STATE / "package-upgrade.preflight.trace"
        lane.evidence[-1]["capsule_preflight"] = (
            trace.read_text().splitlines()[1] if trace.is_file() else None
        )
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "lane": "disposable-arm64-systemd",
                    "promoted_version": promoted.version,
                    "cases": lane.evidence,
                },
                indent=2,
            )
        )
    finally:
        HOLD.unlink(missing_ok=True)
        HOLD_WRAPPER.unlink(missing_ok=True)
        PROBE.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(
            run(
                "/usr/bin/journalctl",
                "--no-pager",
                "-n",
                "150",
                "-u",
                WATCHDOG,
                "-u",
                HELPER,
                "-u",
                AGENT,
                "-u",
                "vonk-forge-package-helper-upgrade-finish.service",
                "-u",
                "vonk-forge-package-upgrade-recover-capsule.service",
                check=False,
            ).stdout
        )
        raise
