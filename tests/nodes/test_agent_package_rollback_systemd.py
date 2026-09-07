#!/usr/bin/env python3
"""Actual signed DEB/systemd rollback acceptance in a disposable ARM64 machine.

Both package generations use the current helper and current preinst contract.
The tiny agent executable and postinst are explicit fault-injection fixtures;
full production maintainer-script recovery remains in the adjacent shell lanes.
Never run on a deployed Spark. No Controller or physical GPU acceptance is claimed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = Path("/var/lib/vonk-forge/package-rollback")
RECEIPT = STATE.parent / "package-activation.receipt.json"
NODE = "spk_" + "1" * 32
AGENT = Path("/usr/lib/vonk-forge/vonk-agent")
HELPER = AGENT.with_name("vonk-agent-helper")
UNIT = "vonk-forge-package-rollback.service"


def run(
    *args: str, check: bool = True, input: str | None = None, env: dict | None = None
):
    result = subprocess.run(
        args,
        text=True,
        input=input,
        capture_output=True,
        check=False,
        env=env,
    )
    if check and result.returncode:
        raise AssertionError(f"{args[0]} failed: {result.stderr[-2500:]}")
    return result


def sha(path: Path) -> str:
    return hashlib.file_digest(path.open("rb"), "sha256").hexdigest()


def write(path: Path, data: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)
    path.chmod(mode)


def wait(predicate, message: str, seconds: int = 60):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(message)


def receipt_phase(phase: str):
    try:
        value = json.loads(RECEIPT.read_text())
        return value if value["phase"] == phase else None
    except FileNotFoundError:
        return None


def build(work: Path, helper: Path, generation: int, fault: str = "") -> dict:
    stage = work / f"stage-{generation}"
    version = f"0.1.1~acceptance.{generation}"
    agent = stage / "usr/lib/vonk-forge/vonk-agent"
    agent.parent.mkdir(parents=True)
    code = work / f"agent-{generation}.c"
    code.write_text(f"""#include <stdio.h>\n#include <string.h>\n#include <unistd.h>\nint main(int argc,char **argv) {{
    if (argc==2 && !strcmp(argv[1],"--version")) {{puts("vonk-agent {version}");return 0;}}
    for (;;) pause();
    }}\n""")
    run("/usr/bin/gcc", "-O2", "-o", str(agent), str(code))
    shutil.copy2(helper, agent.with_name("vonk-agent-helper"))
    for binary in agent.parent.iterdir():
        binary.chmod(0o555)
    agent_sha, helper_sha = sha(agent), sha(helper)
    write(
        stage / "DEBIAN/control",
        f"Package: vonk-forge-agent\nVersion: {version}\nArchitecture: arm64\nMaintainer: acceptance <test@example.invalid>\nDescription: current-contract signed rollback fixture\n",
    )
    # Use the actual current downgrade/preflight preinst. This probe's unit is
    # separate from the production helper unit, so capsule recovery is not armed.
    preinst = (ROOT / "packaging/debian/preinst").read_text()
    for key, value in {
        "VERSION": version,
        "ARCHITECTURE": "arm64",
        "AGENT_SHA256": agent_sha,
        "HELPER_SHA256": helper_sha,
    }.items():
        preinst = preinst.replace(f"@{key}@", value)
    write(stage / "DEBIAN/preinst", preinst, 0o755)
    write(
        stage / "DEBIAN/prerm",
        "#!/bin/sh\nset -eu\n/usr/bin/systemctl stop vonk-forge-agent.service\n",
        0o755,
    )
    failure = "exit 44\n" if fault == "postinst" else ""
    pause = (
        "if [ -e /run/vonk-551-interrupt ]; then touch /run/vonk-551-restoring; while [ -e /run/vonk-551-interrupt ]; do sleep 1; done; fi\n"
        if generation == 1
        else ""
    )
    write(
        stage / "DEBIAN/postinst",
        "#!/bin/sh\nset -eu\n"
        + pause
        + failure
        + 'if [ "${SYSTEMD_OFFLINE:-0}" != 1 ]; then /usr/bin/systemctl daemon-reload; /usr/bin/systemctl restart vonk-forge-agent.service; fi\n',
        0o755,
    )
    write(
        stage / "lib/systemd/system/vonk-forge-agent.service",
        "[Unit]\nDescription=Current-contract acceptance agent\n[Service]\nType=simple\nExecStart=/usr/lib/vonk-forge/vonk-agent\nRestart=on-failure\n[Install]\nWantedBy=multi-user.target\n",
    )
    shutil.copy2(ROOT / "packaging/systemd" / UNIT, stage / "lib/systemd/system" / UNIT)
    package = work / f"package-{generation}.deb"
    run(
        "/usr/bin/dpkg-deb",
        "--build",
        "-Zgzip",
        "-z1",
        "--root-owner-group",
        str(stage),
        str(package),
    )
    digest = sha(package)
    message = work / f"message-{generation}"
    message.write_bytes(b"VONK-HOST-ARTIFACT-V1\0deb\0" + bytes.fromhex(digest))
    signature = work / f"signature-{generation}"
    run(
        "/usr/bin/openssl",
        "pkeyutl",
        "-sign",
        "-rawin",
        "-inkey",
        str(work / "key.pem"),
        "-in",
        str(message),
        "-out",
        str(signature),
    )
    return {
        "package": package,
        "package_sha256": digest,
        "package_signature": signature.read_bytes().hex(),
        "package_version": version,
        "binary_sha256": agent_sha,
        "helper_sha256": helper_sha,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--disposable-systemd", action="store_true", required=True)
    parser.add_argument("--helper", type=Path, required=True)
    parser.add_argument("--probe", type=Path, required=True)
    args = parser.parse_args()
    assert os.geteuid() == 0 and Path("/run/systemd/system").is_dir()
    assert run("/usr/bin/dpkg", "--print-architecture").stdout.strip() == "arm64"
    assert (
        run("/usr/bin/dpkg-query", "-W", "vonk-forge-agent", check=False).returncode
        != 0
    ), "requires fresh disposable machine"
    for directory in (
        "/var/lib/vonk-forge",
        "/var/lib/vonk-forge-agent",
        "/etc/vonk-forge-agent",
        "/etc/sudoers.d",
        "/usr/share/doc/vonk-forge-agent",
        "/usr/share/keyrings",
    ):
        Path(directory).mkdir(parents=True, exist_ok=True)
    STATE.parent.chmod(0o755)
    incoming = STATE.parent / "incoming"
    incoming.mkdir(mode=0o700, exist_ok=True)
    for file in ("/etc/subuid", "/etc/subgid"):
        Path(file).touch(exist_ok=True)
    write(
        Path("/lib/systemd/system/vonk-forge-package-helper.socket"),
        "[Socket]\nListenStream=/run/vonk-551-helper.sock\nService=vonk-forge-package-helper.service\n[Install]\nWantedBy=sockets.target\n",
    )
    write(
        Path("/lib/systemd/system/vonk-forge-package-helper.service"),
        "[Service]\nExecStart=/usr/bin/sleep infinity\n",
    )
    with tempfile.TemporaryDirectory(prefix="vonk-551-", dir="/var/tmp") as temporary:
        work = Path(temporary)
        run(
            "/usr/bin/openssl",
            "genpkey",
            "-algorithm",
            "ED25519",
            "-out",
            str(work / "key.pem"),
        )
        run(
            "/usr/bin/openssl",
            "pkey",
            "-in",
            str(work / "key.pem"),
            "-pubout",
            "-outform",
            "DER",
            "-out",
            str(work / "public.der"),
        )
        write(
            Path("/usr/share/keyrings/vonk-forge-release.pub"),
            (work / "public.der").read_bytes()[-32:].hex(),
        )
        source, candidate, broken = (
            build(work, args.helper, 1),
            build(work, args.helper, 2),
            build(work, args.helper, 3, "postinst"),
        )
        evidence = []

        def baseline():
            run("/usr/bin/systemctl", "stop", UNIT, check=False)
            if STATE.exists():
                shutil.rmtree(STATE)
            RECEIPT.unlink(missing_ok=True)
            run(
                "/usr/bin/dpkg",
                "--remove",
                "--force-remove-reinstreq",
                "vonk-forge-agent",
                check=False,
            )
            run(
                "/usr/bin/dpkg",
                "--install",
                str(source["package"]),
                env=dict(os.environ, SYSTEMD_OFFLINE="1"),
            )
            run("/usr/bin/systemctl", "daemon-reload")
            run("/usr/bin/systemctl", "enable", UNIT)
            run(
                "/usr/bin/systemctl",
                "reset-failed",
                UNIT,
                "vonk-forge-agent.service",
                check=False,
            )
            run("/usr/bin/systemctl", "start", "vonk-forge-agent.service")
            assert sha(AGENT) == source["binary_sha256"]
            for item in (source, candidate, broken):
                path = incoming / f"{item['package_sha256']}.deb"
                shutil.copyfile(item["package"], path)
                path.chmod(0o600)

        def operation(target=candidate, deadline=15):
            return {
                "type": "install-vonk-deb",
                "package_sha256": target["package_sha256"],
                "package_signature": target["package_signature"],
                "rollback": {
                    "source": {
                        key: value for key, value in source.items() if key != "package"
                    },
                    "attempt_nonce": os.urandom(32).hex(),
                    "activation_deadline": int(time.time()) + deadline,
                },
            }

        def probe(payload, success=True):
            # A real sanitized transient systemd service invokes the production
            # executor. Artifact signatures and root custody are not mocked.
            request = work / "request.json"
            request.write_text(json.dumps(payload))
            unit = "vonk-551-activation-" + os.urandom(5).hex()
            result = run(
                "/usr/bin/systemd-run",
                "--quiet",
                "--wait",
                "--pipe",
                "--collect",
                "--unit=" + unit,
                "--property=Environment=PATH=/usr/bin:/bin",
                str(args.probe),
                input=json.dumps(payload),
                check=False,
            )
            assert (result.returncode == 0) == success, result.stderr
            assert "panicked" not in result.stderr, result.stderr
            return result

        def source_restored(label):
            receipt = wait(
                lambda: receipt_phase("rolled_back"), label + ": source not restored"
            )
            assert (
                run(
                    "/usr/bin/dpkg-query",
                    "-W",
                    "-f=${db:Status-Abbrev}|${Version}",
                    "vonk-forge-agent",
                ).stdout
                == "ii |" + source["package_version"]
            )
            assert sha(AGENT) == source["binary_sha256"]
            pid = run(
                "/usr/bin/systemctl",
                "show",
                "--property=MainPID",
                "--value",
                "vonk-forge-agent.service",
            ).stdout.strip()
            assert sha(Path(f"/proc/{pid}/exe")) == source["binary_sha256"]
            assert (
                receipt["source_package_sha256"] == source["package_sha256"]
                and receipt["node_id"] == NODE
            )
            evidence.append({"case": label, **receipt})

        baseline()
        before = run(
            "/usr/bin/systemctl",
            "show",
            "--property=MainPID",
            "--value",
            "vonk-forge-agent.service",
        ).stdout
        logger = Path("/usr/bin/logger")
        logger.rename("/usr/bin/logger.vonk551-test")
        try:
            rejected = probe(operation(), success=False)
            assert "package activation prerequisites failed" in rejected.stderr, (
                rejected.stderr
            )
        finally:
            Path("/usr/bin/logger.vonk551-test").rename(logger)
        assert (
            run(
                "/usr/bin/systemctl",
                "show",
                "--property=MainPID",
                "--value",
                "vonk-forge-agent.service",
            ).stdout
            == before
        )
        assert not STATE.joinpath("transaction.json").exists()
        wrong = operation()
        wrong["rollback"]["source"]["binary_sha256"] = "a" * 64
        probe(wrong, success=False)
        assert (
            run(
                "/usr/bin/systemctl",
                "show",
                "--property=MainPID",
                "--value",
                "vonk-forge-agent.service",
            ).stdout
            == before
        )
        evidence.append(
            {
                "case": "missing-prerequisite-and-source-mismatch",
                "healthy_source_pid_preserved": True,
                "node_id": NODE,
                "source_version": source["package_version"],
                "source_package_sha256": source["package_sha256"],
                "candidate_version": candidate["package_version"],
                "candidate_package_sha256": candidate["package_sha256"],
                "observed_at": int(time.time()),
                "outcome": "preflight_rejected_before_agent_stop",
            }
        )

        baseline()
        pending = operation()
        probe(pending)
        ack = {
            "type": "confirm-package-activation",
            "package_sha256": candidate["package_sha256"],
            "attempt_nonce": "0" * 64,
        }
        probe(ack, success=False)
        source_restored("reconnect-readiness-deadline-and-wrong-nonce")

        baseline()
        pending = operation(deadline=15)
        probe(pending)
        ack["attempt_nonce"] = pending["rollback"]["attempt_nonce"]
        probe(ack)
        confirmed = wait(lambda: receipt_phase("acknowledged"), "ack receipt missing")
        time.sleep(max(0, pending["rollback"]["activation_deadline"] - time.time() + 1))
        assert sha(AGENT) == candidate["binary_sha256"]
        evidence.append({"case": "exact-ack-keeps-candidate", **confirmed})
        healthy_candidate_pid = run(
            "/usr/bin/systemctl",
            "show",
            "--property=MainPID",
            "--value",
            "vonk-forge-agent.service",
        ).stdout
        downgrade = operation(source)
        downgrade["rollback"]["source"] = {
            key: value for key, value in candidate.items() if key != "package"
        }
        rejected = probe(downgrade, success=False)
        assert "package activation prerequisites failed" in rejected.stderr, (
            rejected.stderr
        )
        assert (
            run(
                "/usr/bin/systemctl",
                "show",
                "--property=MainPID",
                "--value",
                "vonk-forge-agent.service",
            ).stdout
            == healthy_candidate_pid
        )
        # Outside the watchdog unit the same signed prior DEB cannot bypass
        # the current production downgrade guard, even if given the nonce.
        forbidden = run(
            "/usr/bin/dpkg",
            "--install",
            str(source["package"]),
            check=False,
            env=dict(
                os.environ,
                SYSTEMD_OFFLINE="1",
                VONK_FORGE_PACKAGE_ROLLBACK_NONCE=ack["attempt_nonce"],
            ),
        )
        assert forbidden.returncode != 0 and sha(AGENT) == candidate["binary_sha256"]

        baseline()
        probe(operation(broken, deadline=45), success=False)
        source_restored("failed-postinst")

        baseline()
        Path("/run/vonk-551-restoring").unlink(missing_ok=True)
        Path("/run/vonk-551-interrupt").touch()
        probe(operation(deadline=10))
        wait(
            lambda: Path("/run/vonk-551-restoring").exists(),
            "rollback did not enter source configure",
        )
        run("/usr/bin/systemctl", "kill", "--kill-whom=all", "--signal=KILL", UNIT)
        Path("/run/vonk-551-interrupt").unlink()
        run("/usr/bin/systemctl", "restart", UNIT)
        source_restored("interrupted-rollback-resumes")
        print(
            json.dumps(
                {
                    "schema_version": 2,
                    "lane": "disposable-arm64-systemd",
                    "cases": evidence,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(
            run(
                "/usr/bin/journalctl",
                "--no-pager",
                "-n",
                "100",
                "-u",
                UNIT,
                check=False,
            ).stdout
        )
        raise
