"""Fault injection for the PostgreSQL backup loop in the shipped entrypoint.

The loop runs under dash, the image's /bin/sh, with the PostgreSQL tools
stubbed and its fixed mount paths relocated into a temporary directory.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
ENTRYPOINT = ROOT / "deploy/compose/postgres/entrypoint.sh"

STUBS = {
    "gosu": 'shift\nexec "$@"\n',
    "initdb": "exit 0\n",
    "pg_ctl": "exit 0\n",
    "install": 'for last; do :; done\nexec mkdir -p "$last"\n',
    "pg_dumpall": "printf '%s\\n' 'CREATE DATABASE litellm;'\n",
    # The verification query answers how many expected databases exist.
    "psql": (
        'case "$*" in\n'
        '  *-tAc*) [ "$FAULT" = verify ] && echo 1 || echo 2 ;;\n'
        "  *) cat >/dev/null ;;\n"
        "esac\n"
    ),
    "chown": (
        'if [ "$FAULT" = publish ] && mkdir "$FAULTS/chown" 2>/dev/null; then\n'
        "  exit 1\n"
        "fi\n"
    ),
    "mv": (
        "for last; do :; done\n"
        'case "$last" in\n'
        "  */last-successful-backup.epoch)\n"
        '    if [ "$FAULT" = success-marker ] && mkdir "$FAULTS/mv" 2>/dev/null; then\n'
        "      exit 1\n"
        "    fi\n"
        "    ;;\n"
        "esac\n"
        'exec "$REAL_MV" "$@"\n'
    ),
    "sleep": 'echo "$1" >>"$SLEEPS"\nexec "$REAL_SLEEP" 0.05\n',
}


def _backup_loop(root: Path) -> str:
    text = ENTRYPOINT.read_text(encoding="utf-8")
    match = re.search(
        r"^backup_loop\(\) [({]\n.*?^[)}]\n", text, re.MULTILINE | re.DOTALL
    )
    assert match, "backup_loop is not defined in the entrypoint"
    return re.sub(
        r"(?<![\w.])/(backups|state|offhost)\b",
        lambda path: f"{root}/{path.group(1)}",
        match.group(0),
    )


def _start(tmp_path: Path, fault: str) -> tuple[subprocess.Popen[str], Path]:
    dash = shutil.which("dash")
    if dash is None:
        if os.environ.get("CI") == "true":
            pytest.fail("dash is required in CI")
        pytest.skip("dash is not installed")
    for directory in ("backups", "state", "offhost", "faults", "bin"):
        (tmp_path / directory).mkdir()
    for name, body in STUBS.items():
        stub = tmp_path / "bin" / name
        stub.write_text("#!/bin/sh\n" + body, encoding="ascii")
        stub.chmod(0o755)
    script = tmp_path / "loop.sh"
    # The entrypoint runs under `set -eu`, which the loop inherits.
    script.write_text(
        "set -eu\n"
        + _backup_loop(tmp_path)
        + 'backup_loop &\nworker=$!\necho "$worker" >"$WORKER_PID"\nwait "$worker"\n',
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "FAULT": fault,
        "WORKER_PID": str(tmp_path / "worker.pid"),
        "FAULTS": str(tmp_path / "faults"),
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "POSTGRES_USER": "control",
        "REAL_MV": str(shutil.which("mv")),
        "REAL_SLEEP": str(shutil.which("sleep")),
        "SLEEPS": str(tmp_path / "sleeps"),
        "VONK_BACKUP_OFFHOST_PATH": "/srv/offhost",
    }
    loop = subprocess.Popen(
        [dash, str(script)],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    return loop, tmp_path / "state"


def _wait_for(condition: Callable[[], bool], loop: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 10
    while not condition():
        assert loop.poll() is None, "the backup loop exited"
        assert time.monotonic() < deadline, "the backup loop made no progress"
        time.sleep(0.05)


def _terminate(loop: subprocess.Popen[str]) -> str:
    try:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(loop.pid, signal.SIGTERM)
        _, stderr = loop.communicate(timeout=10)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(loop.pid, signal.SIGKILL)
    return stderr


@pytest.mark.parametrize("fault", ["publish", "success-marker"])
def test_a_failed_attempt_is_retried_until_a_backup_is_verified(
    tmp_path: Path, fault: str
) -> None:
    loop, state = _start(tmp_path, fault)
    try:
        _wait_for((state / "last-successful-backup.epoch").is_file, loop)
        assert loop.poll() is None
    finally:
        stderr = _terminate(loop)

    assert f"failed at step {fault}" in stderr
    assert "300" in (tmp_path / "sleeps").read_text(encoding="ascii").split()
    assert (state / "last-backup-restore-verification.epoch").is_file()
    assert list((tmp_path / "backups").glob("postgres-*.sql.gz"))
    assert list((tmp_path / "offhost").glob("postgres-*.sql.gz"))


def test_a_failed_restore_verification_never_marks_a_backup_successful(
    tmp_path: Path,
) -> None:
    loop, state = _start(tmp_path, "verify")
    sleeps = tmp_path / "sleeps"
    try:
        _wait_for(
            lambda: (
                sleeps.is_file()
                and sleeps.read_text(encoding="ascii").split().count("300") >= 2
            ),
            loop,
        )
        assert loop.poll() is None
    finally:
        stderr = _terminate(loop)

    assert "failed at step restore-verification" in stderr
    assert not (state / "last-successful-backup.epoch").exists()
    assert not (state / "last-backup-restore-verification.epoch").exists()


def test_signalling_the_background_pid_runs_the_backup_cleanup(tmp_path: Path) -> None:
    # Catches dash's extra outer fork for a background subshell-body function:
    # killing $! must reach the loop's trap, not abandon it and its temporary dump.
    loop, state = _start(tmp_path, "none")
    try:
        _wait_for((state / "last-successful-backup.epoch").is_file, loop)
        temporary = tmp_path / "backups/.postgres-backup.tmp"
        temporary.write_text("interrupted dump", encoding="ascii")
        worker = int((tmp_path / "worker.pid").read_text())
        os.kill(worker, signal.SIGTERM)
        loop.wait(timeout=10)
        assert not temporary.exists(), "the loop's signal trap never ran"
    finally:
        _terminate(loop)
