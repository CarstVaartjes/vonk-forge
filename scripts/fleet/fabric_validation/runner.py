"""Runner for fabric validation."""

from __future__ import annotations

import shlex
import subprocess
from typing import Any

from cluster_profiles.ssh_transport import select_transport_binary

from .common import FABRIC_WORKER_ALIAS, SSH_OPTIONS, GateError, Host


def command_record(
    command: list[str], completed: subprocess.CompletedProcess[str]
) -> dict[str, Any]:
    return {
        "command": shlex.join(command),
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


class Runner:
    def __init__(self, head: Host, worker: Host, evidence: list[dict[str, Any]]):
        self.head = head
        self.worker = worker
        self.evidence = evidence
        self.ssh_bin = select_transport_binary("ssh")

    def local(
        self, command: list[str], *, check: bool = True, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                command,
                input=input_text,
                capture_output=True,
                text=True,
                check=False,
                timeout=600,
            )
        except subprocess.TimeoutExpired as error:
            completed = subprocess.CompletedProcess(
                command,
                124,
                (error.stdout or b"").decode(errors="replace")
                if isinstance(error.stdout, bytes)
                else (error.stdout or ""),
                "fabric diagnostic exceeded its 600s command budget; outcome unconfirmed",
            )
        self.evidence.append(command_record(command, completed))
        if check and completed.returncode:
            raise GateError(
                f"command failed ({completed.returncode}): {shlex.join(command)}"
            )
        return completed

    def remote(
        self, host: str, shell_command: str, *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        # Supplying the body on stdin avoids OpenSSH's lossy joining of remote
        # argv and makes multi-line safety checks unambiguous.
        return self.local(
            [self.ssh_bin, *SSH_OPTIONS, host, "bash", "-s"],
            check=check,
            input_text=shell_command,
        )

    def worker_via_fabric(
        self, shell_command: str, *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        nested = "printf %s " + shlex.quote(shell_command) + " | ssh "
        nested += " ".join(
            shlex.quote(item) for item in (*SSH_OPTIONS, FABRIC_WORKER_ALIAS)
        )
        nested += " bash -s"
        return self.remote(self.head.ssh_alias, nested, check=check)
