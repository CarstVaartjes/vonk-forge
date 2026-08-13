from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT.parent / "control/src"))
from vonk_control.update_authority import UpdateAuthorizationAuthority

SUPERVISOR = PROJECT / "supervisor" / "vonk-agent-supervisor"
AGENT_UNIT = PROJECT / "systemd" / "vonk-forge-agent.service"
SUPERVISOR_UNIT = PROJECT / "systemd" / "vonk-forge-agent-supervisor.service"
ACTIVATION_UNIT = PROJECT / "systemd" / "vonk-forge-agent-activation.service"
ACTIVATION_PATH = PROJECT / "systemd" / "vonk-forge-agent-activation.path"
ROLLBACK_UNIT = PROJECT / "systemd" / "vonk-forge-agent-rollback.service"
ROLLBACK_PATH = PROJECT / "systemd" / "vonk-forge-agent-rollback.path"
SYSTEMD_VERIFY = PROJECT.parent / "scripts" / "verify-agent-systemd"


def _platform_target(platform_version: str, target_sha256: str) -> str:
    return f"platform/releases/{platform_version}/{target_sha256}.json"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_supervisor_entrypoint_ignores_writable_python_site_hooks(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "sitecustomize-ran"
    python_path = tmp_path / "python-path"
    python_path.mkdir()
    (python_path / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('unsafe')\n"
    )
    home = tmp_path / "home"
    user_site = home / (
        f".local/lib/python{platform.python_version_tuple()[0]}."
        f"{platform.python_version_tuple()[1]}/site-packages"
    )
    user_site.mkdir(parents=True)
    (user_site / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('unsafe')\n"
    )

    result = subprocess.run(
        [str(SUPERVISOR), "--help"],
        env={**os.environ, "HOME": str(home), "PYTHONPATH": str(python_path)},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()


class SupervisorHost:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.actions = root / "systemctl-actions"
        self.actions.write_text("")
        self.systemctl = root / "systemctl"
        self.systemctl.write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$*" >> "$VONK_TEST_SYSTEMCTL_ACTIONS"\n'
            'if [ "${1:-}" = is-failed ]; then exit 1; fi\n'
            "exit 0\n"
        )
        self.systemctl.chmod(0o755)
        self.environment = {
            **os.environ,
            "VONK_SUPERVISOR_TEST_ROOT": str(root / "host"),
            "VONK_SUPERVISOR_SYSTEMCTL": str(self.systemctl),
            "VONK_TEST_SYSTEMCTL_ACTIONS": str(self.actions),
            "VONK_SUPERVISOR_TEST_UID": str(os.geteuid()),
            "VONK_SUPERVISOR_POLL_SECONDS": "0.01",
        }
        self.update_signer = ed25519.Ed25519PrivateKey.generate()
        public = self.update_signer.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        authority = self.host_root / "etc/vonk-forge-agent/update-authority.json"
        authority.parent.mkdir(parents=True)
        authority.write_text(
            json.dumps(
                {
                    "algorithm": "ed25519",
                    "key_id": hashlib.sha256(public).hexdigest(),
                    "public_key": public.hex(),
                    "schema_version": 1,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        authority.chmod(0o444)
        config = authority.with_name("config.json")
        config.write_text(
            json.dumps(
                {"node_id": "spk_0123456789abcdef0123456789abcdef"},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        config.chmod(0o644)

    @property
    def host_root(self) -> Path:
        return Path(self.environment["VONK_SUPERVISOR_TEST_ROOT"])

    @property
    def state_path(self) -> Path:
        return self.host_root / "var/lib/vonk-forge-agent-supervisor/state.json"

    @property
    def readiness_path(self) -> Path:
        return self.host_root / "run/vonk-forge-agent/readiness.json"

    @property
    def challenge_path(self) -> Path:
        return self.host_root / "run/vonk-forge-agent-supervisor/activation-challenge"

    @property
    def service_pid_path(self) -> Path:
        return self.root / "agent-service.pid"

    @property
    def activation_request_path(self) -> Path:
        return self.host_root / "run/vonk-forge-agent/activation-request.json"

    @property
    def rollback_request_path(self) -> Path:
        return self.host_root / "run/vonk-forge-agent/rollback-request.json"

    def stage_update_request(
        self,
        candidate: Path,
        *,
        previous: str = "A",
        target: str = "B",
        expires_at: int | None = None,
        operation_id: str = "22222222-2222-4222-8222-222222222222",
        fence: str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        tuf_targets_version: int = 7,
        platform_version: str = "1.2.0",
        platform_target_sha256: str = "b" * 64,
        platform_target_name: str | None = None,
    ) -> str:
        digest = _digest(candidate)
        staging = self.host_root / "var/lib/vonk-forge-agent/update-staging"
        staging.mkdir(parents=True, mode=0o700, exist_ok=True)
        staging.parent.chmod(0o700)
        staging.chmod(0o700)
        staged = staging / f"{digest}.agent"
        if not staged.exists():
            shutil.copyfile(candidate, staged)
            staged.chmod(0o500)
        self.activation_request_path.parent.mkdir(parents=True, exist_ok=True)
        self.activation_request_path.parent.chmod(0o700)
        fixed_expiry = expires_at or int(__import__("time").time()) + 60
        generation = self.state()["generation"]
        authorization = {
            "architecture": (
                "linux-arm64"
                if self.environment.get("VONK_SUPERVISOR_TEST_ARCH") == "aarch64"
                else "linux-x86_64"
            ),
            "attempt": 1,
            "build_digest": "sha256:" + digest,
            "claim_deadline": fixed_expiry,
            "expires_at": fixed_expiry,
            "fence": fence,
            "node_id": "spk_0123456789abcdef0123456789abcdef",
            "oci_manifest_digest": "sha256:" + "a" * 64,
            "operation_id": operation_id,
            "payload_name": "vonk-agent",
            "platform_target_name": platform_target_name
            or _platform_target(platform_version, platform_target_sha256),
            "platform_version": platform_version,
            "platform_target_sha256": platform_target_sha256,
            "previous_slot": previous,
            "previous_sha256": _digest(
                self.host_root
                / f"opt/vonk-forge/agent-slots/{previous}/vonk-forge-agent"
            ),
            "previous_generation": generation,
            "sha256": digest,
            "size": staged.stat().st_size,
            "target_slot": target,
            "tuf_targets_version": tuf_targets_version,
        }
        signature = self.update_signer.sign(
            (json.dumps(authorization, sort_keys=True, separators=(",", ":")) + "\n").encode()
        )
        request = {
            "authorization": authorization,
            "schema_version": 2,
            "signature": {
                "algorithm": "ed25519",
                "key_id": hashlib.sha256(
                    self.update_signer.public_key().public_bytes(
                        serialization.Encoding.Raw,
                        serialization.PublicFormat.Raw,
                    )
                ).hexdigest(),
                "value": signature.hex(),
            },
        }
        self.activation_request_path.write_text(
            json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n"
        )
        self.activation_request_path.chmod(0o600)
        return digest

    @staticmethod
    def write_identity(target: Path, *, platform_version: str = "1.0.0") -> None:
        digest = _digest(target)
        target.with_name("identity.json").write_text(
            json.dumps(
                {
                    "build_digest": "sha256:" + digest,
                    "platform_version": platform_version,
                    "schema_version": 1,
                    "sha256": digest,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        target.with_name("identity.json").chmod(0o444)

    def compile_agent(self, slot: str, message: str) -> Path:
        source = self.root / f"agent-{slot}.c"
        source.write_text(
            "#include <stdio.h>\n"
            'int main(void) { puts("' + message + '"); return 0; }\n'
        )
        target = self.host_root / "opt/vonk-forge/agent-slots" / slot / "vonk-forge-agent"
        target.parent.mkdir(parents=True, mode=0o755)
        subprocess.run(
            ["cc", "-O2", "-o", str(target), str(source)],
            check=True,
            capture_output=True,
        )
        target.chmod(0o555)
        self.write_identity(target)
        return target

    def compile_candidate(self, name: str, message: str) -> Path:
        source = self.root / f"{name}.c"
        source.write_text(
            "#include <stdio.h>\n"
            'int main(void) { puts("' + message + '"); return 0; }\n'
        )
        target = self.root / name
        subprocess.run(
            ["cc", "-O2", "-o", str(target), str(source)],
            check=True,
            capture_output=True,
        )
        return target

    def compile_readiness_agent(
        self,
        slot: str,
        *,
        pid_delta: int = 0,
        publish_delay_milliseconds: int = 0,
    ) -> Path:
        source = self.root / f"agent-ready-{slot}.c"
        source.write_text(
            "#include <fcntl.h>\n#include <stdio.h>\n#include <stdlib.h>\n"
            "#include <string.h>\n"
            "#include <sys/stat.h>\n#include <unistd.h>\n"
            "int main(void) {\n"
            'char credential[4096]; snprintf(credential, sizeof(credential), "%s/activation-challenge", getenv("CREDENTIALS_DIRECTORY"));\n'
            "FILE *c=fopen(credential, \"r\"); if (!c) return 3;\n"
            "char challenge[66]; if (!fgets(challenge, sizeof(challenge), c)) return 4; fclose(c); challenge[strcspn(challenge, \"\\n\")]=0;\n"
            f'FILE *f=fopen("{self.readiness_path}.new", "w"); if (!f) return 2;\n'
            f"usleep({publish_delay_milliseconds} * 1000);\n"
            'fprintf(f, "{\\"challenge\\":\\"%s\\",\\"generation\\":%s,\\"pid\\":%ld,\\"schema_version\\":2,'
            '\\"sha256\\":\\"%s\\",\\"slot\\":\\"%s\\"}\\n", challenge, '
            'getenv("VONK_AGENT_SUPERVISOR_GENERATION"), '
            f"(long)getpid()+{pid_delta}, "
            'getenv("VONK_AGENT_SUPERVISOR_SHA256"), '
            'getenv("VONK_AGENT_SUPERVISOR_SLOT"));\n'
            "fflush(f); fsync(fileno(f)); fchmod(fileno(f), 0600); fclose(f);\n"
            f'if (rename("{self.readiness_path}.new", "{self.readiness_path}") != 0) return 5;\n'
            f'int d=open("{self.readiness_path.parent}", O_RDONLY|O_DIRECTORY); if (d < 0) return 6; fsync(d); close(d);\n'
            f"sleep({3 if pid_delta == 0 else 0}); return 0; }}\n"
        )
        target = self.host_root / "opt/vonk-forge/agent-slots" / slot / "vonk-forge-agent"
        target.parent.mkdir(parents=True, mode=0o755)
        subprocess.run(
            ["cc", "-O2", "-o", str(target), str(source)],
            check=True,
            capture_output=True,
        )
        target.chmod(0o555)
        self.write_identity(target)
        return target

    def spawn_agent_from_systemctl(self) -> None:
        self.systemctl.write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "$*" >> "$VONK_TEST_SYSTEMCTL_ACTIONS"\n'
            'if [ "${1:-}" = stop ]; then\n'
            '  if [ -s "$VONK_TEST_SERVICE_PID" ]; then kill "$(cat "$VONK_TEST_SERVICE_PID")" 2>/dev/null || true; fi\n'
            '  rm -f "$VONK_TEST_SERVICE_PID"\n'
            "fi\n"
            'if [ "${1:-}" = restart ] || [ "${1:-}" = start ]; then\n'
            '  "$VONK_TEST_SUPERVISOR" run-agent &\n'
            '  printf \'%s\\n\' "$!" > "$VONK_TEST_SERVICE_PID"\n'
            "fi\n"
            'if [ "${1:-}" = show ]; then cat "$VONK_TEST_SERVICE_PID" 2>/dev/null || printf \'0\\n\'; fi\n'
            'if [ "${1:-}" = is-failed ]; then\n'
            '  if [ -s "$VONK_TEST_SERVICE_PID" ] && kill -0 "$(cat "$VONK_TEST_SERVICE_PID")" 2>/dev/null; then exit 1; fi\n'
            '  exit 0\n'
            "fi\n"
            "exit 0\n"
        )
        self.systemctl.chmod(0o755)
        self.environment["VONK_TEST_SUPERVISOR"] = str(SUPERVISOR)
        self.environment["VONK_TEST_SERVICE_PID"] = str(self.service_pid_path)

    def run(
        self, *arguments: str, timeout: float = 5
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SUPERVISOR), *arguments],
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def run_with_umask(
        self, umask: int, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(SUPERVISOR), *arguments],
            env=self.environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            preexec_fn=lambda: os.umask(umask),
        )

    def state(self) -> dict[str, object]:
        return json.loads(self.state_path.read_text())

    def readiness(self, *, generation: int, slot: str, digest: str) -> None:
        self.readiness_path.parent.mkdir(parents=True, exist_ok=True)
        self.readiness_path.parent.chmod(0o700)
        document = {
            "generation": generation,
            "schema_version": 1,
            "sha256": digest,
            "slot": slot,
        }
        self.readiness_path.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
        )
        self.readiness_path.chmod(0o600)

    def write_rollback_request(self, request: dict[str, object]) -> None:
        state = self.state()
        authorization = {
            "action": "operator-rollback",
            "attempt": 1,
            "claim_deadline": int(__import__("time").time()) + 60,
            "current_generation": state["generation"],
            "current_sha256": request["current_sha256"],
            "current_slot": request["current_slot"],
            "expires_at": int(__import__("time").time()) + 60,
            "fence": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "node_id": "spk_0123456789abcdef0123456789abcdef",
            "operation_id": "22222222-2222-4222-8222-222222222222",
        }
        authorization["expires_at"] = authorization["claim_deadline"]
        raw_authorization = (
            json.dumps(authorization, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        signature = self.update_signer.sign(raw_authorization)
        signed = {
            "authorization": authorization,
            "schema_version": 2,
            "signature": {
                "algorithm": "ed25519",
                "key_id": hashlib.sha256(
                    self.update_signer.public_key().public_bytes(
                        serialization.Encoding.Raw,
                        serialization.PublicFormat.Raw,
                    )
                ).hexdigest(),
                "value": signature.hex(),
            },
        }
        self.rollback_request_path.write_text(
            json.dumps(signed, sort_keys=True, separators=(",", ":")) + "\n"
        )
        self.rollback_request_path.chmod(0o600)


@pytest.fixture
def supervisor_host(tmp_path: Path) -> SupervisorHost:
    return SupervisorHost(tmp_path)


def test_initialize_and_run_agent_executes_only_verified_elf(
    supervisor_host: SupervisorHost,
) -> None:
    agent = supervisor_host.compile_agent("A", "slot-a")

    initialized = supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", _digest(agent)
    )
    launched = supervisor_host.run("run-agent")

    assert initialized.returncode == 0, initialized.stderr
    assert launched.returncode == 0, launched.stderr
    assert launched.stdout == "slot-a\n"
    assert supervisor_host.state() == {
        "activation_deadline": None,
        "active_slot": "A",
        "boot_attempts": 0,
        "expected_sha256": _digest(agent),
        "generation": 1,
        "previous_slot": None,
        "rollback_performed": False,
        "schema_version": 1,
        "slot_sha256": {"A": _digest(agent), "B": None},
        "status": "stable",
    }


def test_run_agent_exports_verified_release_identity(
    supervisor_host: SupervisorHost,
) -> None:
    source = supervisor_host.root / "identity-agent.c"
    source.write_text(
        "#include <stdio.h>\n#include <stdlib.h>\n"
        "int main(void){printf(\"%s %s %s %s %s\\n\","
        "getenv(\"VONK_AGENT_PLATFORM_VERSION\"),"
        "getenv(\"VONK_AGENT_BUILD_DIGEST\"),"
        "getenv(\"VONK_AGENT_SUPERVISOR_GENERATION\"),"
        "getenv(\"VONK_AGENT_SUPERVISOR_SLOT\"),"
        "getenv(\"VONK_AGENT_SUPERVISOR_SHA256\"));}\n"
    )
    target = (
        supervisor_host.host_root
        / "opt/vonk-forge/agent-slots/A/vonk-forge-agent"
    )
    target.parent.mkdir(parents=True)
    subprocess.run(["cc", "-O2", "-o", target, source], check=True)
    target.chmod(0o555)
    supervisor_host.write_identity(target, platform_version="7.8.9")
    digest = _digest(target)

    assert supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", digest
    ).returncode == 0
    launched = supervisor_host.run("run-agent")

    assert launched.returncode == 0, launched.stderr
    assert launched.stdout == f"7.8.9 sha256:{digest} 1 A {digest}\n"


def test_run_package_helper_executes_the_verified_active_slot_with_socket_state(
    supervisor_host: SupervisorHost,
) -> None:
    source = supervisor_host.root / "package-helper-agent.c"
    source.write_text(
        "#include <stdio.h>\n#include <stdlib.h>\n"
        "int main(int argc, char **argv){"
        'printf("%d %s %s %s %s %s %s\\n", argc, argv[1], argv[2], '
        'getenv("LISTEN_FDS"), getenv("LISTEN_PID"), getenv("HOME"), '
        'getenv("VONK_PACKAGE_HELPER_SLOT_SHA256"));}\n'
    )
    target = (
        supervisor_host.host_root
        / "opt/vonk-forge/agent-slots/A/vonk-forge-agent"
    )
    target.parent.mkdir(parents=True)
    subprocess.run(["cc", "-O2", "-o", target, source], check=True)
    target.chmod(0o555)
    supervisor_host.write_identity(target)
    digest = _digest(target)
    assert supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", digest
    ).returncode == 0

    launched = subprocess.run(
        [
            "/bin/sh",
            "-c",
            f'LISTEN_PID=$$ LISTEN_FDS=1 exec "{SUPERVISOR}" run-package-helper',
        ],
        env=supervisor_host.environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert launched.returncode == 0, launched.stderr
    fields = launched.stdout.strip().split()
    assert fields[:4] == ["3", "--package-helper", "--listen-fd=3", "1"]
    assert int(fields[4]) > 1
    assert fields[5] == "/var/lib/vonk-forge-package-helper"
    assert fields[6] == digest


def test_apply_request_installs_verified_inactive_slot_and_starts_activation(
    supervisor_host: SupervisorHost,
) -> None:
    active = supervisor_host.compile_agent("A", "slot-a")
    assert supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", _digest(active)
    ).returncode == 0
    candidate = supervisor_host.root / "candidate"
    source = supervisor_host.root / "candidate.c"
    source.write_text('#include <stdio.h>\nint main(void){puts("slot-b");}\n')
    subprocess.run(["cc", "-O2", "-o", candidate, source], check=True)
    digest = supervisor_host.stage_update_request(candidate)

    applied = supervisor_host.run("apply-request")

    assert applied.returncode == 0, applied.stderr
    installed = (
        supervisor_host.host_root
        / "opt/vonk-forge/agent-slots/B/vonk-forge-agent"
    )
    assert _digest(installed) == digest
    assert installed.stat().st_mode & 0o777 == 0o555
    identity = json.loads(installed.with_name("identity.json").read_text())
    assert identity == {
        "build_digest": "sha256:" + digest,
        "platform_version": "1.2.0",
        "schema_version": 1,
        "sha256": digest,
    }
    assert not supervisor_host.activation_request_path.exists()
    state = supervisor_host.state()
    assert state["active_slot"] == "B"
    assert state["previous_slot"] == "A"
    assert state["status"] == "pending"
    assert "--no-block restart vonk-forge-agent-supervisor.service" in (
        supervisor_host.actions.read_text()
    )


def test_apply_request_cleans_crash_leftovers_before_slot_publication(
    supervisor_host: SupervisorHost,
) -> None:
    active = supervisor_host.compile_agent("A", "slot-a")
    assert supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", _digest(active)
    ).returncode == 0
    candidate = supervisor_host.root / "candidate"
    source = supervisor_host.root / "candidate.c"
    source.write_text('int main(void){return 0;}\n')
    subprocess.run(["cc", "-O2", "-o", candidate, source], check=True)
    supervisor_host.stage_update_request(candidate)
    slot = supervisor_host.host_root / "opt/vonk-forge/agent-slots/B"
    slot.mkdir(parents=True, exist_ok=True)
    executable_partial = slot / (".vonk-forge-agent." + "0" * 24 + ".new")
    identity_partial = slot / (".identity.json." + "1" * 24 + ".new")
    executable_partial.write_bytes(b"partial")
    executable_partial.chmod(0o500)
    identity_partial.write_bytes(b"partial")
    identity_partial.chmod(0o400)

    applied = supervisor_host.run("apply-request")

    assert applied.returncode == 0, applied.stderr
    assert not executable_partial.exists()
    assert not identity_partial.exists()


def test_unprivileged_agent_cannot_persist_unsigned_elf(
    supervisor_host: SupervisorHost,
) -> None:
    active = supervisor_host.compile_agent("A", "slot-a")
    assert supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", _digest(active)
    ).returncode == 0
    candidate = supervisor_host.compile_candidate("unsigned-candidate", "slot-b")
    supervisor_host.stage_update_request(candidate)
    request = json.loads(supervisor_host.activation_request_path.read_text())
    request["signature"]["value"] = "0" * 128
    supervisor_host.activation_request_path.write_text(
        json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n"
    )
    before = supervisor_host.state()

    rejected = supervisor_host.run("apply-request")

    assert rejected.returncode == 1
    assert "authorization signature" in rejected.stderr
    assert supervisor_host.state() == before
    assert not (
        supervisor_host.host_root
        / "opt/vonk-forge/agent-slots/B/vonk-forge-agent"
    ).exists()


def test_activation_rejects_mutable_root_authority_material(
    supervisor_host: SupervisorHost,
) -> None:
    active = supervisor_host.compile_agent("A", "slot-a")
    assert supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", _digest(active)
    ).returncode == 0
    candidate = supervisor_host.compile_candidate("authority-candidate", "slot-b")
    supervisor_host.stage_update_request(candidate)
    authority = (
        supervisor_host.host_root
        / "etc/vonk-forge-agent/update-authority.json"
    )
    authority.chmod(0o644)
    before = supervisor_host.state()

    rejected = supervisor_host.run("apply-request")

    assert rejected.returncode == 1
    assert "path mode is unsafe" in rejected.stderr
    assert supervisor_host.state() == before


def test_expired_and_replayed_activation_receipts_fail_before_mutation(
    supervisor_host: SupervisorHost,
) -> None:
    active = supervisor_host.compile_agent("A", "slot-a")
    assert supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", _digest(active)
    ).returncode == 0
    candidate = supervisor_host.compile_candidate("expiry-candidate", "slot-b")
    supervisor_host.stage_update_request(candidate, expires_at=10)
    before = supervisor_host.state()

    expired = supervisor_host.run("apply-request")

    assert expired.returncode == 1
    assert "expired" in expired.stderr
    assert supervisor_host.state() == before

    supervisor_host.stage_update_request(candidate)
    original = supervisor_host.activation_request_path.read_bytes()
    assert supervisor_host.run("apply-request").returncode == 0
    after_first = supervisor_host.state()
    supervisor_host.activation_request_path.write_bytes(original)
    supervisor_host.activation_request_path.chmod(0o600)

    replayed = supervisor_host.run("apply-request")

    assert replayed.returncode == 1
    assert "already consumed" in replayed.stderr
    assert supervisor_host.state() == after_first


def test_candidate_verification_failure_does_not_consume_activation_receipt(
    supervisor_host: SupervisorHost,
) -> None:
    active = supervisor_host.compile_agent("A", "slot-a")
    assert supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", _digest(active)
    ).returncode == 0
    candidate = supervisor_host.compile_candidate("retry-candidate", "slot-b")
    digest = supervisor_host.stage_update_request(candidate)
    staged = (
        supervisor_host.host_root
        / f"var/lib/vonk-forge-agent/update-staging/{digest}.agent"
    )
    original = staged.read_bytes()
    staged.chmod(0o700)
    staged.write_bytes(bytes([original[0] ^ 0x01]) + original[1:])
    staged.chmod(0o500)
    marker = supervisor_host.state_path.with_name(
        "authorization-"
        + hashlib.sha256(b"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa").hexdigest()
        + ".used"
    )

    rejected = supervisor_host.run("apply-request")

    assert rejected.returncode == 1
    assert "digest" in rejected.stderr
    assert not marker.exists()
    staged.chmod(0o700)
    staged.write_bytes(original)
    staged.chmod(0o500)
    retried = supervisor_host.run("apply-request")
    assert retried.returncode == 0, retried.stderr


def test_activation_receipt_rejects_stale_tuf_metadata_version(
    supervisor_host: SupervisorHost,
) -> None:
    active = supervisor_host.compile_agent("A", "slot-a")
    assert supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", _digest(active)
    ).returncode == 0
    floor = supervisor_host.state_path.with_name("authorization-floor.json")
    floor.write_text('{"schema_version":1,"tuf_targets_version":8}\n')
    floor.chmod(0o444)
    candidate = supervisor_host.compile_candidate("stale-candidate", "slot-b")
    supervisor_host.stage_update_request(candidate, tuf_targets_version=7)
    before = supervisor_host.state()

    rejected = supervisor_host.run("apply-request")

    assert rejected.returncode == 1
    assert "metadata is stale" in rejected.stderr
    assert supervisor_host.state() == before


@pytest.mark.parametrize(
    "platform_target_name",
    (
        "platform-release.json",
        _platform_target("1.2.1", "b" * 64),
        _platform_target("1.2.0", "0" * 64),
        "platform/releases/1.2.0/../../escape.json",
        _platform_target("1.2.0", "B" * 64),
    ),
)
def test_activation_receipt_rejects_invalid_platform_target_identity(
    supervisor_host: SupervisorHost,
    platform_target_name: str,
) -> None:
    active = supervisor_host.compile_agent("A", "slot-a")
    assert supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", _digest(active)
    ).returncode == 0
    candidate = supervisor_host.compile_candidate("wrong-target", "slot-b")
    supervisor_host.stage_update_request(
        candidate,
        platform_target_name=platform_target_name,
    )
    before = supervisor_host.state()

    rejected = supervisor_host.run("apply-request")

    assert rejected.returncode == 1
    assert "activation request is invalid" in rejected.stderr
    assert supervisor_host.state() == before


@pytest.mark.parametrize(
    "binding",
    ("sha256", "size", "slot_pair", "operation_id", "platform_target_sha256"),
)
def test_signed_activation_bindings_cannot_be_swapped(
    supervisor_host: SupervisorHost, binding: str
) -> None:
    active = supervisor_host.compile_agent("A", "slot-a")
    assert supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", _digest(active)
    ).returncode == 0
    candidate = supervisor_host.compile_candidate("swap-candidate", "slot-b")
    supervisor_host.stage_update_request(candidate)
    request = json.loads(supervisor_host.activation_request_path.read_text())
    replacements = {
        "sha256": "0" * 64,
        "size": int(request["authorization"]["size"]) + 1,
        "operation_id": "33333333-3333-4333-8333-333333333333",
        "platform_target_sha256": "0" * 64,
    }
    if binding == "slot_pair":
        request["authorization"]["previous_slot"] = "B"
        request["authorization"]["target_slot"] = "A"
    else:
        request["authorization"][binding] = replacements[binding]
    supervisor_host.activation_request_path.write_text(
        json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n"
    )
    before = supervisor_host.state()

    rejected = supervisor_host.run("apply-request")

    assert rejected.returncode == 1
    expected = (
        "activation request"
        if binding == "platform_target_sha256"
        else "authorization signature"
    )
    assert expected in rejected.stderr
    assert supervisor_host.state() == before


@pytest.mark.parametrize(
    ("previous", "target"),
    [("B", "B"), ("A", "A")],
)
def test_apply_request_rejects_wrong_previous_or_active_target(
    supervisor_host: SupervisorHost, previous: str, target: str
) -> None:
    active = supervisor_host.compile_agent("A", "slot-a")
    assert supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", _digest(active)
    ).returncode == 0
    candidate = supervisor_host.compile_agent("B", "slot-b")
    supervisor_host.stage_update_request(candidate, previous=previous, target=target)

    applied = supervisor_host.run("apply-request")

    assert applied.returncode == 1
    assert supervisor_host.state()["active_slot"] == "A"


def test_apply_rollback_request_selects_only_recorded_verified_previous_slot(
    supervisor_host: SupervisorHost,
) -> None:
    a = supervisor_host.compile_readiness_agent("A")
    b = supervisor_host.compile_readiness_agent("B")
    assert supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", _digest(a)
    ).returncode == 0
    assert supervisor_host.run(
        "activate", "--slot", "B", "--sha256", _digest(b)
    ).returncode == 0
    supervisor_host.spawn_agent_from_systemctl()
    assert supervisor_host.run("supervise").returncode == 0
    request = {
        "current_sha256": _digest(b),
        "current_slot": "B",
        "previous_sha256": _digest(a),
        "previous_slot": "A",
        "schema_version": 1,
    }
    supervisor_host.write_rollback_request(request)

    applied = supervisor_host.run("apply-rollback-request")

    assert applied.returncode == 0, applied.stderr
    rolled_back = supervisor_host.state()
    assert rolled_back["active_slot"] == "A"
    assert rolled_back["previous_slot"] == "B"
    assert rolled_back["status"] == "pending"
    assert not supervisor_host.rollback_request_path.exists()


def test_control_authority_signature_is_verified_by_root_supervisor(
    supervisor_host: SupervisorHost,
) -> None:
    a, b = _stable_ab_host(supervisor_host)
    signer = ed25519.Ed25519PrivateKey.generate()

    class UnusedReleaseSource:
        def refresh(self):
            raise AssertionError("rollback signing must not consult platform TUF")

    authority = UpdateAuthorizationAuthority(
        signer, release_source=UnusedReleaseSource()
    )
    pinned = supervisor_host.host_root / "etc/vonk-forge-agent/update-authority.json"
    pinned.chmod(0o600)
    pinned.write_bytes(authority.public_authority_bytes())
    pinned.chmod(0o444)
    now = datetime.now(UTC)
    state = supervisor_host.state()
    payload = authority.authorize_rollback(
        operation_id="22222222-2222-4222-8222-222222222222",
        fence="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        expires_at=int(now.timestamp()) + 60,
        current_slot="B",
        current_sha256=_digest(b),
        current_generation=int(state["generation"]),
        node_id="spk_0123456789abcdef0123456789abcdef",
        attempt=1,
        claim_deadline=int(now.timestamp()) + 60,
        now=now,
    )
    supervisor_host.rollback_request_path.write_text(
        json.dumps(
            {
                "authorization": payload["receipt"],
                "schema_version": 2,
                "signature": payload["signature"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    supervisor_host.rollback_request_path.chmod(0o600)

    applied = supervisor_host.run("apply-rollback-request")

    assert applied.returncode == 0, applied.stderr
    assert supervisor_host.state()["active_slot"] == "A"
    assert _digest(a) == supervisor_host.state()["expected_sha256"]


def _stable_ab_host(supervisor_host: SupervisorHost) -> tuple[Path, Path]:
    a = supervisor_host.compile_readiness_agent("A")
    b = supervisor_host.compile_readiness_agent("B")
    assert supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", _digest(a)
    ).returncode == 0
    assert supervisor_host.run(
        "activate", "--slot", "B", "--sha256", _digest(b)
    ).returncode == 0
    supervisor_host.spawn_agent_from_systemctl()
    assert supervisor_host.run("supervise").returncode == 0
    assert supervisor_host.state()["status"] == "stable"
    supervisor_host.actions.write_text("")
    return a, b


def test_readiness_fixture_publishes_only_complete_marker(
    supervisor_host: SupervisorHost,
) -> None:
    a = supervisor_host.compile_agent("A", "slot-a")
    b = supervisor_host.compile_readiness_agent(
        "B", publish_delay_milliseconds=100
    )
    assert supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", _digest(a)
    ).returncode == 0
    assert supervisor_host.run(
        "activate", "--slot", "B", "--sha256", _digest(b)
    ).returncode == 0
    supervisor_host.spawn_agent_from_systemctl()

    supervised = supervisor_host.run("supervise")

    assert supervised.returncode == 0, supervised.stderr
    assert supervisor_host.state()["status"] == "stable"


def _rollback_request(a: Path, b: Path) -> dict[str, object]:
    return {
        "current_sha256": _digest(b),
        "current_slot": "B",
        "previous_sha256": _digest(a),
        "previous_slot": "A",
        "schema_version": 1,
    }


@pytest.mark.parametrize(
    "malformation",
    (
        "boolean-schema",
        "float-schema",
        "unknown-field",
        "duplicate-field",
        "noncanonical",
        "truncated",
    ),
)
def test_apply_rollback_request_rejects_malformed_documents_without_state_change(
    supervisor_host: SupervisorHost, malformation: str
) -> None:
    a, b = _stable_ab_host(supervisor_host)
    request = _rollback_request(a, b)
    if malformation == "boolean-schema":
        request["schema_version"] = True
        raw = json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n"
    elif malformation == "float-schema":
        request["schema_version"] = 1.0
        raw = json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n"
    elif malformation == "unknown-field":
        request["unexpected"] = "unsafe"
        raw = json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n"
    elif malformation == "duplicate-field":
        raw = (
            json.dumps(request, sort_keys=True, separators=(",", ":"))[:-1]
            + ',"schema_version":1}\n'
        )
    elif malformation == "noncanonical":
        raw = json.dumps(request, indent=2) + "\n"
    else:
        raw = '{"schema_version":1\n'
    supervisor_host.rollback_request_path.write_text(raw)
    supervisor_host.rollback_request_path.chmod(0o600)
    before = supervisor_host.state()

    rejected = supervisor_host.run("apply-rollback-request")

    assert rejected.returncode == 1
    assert supervisor_host.state() == before
    assert supervisor_host.rollback_request_path.exists()
    assert supervisor_host.actions.read_text() == ""


@pytest.mark.parametrize("forgery", ("current_sha256", "slot_pair"))
def test_apply_rollback_request_rejects_forged_stable_identity(
    supervisor_host: SupervisorHost, forgery: str
) -> None:
    a, b = _stable_ab_host(supervisor_host)
    request = _rollback_request(a, b)
    if forgery == "slot_pair":
        request["current_slot"] = "A"
        request["previous_slot"] = "B"
    else:
        request[forgery] = "0" * 64
    supervisor_host.write_rollback_request(request)
    before = supervisor_host.state()

    rejected = supervisor_host.run("apply-rollback-request")

    assert rejected.returncode == 1
    assert supervisor_host.state() == before
    assert supervisor_host.rollback_request_path.exists()
    assert supervisor_host.actions.read_text() == ""


@pytest.mark.parametrize("unsafe_path", ("wrong-mode", "hardlink"))
def test_apply_rollback_request_rejects_unsafe_unprivileged_request_path(
    supervisor_host: SupervisorHost, unsafe_path: str
) -> None:
    a, b = _stable_ab_host(supervisor_host)
    supervisor_host.write_rollback_request(_rollback_request(a, b))
    if unsafe_path == "wrong-mode":
        supervisor_host.rollback_request_path.chmod(0o644)
    else:
        os.link(
            supervisor_host.rollback_request_path,
            supervisor_host.rollback_request_path.with_name("rollback-hardlink.json"),
        )
    before = supervisor_host.state()

    rejected = supervisor_host.run("apply-rollback-request")

    assert rejected.returncode == 1
    assert supervisor_host.state() == before
    assert supervisor_host.rollback_request_path.exists()
    assert supervisor_host.actions.read_text() == ""


@pytest.mark.parametrize(
    "corruption", ("invalid-identity", "boolean-identity-schema", "executable")
)
def test_apply_rollback_request_rejects_corrupt_recorded_slot_artifacts(
    supervisor_host: SupervisorHost, corruption: str
) -> None:
    a, b = _stable_ab_host(supervisor_host)
    request = _rollback_request(a, b)
    if corruption in {"invalid-identity", "boolean-identity-schema"}:
        identity = a.with_name("identity.json")
        identity.chmod(0o644)
        if corruption == "invalid-identity":
            identity.write_text("{not-json}\n")
        else:
            document = json.loads(identity.read_text())
            document["schema_version"] = True
            identity.write_text(
                json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
            )
        identity.chmod(0o444)
    else:
        a.chmod(0o755)
        a.write_bytes(b"corrupt rollback slot" * 8)
        a.chmod(0o555)
    supervisor_host.write_rollback_request(request)
    before = supervisor_host.state()

    rejected = supervisor_host.run("apply-rollback-request")

    assert rejected.returncode == 1
    assert supervisor_host.state() == before
    assert supervisor_host.rollback_request_path.exists()
    assert supervisor_host.actions.read_text() == ""


def test_stale_rollback_request_cannot_reverse_a_completed_rollback(
    supervisor_host: SupervisorHost,
) -> None:
    a, b = _stable_ab_host(supervisor_host)
    request = _rollback_request(a, b)
    supervisor_host.write_rollback_request(request)
    assert supervisor_host.run("apply-rollback-request").returncode == 0
    assert supervisor_host.run("supervise").returncode == 0
    assert supervisor_host.state()["status"] == "stable"
    supervisor_host.actions.write_text("")
    supervisor_host.write_rollback_request(request)
    before = supervisor_host.state()

    rejected = supervisor_host.run("apply-rollback-request")

    assert rejected.returncode == 1
    assert supervisor_host.state() == before
    assert supervisor_host.rollback_request_path.exists()
    assert supervisor_host.actions.read_text() == ""


def test_explicit_rollback_requires_generation_bound_readiness_to_commit(
    supervisor_host: SupervisorHost,
) -> None:
    a, b = _stable_ab_host(supervisor_host)
    supervisor_host.write_rollback_request(_rollback_request(a, b))
    assert supervisor_host.run("apply-rollback-request").returncode == 0

    committed = supervisor_host.run("supervise")

    assert committed.returncode == 0, committed.stderr
    stable = supervisor_host.state()
    assert stable["active_slot"] == "A"
    assert stable["previous_slot"] == "B"
    assert stable["status"] == "stable"
    assert stable["rollback_performed"] is False


def test_explicit_rollback_automatically_reverts_when_target_is_not_ready(
    supervisor_host: SupervisorHost,
) -> None:
    a, b = _stable_ab_host(supervisor_host)
    supervisor_host.write_rollback_request(_rollback_request(a, b))
    assert supervisor_host.run("apply-rollback-request").returncode == 0
    pending = supervisor_host.state()
    supervisor_host.environment["VONK_SUPERVISOR_NOW"] = str(
        float(pending["activation_deadline"]) + 1
    )

    reverted = supervisor_host.run("supervise")

    assert reverted.returncode == 1
    stable = supervisor_host.state()
    assert stable["active_slot"] == "B"
    assert stable["previous_slot"] == "A"
    assert stable["expected_sha256"] == _digest(b)
    assert stable["status"] == "stable"
    assert stable["rollback_performed"] is True
    assert "restart vonk-forge-agent.service" in (
        supervisor_host.actions.read_text().splitlines()
    )


def test_run_agent_rejects_script_and_hardlinked_or_tampered_elf(
    supervisor_host: SupervisorHost,
) -> None:
    agent = supervisor_host.compile_agent("A", "slot-a")
    digest = _digest(agent)
    assert (
        supervisor_host.run("initialize", "--slot", "A", "--sha256", digest).returncode
        == 0
    )

    link = agent.with_name("other-link")
    os.link(agent, link)
    hardlinked = supervisor_host.run("run-agent")
    link.unlink()
    agent.chmod(0o755)
    agent.write_bytes(b"#!/usr/bin/python3\nprint('mutable import')\n")
    agent.chmod(0o555)
    script = supervisor_host.run("run-agent")

    assert hardlinked.returncode != 0
    assert script.returncode != 0
    assert hardlinked.stdout == script.stdout == ""


def test_slot_path_rejects_symlink_in_any_ancestor(
    supervisor_host: SupervisorHost,
) -> None:
    agent = supervisor_host.compile_agent("A", "slot-a")
    digest = _digest(agent)
    vonk_root = supervisor_host.host_root / "opt/vonk-forge"
    moved = supervisor_host.root / "moved-vonk-forge"
    vonk_root.rename(moved)
    vonk_root.symlink_to(moved, target_is_directory=True)

    initialized = supervisor_host.run("initialize", "--slot", "A", "--sha256", digest)

    assert initialized.returncode != 0


def test_state_publication_is_exact_mode_even_under_service_umask(
    supervisor_host: SupervisorHost,
) -> None:
    agent = supervisor_host.compile_agent("A", "slot-a")

    initialized = supervisor_host.run_with_umask(
        0o077, "initialize", "--slot", "A", "--sha256", _digest(agent)
    )

    assert initialized.returncode == 0, initialized.stderr
    assert supervisor_host.state_path.stat().st_mode & 0o777 == 0o644
    assert supervisor_host.run("run-agent").returncode == 0


def test_stable_supervisor_prepares_clean_boot_runtime_without_dependency_cycle(
    supervisor_host: SupervisorHost,
) -> None:
    agent = supervisor_host.compile_agent("A", "slot-a")
    assert (
        supervisor_host.run(
            "initialize", "--slot", "A", "--sha256", _digest(agent)
        ).returncode
        == 0
    )
    assert not supervisor_host.readiness_path.parent.exists()

    supervised = supervisor_host.run("supervise")

    assert supervised.returncode == 0, supervised.stderr
    runtime = supervisor_host.readiness_path.parent
    assert runtime.is_dir()
    assert runtime.stat().st_mode & 0o777 == 0o700


def test_concurrent_initialization_serializes_and_publication_crashes_recover(
    tmp_path: Path,
) -> None:
    concurrent = SupervisorHost(tmp_path / "concurrent")
    agent = concurrent.compile_agent("A", "slot-a")
    arguments = ("initialize", "--slot", "A", "--sha256", _digest(agent))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: concurrent.run(*arguments), range(4)))
    assert [result.returncode for result in results] == [0, 0, 0, 0]

    for stage in ("create", "write", "file-fsync", "rename", "directory-fsync"):
        host = SupervisorHost(tmp_path / stage)
        artifact = host.compile_agent("A", "slot-a")
        host.environment["VONK_SUPERVISOR_CRASH_AFTER"] = stage
        crashed = host.run("initialize", "--slot", "A", "--sha256", _digest(artifact))
        assert crashed.returncode == 99
        host.environment.pop("VONK_SUPERVISOR_CRASH_AFTER")

        recovered = host.run("initialize", "--slot", "A", "--sha256", _digest(artifact))

        assert recovered.returncode == 0, recovered.stderr
        state_root = host.state_path.parent
        assert not list(state_root.glob(".state.*.new"))
        assert host.run("run-agent").returncode == 0


def test_activation_accepts_only_exact_generation_bound_readiness(
    supervisor_host: SupervisorHost,
) -> None:
    a = supervisor_host.compile_agent("A", "slot-a")
    b = supervisor_host.compile_agent("B", "slot-b")
    digest_a, digest_b = _digest(a), _digest(b)
    assert (
        supervisor_host.run(
            "initialize", "--slot", "A", "--sha256", digest_a
        ).returncode
        == 0
    )
    activated = supervisor_host.run("activate", "--slot", "B", "--sha256", digest_b)
    assert activated.returncode == 0, activated.stderr
    assert (
        "--no-block restart vonk-forge-agent-supervisor.service"
        in supervisor_host.actions.read_text().splitlines()
    )
    generation = int(supervisor_host.state()["generation"])
    supervisor_host.readiness(generation=generation - 1, slot="B", digest=digest_b)
    supervisor_host.systemctl.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$VONK_TEST_SYSTEMCTL_ACTIONS"\n'
        'if [ "${1:-}" = is-failed ]; then exit 0; fi\n'
        "exit 0\n"
    )
    supervisor_host.systemctl.chmod(0o755)
    supervised = supervisor_host.run("supervise")

    assert supervised.returncode != 0
    state = supervisor_host.state()
    assert state["active_slot"] == "A"
    assert state["expected_sha256"] == digest_a
    assert state["rollback_performed"] is True
    assert not supervisor_host.readiness_path.exists()


def test_preplanted_correct_looking_readiness_cannot_commit_new_slot(
    supervisor_host: SupervisorHost,
) -> None:
    a = supervisor_host.compile_agent("A", "slot-a")
    b = supervisor_host.compile_agent("B", "slot-b")
    assert (
        supervisor_host.run(
            "initialize", "--slot", "A", "--sha256", _digest(a)
        ).returncode
        == 0
    )
    assert (
        supervisor_host.run(
            "activate", "--slot", "B", "--sha256", _digest(b)
        ).returncode
        == 0
    )
    pending = supervisor_host.state()
    supervisor_host.readiness(
        generation=int(pending["generation"]), slot="B", digest=_digest(b)
    )
    supervisor_host.systemctl.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$VONK_TEST_SYSTEMCTL_ACTIONS\"\n"
        "if [ \"${1:-}\" = is-failed ]; then exit 0; fi\n"
        "exit 0\n"
    )
    supervisor_host.systemctl.chmod(0o755)

    supervised = supervisor_host.run("supervise")

    assert supervised.returncode != 0
    state = supervisor_host.state()
    assert state["active_slot"] == "A"
    assert state["previous_slot"] == "B"
    assert state["status"] == "stable"
    assert state["activation_deadline"] is None
    assert state["rollback_performed"] is True
    assert not supervisor_host.readiness_path.exists()
    actions = supervisor_host.actions.read_text().splitlines()
    assert actions.index("stop vonk-forge-agent.service") < actions.index(
        "restart vonk-forge-agent.service"
    )


def test_pending_slot_replacement_before_commit_rolls_back(
    supervisor_host: SupervisorHost,
) -> None:
    a = supervisor_host.compile_agent("A", "slot-a")
    b = supervisor_host.compile_readiness_agent("B")
    assert (
        supervisor_host.run(
            "initialize", "--slot", "A", "--sha256", _digest(a)
        ).returncode
        == 0
    )
    assert (
        supervisor_host.run(
            "activate", "--slot", "B", "--sha256", _digest(b)
        ).returncode
        == 0
    )
    replacement = b.with_name(".vonk-forge-agent.commit-race")
    replacement.write_bytes(a.read_bytes())
    replacement.chmod(0o555)
    supervisor_host.environment["VONK_SUPERVISOR_SWAP_SLOT_BEFORE_COMMIT_TEST"] = "1"
    supervisor_host.spawn_agent_from_systemctl()

    supervised = supervisor_host.run("supervise")

    assert supervised.returncode != 0
    state = supervisor_host.state()
    assert state["active_slot"] == "A"
    assert state["expected_sha256"] == _digest(a)
    assert state["rollback_performed"] is True


def test_readiness_replacement_during_consumption_is_not_unlinked(
    supervisor_host: SupervisorHost,
) -> None:
    a = supervisor_host.compile_agent("A", "slot-a")
    b = supervisor_host.compile_readiness_agent("B")
    assert (
        supervisor_host.run(
            "initialize", "--slot", "A", "--sha256", _digest(a)
        ).returncode
        == 0
    )
    assert (
        supervisor_host.run(
            "activate", "--slot", "B", "--sha256", _digest(b)
        ).returncode
        == 0
    )
    pending = supervisor_host.state()
    generation = int(pending["generation"])
    replacement = supervisor_host.readiness_path.with_name("replacement.json")
    replacement.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    replacement.write_text(
        json.dumps(
            {
                "generation": generation - 1,
                "schema_version": 1,
                "sha256": _digest(b),
                "slot": "B",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    replacement.chmod(0o600)
    supervisor_host.environment["VONK_SUPERVISOR_SWAP_READINESS_TEST"] = "1"
    supervisor_host.spawn_agent_from_systemctl()

    supervised = supervisor_host.run("supervise")

    assert supervised.returncode == 0, supervised.stderr
    assert supervisor_host.state()["status"] == "stable"
    assert json.loads(supervisor_host.readiness_path.read_text())["generation"] == (
        generation - 1
    )


def test_readiness_replacement_after_identity_check_survives(
    supervisor_host: SupervisorHost,
) -> None:
    a = supervisor_host.compile_agent("A", "slot-a")
    b = supervisor_host.compile_readiness_agent("B")
    assert (
        supervisor_host.run(
            "initialize", "--slot", "A", "--sha256", _digest(a)
        ).returncode
        == 0
    )
    assert (
        supervisor_host.run(
            "activate", "--slot", "B", "--sha256", _digest(b)
        ).returncode
        == 0
    )
    pending = supervisor_host.state()
    generation = int(pending["generation"])
    replacement = supervisor_host.readiness_path.with_name("replacement.json")
    replacement.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    replacement.write_text(
        json.dumps(
            {
                "generation": generation - 1,
                "schema_version": 1,
                "sha256": _digest(b),
                "slot": "B",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    replacement.chmod(0o600)
    supervisor_host.environment[
        "VONK_SUPERVISOR_SWAP_READINESS_AFTER_STAT_TEST"
    ] = "1"
    supervisor_host.spawn_agent_from_systemctl()

    supervised = supervisor_host.run("supervise")

    assert supervised.returncode == 0, supervised.stderr
    assert supervisor_host.state()["status"] == "stable"
    assert json.loads(supervisor_host.readiness_path.read_text())["generation"] == (
        generation - 1
    )


def test_supervise_releases_writer_lock_so_restarted_agent_can_emit_readiness(
    supervisor_host: SupervisorHost,
) -> None:
    a = supervisor_host.compile_agent("A", "slot-a")
    b = supervisor_host.compile_readiness_agent("B")
    assert (
        supervisor_host.run(
            "initialize", "--slot", "A", "--sha256", _digest(a)
        ).returncode
        == 0
    )
    assert (
        supervisor_host.run(
            "activate", "--slot", "B", "--sha256", _digest(b)
        ).returncode
        == 0
    )
    supervisor_host.readiness_path.parent.mkdir(parents=True, exist_ok=True)
    supervisor_host.readiness_path.parent.chmod(0o700)
    supervisor_host.spawn_agent_from_systemctl()

    supervised = supervisor_host.run("supervise", timeout=3)

    assert supervised.returncode == 0, supervised.stderr
    assert supervisor_host.state()["status"] == "stable"
    assert not supervisor_host.readiness_path.exists()
    assert supervisor_host.challenge_path.read_text() == "0" * 64 + "\n"
    assert supervisor_host.challenge_path.stat().st_mode & 0o777 == 0o600
    assert supervisor_host.challenge_path.stat().st_uid == os.geteuid()


def test_activation_rejects_readiness_from_wrong_service_pid(
    supervisor_host: SupervisorHost,
) -> None:
    a = supervisor_host.compile_agent("A", "slot-a")
    b = supervisor_host.compile_readiness_agent("B", pid_delta=1)
    assert supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", _digest(a)
    ).returncode == 0
    assert supervisor_host.run(
        "activate", "--slot", "B", "--sha256", _digest(b)
    ).returncode == 0
    supervisor_host.spawn_agent_from_systemctl()

    supervised = supervisor_host.run("supervise", timeout=3)

    assert supervised.returncode != 0
    assert supervisor_host.state()["active_slot"] == "A"
    assert supervisor_host.state()["rollback_performed"] is True


def test_main_pid_change_after_readiness_verification_rolls_back(
    supervisor_host: SupervisorHost,
) -> None:
    a = supervisor_host.compile_agent("A", "slot-a")
    b = supervisor_host.compile_readiness_agent("B")
    assert supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", _digest(a)
    ).returncode == 0
    assert supervisor_host.run(
        "activate", "--slot", "B", "--sha256", _digest(b)
    ).returncode == 0
    show_count = supervisor_host.root / "show-count"
    supervisor_host.systemctl.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$VONK_TEST_SYSTEMCTL_ACTIONS\"\n"
        'if [ "${1:-}" = stop ]; then rm -f "$VONK_TEST_SERVICE_PID"; fi\n'
        'if [ "${1:-}" = restart ]; then "$VONK_TEST_SUPERVISOR" run-agent & printf \'%s\\n\' "$!" > "$VONK_TEST_SERVICE_PID"; fi\n'
        'if [ "${1:-}" = show ]; then\n'
        '  count=0; if [ -s "$VONK_TEST_SHOW_COUNT" ]; then count=$(sed -n \'1p\' "$VONK_TEST_SHOW_COUNT"); fi; count=$((count + 1)); printf \'%s\\n\' "$count" > "$VONK_TEST_SHOW_COUNT"\n'
        '  if [ "$count" -le 2 ]; then cat "$VONK_TEST_SERVICE_PID"; else printf \'0\\n\'; fi\n'
        "fi\n"
        'if [ "${1:-}" = is-failed ]; then exit 1; fi\n'
        "exit 0\n"
    )
    supervisor_host.systemctl.chmod(0o755)
    supervisor_host.environment.update(
        {
            "VONK_TEST_SERVICE_PID": str(supervisor_host.service_pid_path),
            "VONK_TEST_SHOW_COUNT": str(show_count),
            "VONK_TEST_SUPERVISOR": str(SUPERVISOR),
        }
    )

    supervised = supervisor_host.run("supervise", timeout=3)

    assert supervised.returncode != 0
    assert supervisor_host.state()["active_slot"] == "A"
    assert supervisor_host.state()["rollback_performed"] is True


def test_activation_rejects_matching_digest_from_wrong_executable_inode(
    supervisor_host: SupervisorHost,
) -> None:
    a = supervisor_host.compile_agent("A", "slot-a")
    b = supervisor_host.compile_readiness_agent("B")
    impostor = supervisor_host.root / "impostor-agent"
    shutil.copyfile(b, impostor)
    impostor.chmod(0o555)
    assert _digest(impostor) == _digest(b)
    assert supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", _digest(a)
    ).returncode == 0
    assert supervisor_host.run(
        "activate", "--slot", "B", "--sha256", _digest(b)
    ).returncode == 0
    pending = supervisor_host.state()
    supervisor_host.systemctl.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$VONK_TEST_SYSTEMCTL_ACTIONS\"\n"
        'if [ "${1:-}" = stop ]; then rm -f "$VONK_TEST_SERVICE_PID"; fi\n'
        'if [ "${1:-}" = restart ]; then\n'
        '  CREDENTIALS_DIRECTORY="$VONK_TEST_CREDENTIALS" VONK_AGENT_SUPERVISOR_GENERATION="$VONK_TEST_GENERATION" VONK_AGENT_SUPERVISOR_SLOT=B VONK_AGENT_SUPERVISOR_SHA256="$VONK_TEST_SHA256" "$VONK_TEST_IMPOSTOR" &\n'
        '  printf \'%s\\n\' "$!" > "$VONK_TEST_SERVICE_PID"\n'
        "fi\n"
        'if [ "${1:-}" = show ]; then cat "$VONK_TEST_SERVICE_PID" 2>/dev/null || printf \'0\\n\'; fi\n'
        'if [ "${1:-}" = is-failed ]; then\n'
        '  if [ -s "$VONK_TEST_SERVICE_PID" ] && kill -0 "$(cat "$VONK_TEST_SERVICE_PID")" 2>/dev/null; then exit 1; fi\n'
        '  exit 0\n'
        "fi\n"
        "exit 0\n"
    )
    supervisor_host.systemctl.chmod(0o755)
    supervisor_host.environment.update(
        {
            "VONK_TEST_CREDENTIALS": str(supervisor_host.challenge_path.parent),
            "VONK_TEST_GENERATION": str(pending["generation"]),
            "VONK_TEST_IMPOSTOR": str(impostor),
            "VONK_TEST_SERVICE_PID": str(supervisor_host.service_pid_path),
            "VONK_TEST_SHA256": _digest(b),
        }
    )

    supervised = supervisor_host.run("supervise", timeout=5)

    assert supervised.returncode != 0
    assert supervisor_host.state()["active_slot"] == "A"
    assert supervisor_host.state()["rollback_performed"] is True


def test_candidate_crash_without_authenticated_readiness_rolls_back(
    supervisor_host: SupervisorHost,
) -> None:
    a = supervisor_host.compile_agent("A", "slot-a")
    b = supervisor_host.compile_agent("B", "candidate-crashes-before-readiness")
    assert supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", _digest(a)
    ).returncode == 0
    assert supervisor_host.run(
        "activate", "--slot", "B", "--sha256", _digest(b)
    ).returncode == 0
    supervisor_host.spawn_agent_from_systemctl()

    supervised = supervisor_host.run("supervise", timeout=3)

    assert supervised.returncode != 0
    state = supervisor_host.state()
    assert state["active_slot"] == "A"
    assert state["rollback_performed"] is True
    assert supervisor_host.challenge_path.read_text() == "0" * 64 + "\n"


def test_stale_challenge_reuse_after_readiness_clear_is_rejected(
    supervisor_host: SupervisorHost,
) -> None:
    a = supervisor_host.compile_agent("A", "slot-a")
    b = supervisor_host.compile_agent("B", "slot-b")
    assert supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", _digest(a)
    ).returncode == 0
    assert supervisor_host.run(
        "activate", "--slot", "B", "--sha256", _digest(b)
    ).returncode == 0
    captured = supervisor_host.root / "captured-activation-challenge"
    supervisor_host.systemctl.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$VONK_TEST_SYSTEMCTL_ACTIONS\"\n"
        'if [ "${1:-}" = restart ] && [ ! -e "$VONK_TEST_CAPTURED_CHALLENGE" ]; then cp "$VONK_TEST_CHALLENGE" "$VONK_TEST_CAPTURED_CHALLENGE"; fi\n'
        'if [ "${1:-}" = is-failed ]; then exit 0; fi\n'
        "exit 0\n"
    )
    supervisor_host.systemctl.chmod(0o755)
    supervisor_host.environment["VONK_TEST_CAPTURED_CHALLENGE"] = str(captured)
    supervisor_host.environment["VONK_TEST_CHALLENGE"] = str(
        supervisor_host.challenge_path
    )
    assert supervisor_host.run("supervise").returncode != 0
    stale_challenge = captured.read_text().strip()
    assert stale_challenge != "0" * 64
    assert supervisor_host.run(
        "activate", "--slot", "B", "--sha256", _digest(b)
    ).returncode == 0
    pending = supervisor_host.state()
    stale = supervisor_host.root / "stale-readiness.json"
    stale.write_text(
        json.dumps(
            {
                "challenge": stale_challenge,
                "generation": pending["generation"],
                "pid": os.getpid(),
                "schema_version": 2,
                "sha256": _digest(b),
                "slot": "B",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    supervisor_host.systemctl.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$VONK_TEST_SYSTEMCTL_ACTIONS\"\n"
        'if [ "${1:-}" = restart ]; then cp "$VONK_TEST_STALE_READINESS" "$VONK_TEST_READINESS"; chmod 0600 "$VONK_TEST_READINESS"; fi\n'
        'if [ "${1:-}" = is-failed ]; then exit 0; fi\n'
        "exit 0\n"
    )
    supervisor_host.systemctl.chmod(0o755)
    supervisor_host.environment["VONK_TEST_STALE_READINESS"] = str(stale)
    supervisor_host.environment["VONK_TEST_READINESS"] = str(
        supervisor_host.readiness_path
    )

    supervised = supervisor_host.run("supervise")

    assert supervised.returncode != 0
    assert supervisor_host.state()["active_slot"] == "A"
    assert supervisor_host.state()["rollback_performed"] is True


def test_old_agent_readiness_racing_service_stop_is_cleared(
    supervisor_host: SupervisorHost,
) -> None:
    a = supervisor_host.compile_agent("A", "slot-a")
    b = supervisor_host.compile_agent("B", "slot-b")
    assert supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", _digest(a)
    ).returncode == 0
    assert supervisor_host.run(
        "activate", "--slot", "B", "--sha256", _digest(b)
    ).returncode == 0
    pending = supervisor_host.state()
    raced = supervisor_host.root / "old-agent-race.json"
    raced.write_text(
        json.dumps(
            {
                "challenge": supervisor_host.challenge_path.read_text().strip(),
                "generation": pending["generation"],
                "pid": os.getpid(),
                "schema_version": 2,
                "sha256": _digest(b),
                "slot": "B",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    supervisor_host.systemctl.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >> \"$VONK_TEST_SYSTEMCTL_ACTIONS\"\n"
        'if [ "${1:-}" = stop ]; then cp "$VONK_TEST_RACED_READINESS" "$VONK_TEST_READINESS"; chmod 0600 "$VONK_TEST_READINESS"; fi\n'
        'if [ "${1:-}" = is-failed ]; then exit 0; fi\n'
        "exit 0\n"
    )
    supervisor_host.systemctl.chmod(0o755)
    supervisor_host.environment["VONK_TEST_RACED_READINESS"] = str(raced)
    supervisor_host.environment["VONK_TEST_READINESS"] = str(
        supervisor_host.readiness_path
    )

    supervised = supervisor_host.run("supervise")

    assert supervised.returncode != 0
    assert supervisor_host.state()["active_slot"] == "A"
    assert not supervisor_host.readiness_path.exists()


def test_pending_invalid_active_slot_rolls_back_to_verified_previous(
    supervisor_host: SupervisorHost,
) -> None:
    a = supervisor_host.compile_agent("A", "slot-a")
    b = supervisor_host.compile_agent("B", "slot-b")
    digest_a, digest_b = _digest(a), _digest(b)
    assert (
        supervisor_host.run(
            "initialize", "--slot", "A", "--sha256", digest_a
        ).returncode
        == 0
    )
    assert (
        supervisor_host.run("activate", "--slot", "B", "--sha256", digest_b).returncode
        == 0
    )
    b.unlink()

    supervised = supervisor_host.run("supervise")

    assert supervised.returncode != 0
    state = supervisor_host.state()
    assert state["active_slot"] == "A"
    assert state["expected_sha256"] == digest_a
    assert state["rollback_performed"] is True


def test_corrupt_state_and_both_invalid_slots_fail_closed(
    supervisor_host: SupervisorHost,
) -> None:
    a = supervisor_host.compile_agent("A", "slot-a")
    assert (
        supervisor_host.run(
            "initialize", "--slot", "A", "--sha256", _digest(a)
        ).returncode
        == 0
    )
    supervisor_host.state_path.write_text('{"schema_version":1,"schema_version":1}\n')

    corrupt = supervisor_host.run("run-agent")

    assert corrupt.returncode != 0
    assert corrupt.stdout == ""


def test_nonfinite_activation_deadline_fails_closed(
    supervisor_host: SupervisorHost,
) -> None:
    a = supervisor_host.compile_agent("A", "slot-a")
    b = supervisor_host.compile_agent("B", "slot-b")
    assert (
        supervisor_host.run(
            "initialize", "--slot", "A", "--sha256", _digest(a)
        ).returncode
        == 0
    )
    assert (
        supervisor_host.run(
            "activate", "--slot", "B", "--sha256", _digest(b)
        ).returncode
        == 0
    )
    state = supervisor_host.state()
    state["activation_deadline"] = float("nan")
    supervisor_host.state_path.write_text(
        json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n"
    )

    launched = supervisor_host.run("run-agent")

    assert launched.returncode != 0
    assert launched.stdout == ""


def test_state_generation_matches_readiness_reporter_bound(
    supervisor_host: SupervisorHost,
) -> None:
    agent = supervisor_host.compile_agent("A", "slot-a")
    assert (
        supervisor_host.run(
            "initialize", "--slot", "A", "--sha256", _digest(agent)
        ).returncode
        == 0
    )
    state = supervisor_host.state()
    state["generation"] = 1_000_000_000
    supervisor_host.state_path.write_text(
        json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n"
    )

    rejected = supervisor_host.run("run-agent")

    assert rejected.returncode != 0


def test_supervisor_interface_has_no_path_or_shell_argument(
    supervisor_host: SupervisorHost,
) -> None:
    result = supervisor_host.run(
        "activate", "--slot", "B", "--sha256", "a" * 64, "--path", "/tmp/x"
    )
    shell = supervisor_host.run("/bin/sh", "-c", "id")

    assert result.returncode == shell.returncode == 64


def test_arm64_elf_is_validated_without_execution(
    supervisor_host: SupervisorHost,
) -> None:
    if platform.machine() not in {"x86_64", "AMD64"}:
        pytest.skip("foreign ARM64 validation is exercised from x86_64")
    target = supervisor_host.host_root / "opt/vonk-forge/agent-slots/A/vonk-forge-agent"
    target.parent.mkdir(parents=True)
    # ELF64 little-endian, ET_EXEC, EM_AARCH64; no executable body is needed.
    target.write_bytes(
        b"\x7fELF\x02\x01\x01" + b"\0" * 9 + b"\x02\0\xb7\0" + b"\0" * 44
    )
    target.chmod(0o555)
    supervisor_host.write_identity(target)
    supervisor_host.environment["VONK_SUPERVISOR_TEST_ARCH"] = "aarch64"

    initialized = supervisor_host.run(
        "initialize", "--slot", "A", "--sha256", _digest(target)
    )

    assert initialized.returncode == 0, initialized.stderr


def test_systemd_units_verify_and_enforce_split_privilege_hardening(
    tmp_path: Path,
) -> None:
    unit_root = tmp_path / "unit-root"
    unit_directory = unit_root / "etc/systemd/system"
    executable_directory = unit_root / "usr/libexec"
    shutil.copytree("/usr/lib/systemd/system", unit_root / "usr/lib/systemd/system")
    unit_directory.mkdir(parents=True)
    executable_directory.mkdir(parents=True)
    units = (
        AGENT_UNIT,
        SUPERVISOR_UNIT,
        ACTIVATION_UNIT,
        ACTIVATION_PATH,
        ROLLBACK_UNIT,
        ROLLBACK_PATH,
    )
    for source in units:
        shutil.copy2(source, unit_directory / source.name)
    shutil.copy2("/bin/true", executable_directory / "vonk-agent-supervisor")
    verified = subprocess.run(
        [
            "systemd-analyze",
            "verify",
            f"--root={unit_root}",
            *(str(unit_directory / unit.name) for unit in units),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr
    effective: dict[str, dict[str, object]] = {}
    for unit in (AGENT_UNIT, SUPERVISOR_UNIT, ACTIVATION_UNIT, ROLLBACK_UNIT):
        analyzed = subprocess.run(
            [
                "systemd-analyze",
                "security",
                "--offline=yes",
                "--json=short",
                str(unit),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        assert analyzed.returncode == 0, analyzed.stderr
        effective[unit.name] = {
            assessment["json_field"]: assessment["set"]
            for assessment in json.loads(analyzed.stdout)
        }
    assert effective[AGENT_UNIT.name]["UserOrDynamicUser"] is True
    assert effective[AGENT_UNIT.name]["NoNewPrivileges"] is True
    assert effective[AGENT_UNIT.name]["ProtectSystem"] is True
    assert effective[AGENT_UNIT.name]["AmbientCapabilities"] is True
    assert effective[AGENT_UNIT.name]["CapabilityBoundingSet_CAP_SYS_PTRACE"] is True
    assert effective[SUPERVISOR_UNIT.name]["PrivateNetwork"] is True
    assert effective[SUPERVISOR_UNIT.name]["NoNewPrivileges"] is True
    assert effective[SUPERVISOR_UNIT.name]["ProtectSystem"] is True
    assert effective[SUPERVISOR_UNIT.name]["AmbientCapabilities"] is True
    assert (
        effective[SUPERVISOR_UNIT.name]["CapabilityBoundingSet_CAP_SYS_PTRACE"]
        is False
    )
    assert effective[ACTIVATION_UNIT.name]["PrivateNetwork"] is True
    assert effective[ACTIVATION_UNIT.name]["NoNewPrivileges"] is True
    assert effective[ACTIVATION_UNIT.name]["ProtectSystem"] is True
    assert effective[ROLLBACK_UNIT.name]["PrivateNetwork"] is True
    assert effective[ROLLBACK_UNIT.name]["NoNewPrivileges"] is True
    assert effective[ROLLBACK_UNIT.name]["ProtectSystem"] is True
    assert (
        effective[ROLLBACK_UNIT.name][
            "CapabilityBoundingSet_CAP_CHOWN_FSETID_SETFCAP"
        ]
        is True
    )
    assert (
        effective[SUPERVISOR_UNIT.name][
            "CapabilityBoundingSet_CAP_CHOWN_FSETID_SETFCAP"
        ]
        is False
    )
    agent = AGENT_UNIT.read_text()
    supervisor = SUPERVISOR_UNIT.read_text()
    activation = ACTIVATION_UNIT.read_text()
    activation_path = ACTIVATION_PATH.read_text()
    rollback = ROLLBACK_UNIT.read_text()
    rollback_path = ROLLBACK_PATH.read_text()
    for literal in (
        "User=vonk-agent",
        "Group=vonk-agent",
        "SupplementaryGroups=",
        "PartOf=vonk-forge-agent-supervisor.service",
        "ExecStart=/usr/libexec/vonk-agent-supervisor run-agent",
        "LoadCredential=activation-challenge:/run/vonk-forge-agent-supervisor/activation-challenge",
        "UMask=0077",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "PrivateTmp=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "MemoryDenyWriteExecute=yes",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "ReadWritePaths=/var/lib/vonk-forge-agent /var/lib/vonk-forge/releases /var/lib/vonk-forge/release-staging /run/vonk-forge-agent",
    ):
        assert literal in agent
    assert "docker" not in agent.lower()
    for literal in (
        "ExecStart=/usr/libexec/vonk-agent-supervisor supervise",
        "UMask=0077",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=CAP_CHOWN CAP_DAC_READ_SEARCH CAP_DAC_OVERRIDE CAP_SYS_PTRACE",
        "AmbientCapabilities=",
        "PrivateNetwork=yes",
        "ProtectSystem=strict",
    ):
        assert literal in supervisor
    assert "User=" not in supervisor
    for literal in (
        "ExecStart=/usr/libexec/vonk-agent-supervisor apply-request",
        "NoNewPrivileges=yes",
        "PrivateNetwork=yes",
        "ProtectSystem=strict",
        "ReadOnlyPaths=/var/lib/vonk-forge-agent/update-staging /etc/vonk-forge-agent/update-authority.json /usr/bin/openssl /usr/libexec/vonk-agent-supervisor",
        "ReadWritePaths=/opt/vonk-forge/agent-slots /var/lib/vonk-forge-agent-supervisor /run/vonk-forge-agent",
    ):
        assert literal in activation
    assert "User=" not in activation
    assert "PathExists=/run/vonk-forge-agent/activation-request.json" in activation_path
    assert "Unit=vonk-forge-agent-activation.service" in activation_path
    assert "TriggerLimitIntervalSec=60s" in activation_path
    assert "TriggerLimitBurst=3" in activation_path
    for literal in (
        "ExecStart=/usr/libexec/vonk-agent-supervisor apply-rollback-request",
        "CapabilityBoundingSet=CAP_DAC_READ_SEARCH CAP_DAC_OVERRIDE",
        "NoNewPrivileges=yes",
        "PrivateNetwork=yes",
        "ProtectSystem=strict",
        "ReadOnlyPaths=/opt/vonk-forge/agent-slots /usr/libexec/vonk-agent-supervisor",
        "ReadWritePaths=/var/lib/vonk-forge-agent-supervisor /run/vonk-forge-agent",
    ):
        assert literal in rollback
    assert "User=" not in rollback
    assert "PathExists=/run/vonk-forge-agent/rollback-request.json" in rollback_path
    assert "Unit=vonk-forge-agent-rollback.service" in rollback_path
    assert "TriggerLimitIntervalSec=60s" in rollback_path
    assert "TriggerLimitBurst=3" in rollback_path


def test_agent_effective_device_policy_is_closed_and_read_only() -> None:
    analyzed = subprocess.run(
        [
            "systemd-analyze",
            "security",
            "--offline=yes",
            "--json=short",
            str(AGENT_UNIT),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert analyzed.returncode == 0, analyzed.stderr
    assessments = {
        assessment["json_field"]: assessment
        for assessment in json.loads(analyzed.stdout)
    }
    assert assessments["PrivateDevices"]["set"] is True
    device_acl = assessments["DeviceAllow"]["description"].split(": ", 1)[1]
    assert set(device_acl.split()) == {
        "/dev/nvidia-caps/nvidia-cap2:r",
        "/dev/nvidia-modeset:r",
        "/dev/nvidia-uvm-tools:r",
        "/dev/nvidia-uvm:r",
        "/dev/nvidia0:r",
        "/dev/nvidiactl:r",
        "char-rtc:r",
    }
    directives = AGENT_UNIT.read_text().splitlines()
    assert "DevicePolicy=closed" in directives
    bind = next(
        line.removeprefix("BindReadOnlyPaths=").split()
        for line in directives
        if line.startswith("BindReadOnlyPaths=")
    )
    assert set(bind) == {
        "-/dev/nvidia-caps/nvidia-cap2",
        "-/dev/nvidia-modeset",
        "-/dev/nvidia-uvm-tools",
        "-/dev/nvidia-uvm",
        "-/dev/nvidia0",
        "-/dev/nvidiactl",
    }


def test_installed_systemd_harness_verifies_units_by_installed_name() -> None:
    assert SYSTEMD_VERIFY.is_file()

    verified = subprocess.run(
        [str(SYSTEMD_VERIFY), "--json"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert verified.returncode == 0, verified.stderr
    report = json.loads(verified.stdout)
    assert report["verify"] == "passed"
    assert set(report["units"]) == {
        "vonk-forge-agent.service",
        "vonk-forge-agent-supervisor.service",
        "vonk-forge-agent-activation.service",
        "vonk-forge-agent-activation.path",
        "vonk-forge-agent-rollback.service",
        "vonk-forge-agent-rollback.path",
        "vonk-forge-package-helper.service",
        "vonk-forge-package-helper.socket",
    }
    assert set(report["security_units"]) == {
        "vonk-forge-agent.service",
        "vonk-forge-agent-supervisor.service",
        "vonk-forge-agent-activation.service",
        "vonk-forge-agent-rollback.service",
        "vonk-forge-package-helper.service",
    }
    assert report["security_units"]["vonk-forge-agent-supervisor.service"][
        "cap_sys_ptrace"
    ] is True
    assert report["security_units"]["vonk-forge-agent.service"][
        "cap_sys_ptrace"
    ] is False
    assert all(
        unit["ambient_capabilities"] is False
        for unit in report["security_units"].values()
    )
    assert all(
        unit["exposure"] == "OK" for unit in report["security_units"].values()
    )
    assert report["package_helper_socket"] == {
        "directory_mode": "0711",
        "group": "vonk-agent",
        "listen_stream": "/run/vonk-forge-package-helper/package-helper.sock",
        "runtime_owner": "root",
    }


def test_rust_supervisor_is_stable_outside_slots_and_units_keep_agent_unprivileged() -> None:
    packaging = PROJECT.parent / "packaging/systemd"
    rust_agent = (packaging / "vonk-forge-agent.service").read_text().splitlines()
    rust_supervisor = (
        packaging / "vonk-forge-agent-supervisor.service"
    ).read_text().splitlines()

    assert "User=vonk-agent" in rust_agent
    assert "SupplementaryGroups=" in rust_agent
    assert "NoNewPrivileges=no" in rust_agent
    assert (
        "CapabilityBoundingSet=CAP_DAC_OVERRIDE CAP_SETGID CAP_SETUID CAP_SYS_ADMIN"
        in rust_agent
    )
    assert "AmbientCapabilities=" in rust_agent
    assert "ProtectProc=default" in rust_agent
    assert "ProtectHome=no" in rust_agent
    assert "InaccessiblePaths=/home /root -/run/docker.sock" in rust_agent
    assert "BindReadOnlyPaths=/run/user" not in rust_agent
    assert "RestrictSUIDSGID=yes" not in rust_agent
    assert (
        "ExecStart=/usr/lib/vonk-forge/vonk-agent-supervisor run-agent"
        in rust_agent
    )
    assert (
        "LoadCredential=activation-challenge:"
            "/var/lib/vonk-forge/supervisor/activation-challenge"
        in rust_agent
    )
    assert (
        "ExecStart=/usr/lib/vonk-forge/vonk-agent-supervisor supervise"
        in rust_supervisor
    )
    assert "PrivateNetwork=yes" in rust_supervisor
    assert not any("/slots/" in line for line in rust_supervisor if line.startswith("ExecStart="))
