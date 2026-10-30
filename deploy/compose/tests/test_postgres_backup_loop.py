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
    # Records what is on disk while the dump streams, then dumps (or fails).
    "pg_dumpall": (
        'ls -d "$BACKUPS"/.[!.]* "$OFFHOST"/.[!.]* >"$LISTING" 2>/dev/null\n'
        'cat "$BACKUPS/.postgres-backup.tmp.gz" >"$LISTING.gz" 2>/dev/null\n'
        # The dump runs in a brace group that records its status afterwards;
        # killing that group simulates a dump that never reports a status.
        '[ "$FAULT" = nostatus ] && kill -9 "$PPID"\n'
        '[ "$FAULT" = emptystatus ] && : >"$BACKUPS/.postgres-backup.tmp.status" && kill -9 "$PPID"\n'
        "printf '%s\\n' 'CREATE DATABASE litellm;'\n"
        '[ "$FAULT" = dump ] && exit 1\n'
        "exit 0\n"
    ),
    # POSIX df output; /tmp is the restore scratch location.
    "df": (
        'case "$*" in\n'
        "  */tmp) kb=${FREE_TMP_KB:-100000000} ;;\n"
        "  *) kb=${FREE_KB:-100000000} ;;\n"
        "esac\n"
        '[ -n "${DF_UNKNOWN:-}" ] && exit 1\n'
        "echo 'Filesystem 1024-blocks Used Available Capacity Mounted on'\n"
        'echo "stub 1 1 $kb 1% /"\n'
    ),
    # The verification query answers how many expected databases exist.
    "psql": (
        'case "$*" in\n'
        "  *pg_database_size*) echo 1000 ;;\n"
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
    # Compresses like gzip except that the FAULT=gzip compression fails (ENOSPC).
    "gzip": (
        '[ "$FAULT" = gzip ] && [ "$*" = "-c" ] && exit 1\nexec "$REAL_GZIP" "$@"\n'
    ),
    "sleep": 'echo "$1" >>"$SLEEPS"\nexec "$REAL_SLEEP" "${LOOP_TEST_SLEEP:-0.05}"\n',
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


def _start(
    tmp_path: Path,
    fault: str,
    *,
    sleep_seconds: str = "0.05",
    seed: Callable[[Path], None] | None = None,
    extra_environment: dict[str, str] | None = None,
) -> tuple[subprocess.Popen[str], Path]:
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
    if seed is not None:
        seed(tmp_path)
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
        "BACKUPS": str(tmp_path / "backups"),
        "OFFHOST": str(tmp_path / "offhost"),
        "REAL_GZIP": str(shutil.which("gzip")),
        "LISTING": str(tmp_path / "listing"),
        **(extra_environment or {}),
        "WORKER_PID": str(tmp_path / "worker.pid"),
        "FAULTS": str(tmp_path / "faults"),
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "POSTGRES_USER": "control",
        "REAL_MV": str(shutil.which("mv")),
        "REAL_SLEEP": str(shutil.which("sleep")),
        "SLEEPS": str(tmp_path / "sleeps"),
        "LOOP_TEST_SLEEP": sleep_seconds,
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
    loop, state = _start(tmp_path, "none", sleep_seconds="30")
    try:
        _wait_for((state / "last-successful-backup.epoch").is_file, loop)
        temporary = tmp_path / "backups/.postgres-backup.tmp.gz"
        temporary.write_text("interrupted dump", encoding="ascii")
        worker = int((tmp_path / "worker.pid").read_text())
        os.kill(worker, signal.SIGTERM)
        loop.wait(timeout=10)
        assert not temporary.exists(), "the loop's signal trap never ran"
    finally:
        _terminate(loop)


def _run_until_marker(loop: subprocess.Popen[str], state: Path) -> str:
    try:
        _wait_for((state / "last-successful-backup.epoch").is_file, loop)
    except BaseException:
        _terminate(loop)
        raise
    return _terminate(loop)


def test_the_dump_streams_into_gzip_without_an_uncompressed_temporary(
    tmp_path: Path,
) -> None:
    loop, state = _start(tmp_path, "none", sleep_seconds="30")
    _run_until_marker(loop, state)

    # While pg_dumpall ran, no uncompressed dump file existed on the volume.
    listing = (tmp_path / "listing").read_text(encoding="ascii").split()
    assert ".postgres-backup.tmp" not in listing
    assert (state / "last-backup-restore-verification.epoch").is_file()


def test_temporaries_left_by_a_killed_run_are_removed_at_start(tmp_path: Path) -> None:
    leftovers = (
        "backups/.postgres-backup.tmp.gz",
        "backups/.postgres-backup.tmp.status",
        "offhost/.postgres-backup.tmp.gz",
    )

    def seed(root: Path) -> None:
        for name in leftovers:
            (root / name).write_text("left by SIGKILL", encoding="ascii")

    loop, state = _start(tmp_path, "none", sleep_seconds="30", seed=seed)
    _run_until_marker(loop, state)

    # The pg_dumpall stub records what lay on both volumes when the dump started.
    listing = (tmp_path / "listing").read_text(encoding="ascii")
    assert ".postgres-backup.tmp.status" not in listing
    assert "offhost/.postgres-backup.tmp.gz" not in listing
    assert "left by SIGKILL" not in (tmp_path / "listing.gz").read_text(
        errors="replace"
    )


def test_insufficient_space_skips_the_attempt_and_keeps_the_old_marker(
    tmp_path: Path,
) -> None:
    def old_marker(root: Path) -> None:
        (root / "state/last-successful-backup.epoch").write_text("123\n")

    loop, state = _start(
        tmp_path, "none", seed=old_marker, extra_environment={"FREE_KB": "1"}
    )
    sleeps = tmp_path / "sleeps"
    try:
        _wait_for(
            lambda: (
                sleeps.is_file()
                and sleeps.read_text(encoding="ascii").split().count("300") >= 2
            ),
            loop,
        )
    finally:
        stderr = _terminate(loop)

    assert "needs" in stderr and "bytes free" in stderr
    assert (state / "last-successful-backup.epoch").read_text() == "123\n"
    assert not (tmp_path / "listing").exists(), "the dump started without space"
    assert not list((tmp_path / "backups").glob("postgres-*.sql.gz"))
    assert not list((tmp_path / "backups").glob(".postgres-backup.tmp*"))


def test_a_failed_dump_fails_the_attempt_even_when_gzip_succeeds(
    tmp_path: Path,
) -> None:
    loop, state = _start(tmp_path, "dump")
    sleeps = tmp_path / "sleeps"
    try:
        _wait_for(
            lambda: (
                sleeps.is_file()
                and sleeps.read_text(encoding="ascii").split().count("300") >= 2
            ),
            loop,
        )
    finally:
        stderr = _terminate(loop)

    assert "failed at step dump" in stderr
    assert not list((tmp_path / "backups").glob("postgres-*.sql.gz"))
    assert not (state / "last-successful-backup.epoch").exists()


def test_restore_verification_is_skipped_when_the_scratch_space_is_short(
    tmp_path: Path,
) -> None:
    loop, state = _start(
        tmp_path,
        "none",
        sleep_seconds="30",
        extra_environment={"FREE_TMP_KB": "1"},
    )
    stderr = _run_until_marker(loop, state)

    assert "restore verification skipped" in stderr
    assert list((tmp_path / "backups").glob("postgres-*.sql.gz"))
    assert not (state / "last-backup-restore-verification.epoch").exists()


def test_same_device_copies_need_both_dumps_but_not_more(tmp_path: Path) -> None:
    margin = 268435456
    newest = 1000

    def previous_dump(root: Path) -> None:
        (root / "backups/postgres-20200101T000000Z.sql.gz").write_bytes(b"x" * newest)

    # Free space sits between one copy and two copies on the shared volume.
    single = newest + margin
    free_kb = (single + single // 2) // 1024
    loop, _ = _start(
        tmp_path,
        "none",
        seed=previous_dump,
        extra_environment={"FREE_KB": str(free_kb)},
    )
    sleeps = tmp_path / "sleeps"
    try:
        _wait_for(
            lambda: sleeps.is_file() and "300" in sleeps.read_text().split(), loop
        )
    finally:
        stderr = _terminate(loop)

    assert f"needs {2 * single} bytes free" in stderr
    assert not (tmp_path / "listing").exists(), "the dump started without space"


@pytest.mark.parametrize("fault", ["gzip", "nostatus", "emptystatus"])
def test_a_broken_compression_or_missing_dump_status_fails_the_attempt(
    tmp_path: Path, fault: str
) -> None:
    loop, state = _start(tmp_path, fault)
    sleeps = tmp_path / "sleeps"
    try:
        _wait_for(
            lambda: (
                sleeps.is_file()
                and sleeps.read_text(encoding="ascii").split().count("300") >= 2
            ),
            loop,
        )
    finally:
        stderr = _terminate(loop)

    assert "failed at step dump" in stderr
    assert not list((tmp_path / "backups").glob("postgres-*.sql.gz"))
    assert not (state / "last-successful-backup.epoch").exists()


def test_unreadable_free_space_is_reported_as_unknown_not_zero(tmp_path: Path) -> None:
    loop, state = _start(
        tmp_path, "none", sleep_seconds="30", extra_environment={"DF_UNKNOWN": "1"}
    )
    stderr = _run_until_marker(loop, state)

    assert "cannot read the free space" in stderr
    assert "0 are free" not in stderr
    assert (state / "last-successful-backup.epoch").is_file()
