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
# Only a hang guard: every wait below ends on the event it waits for. It is
# never the thing under test, so a loaded runner cannot trip it.
HANG_GUARD_SECONDS = 120
# Injected stalls sleep far longer than any wait, so only the script's own bound
# (the stubbed `timeout` below) can end them.
STALL_SECONDS = "600"

STUBS = {
    "gosu": 'shift\nexec "$@"\n',
    # FAULT=hangrestore stalls the restore verification inside initdb.
    "initdb": (
        'if [ "$FAULT" = hangrestore ]; then\n'
        '  echo $$ >"$FAULTS/initdb.pid"\n'
        '  exec "$REAL_SLEEP" "$STALL_SECONDS"\n'
        "fi\n"
        "exit 0\n"
    ),
    # Every call is logged so a test can count the restore's cleanup stops.
    "pg_ctl": 'echo "$*" >>"$FAULTS/pg_ctl.log"\nexit 0\n',
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
    # POSIX df output; /tmp is the restore scratch location. DF_FAIL names a
    # path suffix whose df fails (DF_STALL: stalls past the timeout instead);
    # KB_PER_DUMP makes /backups shrink with every published dump.
    "df": (
        'case "$*" in\n'
        '  *"${DF_FAIL:-NO-SUCH-SUFFIX}")\n'
        '    [ -n "${DF_STALL:-}" ] && exec "$REAL_SLEEP" "$STALL_SECONDS"\n'
        "    exit 1\n"
        "    ;;\n"
        "esac\n"
        'case "$*" in\n'
        "  */tmp) kb=${FREE_TMP_KB:-100000000}; device=scratch ;;\n"
        "  *)\n"
        "    kb=${FREE_KB:-100000000}; device=pool\n"
        '    dumps=$(ls "$BACKUPS"/postgres-*.sql.gz 2>/dev/null | wc -l)\n'
        "    kb=$((kb - ${KB_PER_DUMP:-0} * dumps))\n"
        '    [ -e "$BACKUPS/.postgres-backup.tmp" ] && kb=$((kb - ${KB_STALE:-0}))\n'
        "    ;;\n"
        "esac\n"
        'case "$*" in *offhost) device=${OFFHOST_DEVICE:-$device} ;; esac\n'
        "echo 'Filesystem 1024-blocks Used Available Capacity Mounted on'\n"
        'echo "$device 1 1 $kb 1% /"\n'
    ),
    # The probes' real bounds are seconds. Tests that inject a stall shorten the
    # bound (STUB_TIMEOUT); all others keep it far above any loaded runner's delay.
    "timeout": 'shift\nexec "$REAL_TIMEOUT" "$STUB_TIMEOUT" "$@"\n',
    # Distinct dump names even within one second.
    "date": (
        'case "$*" in\n'
        "  *%Y%m%dT*)\n"
        '    n=$(cat "$FAULTS/date" 2>/dev/null || echo 0); n=$((n + 1))\n'
        '    echo "$n" >"$FAULTS/date"\n'
        "    printf '20300101T%06dZ\\n' \"$n\"\n"
        "    ;;\n"
        '  *) exec "$REAL_DATE" "$@" ;;\n'
        "esac\n"
    ),
    # An unreachable off-host mount: removing there never returns.
    "rm": (
        'case "$*" in *offhost*) [ -n "${RM_HANG:-}" ] && exec "$REAL_SLEEP" "$STALL_SECONDS" ;; esac\n'
        'exec "$REAL_RM" "$@"\n'
    ),
    # Off-host copies fail CP_FAILURES times (or hang); the first copy after that
    # works. Every off-host copy records how many dumps /backups holds.
    "cp": (
        'case "$*" in *offhost*) ;; *) exec "$REAL_CP" "$@" ;; esac\n'
        'ls "$BACKUPS"/postgres-*.sql.gz | wc -l >>"$COUNTS"\n'
        '[ "$FAULT" = hangcp ] && exec "$REAL_SLEEP" "$STALL_SECONDS"\n'
        # A copy that keeps growing never trips the stall bound: only a signal
        # ends it, like a multi-gigabyte copy.
        'if [ "$FAULT" = growcp ]; then\n'
        '  echo $$ >"$FAULTS/cp.pid"\n'
        '  while :; do printf x >>"$2"; "$REAL_SLEEP" 0.05; done\n'
        "fi\n"
        # A truncated copy, and a slow copy that keeps growing before it completes.
        '[ "$FAULT" = shortcp ] && head -c 1 "$1" >"$2" && exit 0\n'
        'if [ "$FAULT" = slowcp ]; then\n'
        # Each chunk waits for the loop's next poll (it logs a sleep), so the
        # copy visibly grows between polls however slowly the runner schedules.
        "  for chunk in $(seq 1 30); do\n"
        '    seen=$({ cat "$SLEEPS" 2>/dev/null || :; } | wc -l)\n'
        '    printf x >>"$2"\n'
        '    while [ "$({ cat "$SLEEPS" 2>/dev/null || :; } | wc -l)" -le "$seen" ]; do\n'
        '      "$REAL_SLEEP" 0.01\n'
        "    done\n"
        "  done\n"
        "fi\n"
        "attempt=0\n"
        'while [ "$attempt" -lt "${CP_FAILURES:-0}" ]; do\n'
        '  mkdir "$FAULTS/cp$attempt" 2>/dev/null && exit 1\n'
        "  attempt=$((attempt + 1))\n"
        "done\n"
        'exec "$REAL_CP" "$@"\n'
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
    # Retry and interval sleeps last LOOP_TEST_SLEEP; the copy's poll stays short.
    "sleep": (
        'echo "$1" >>"$SLEEPS"\n'
        'case "$1" in\n'
        '  86400|300) exec "$REAL_SLEEP" "${LOOP_TEST_SLEEP:-0.05}" ;;\n'
        '  *) exec "$REAL_SLEEP" 0.05 ;;\n'
        "esac\n"
    ),
}


def _backup_loop(root: Path) -> str:
    text = ENTRYPOINT.read_text(encoding="utf-8")
    match = re.search(
        r"^backup_loop\(\) [({]\n.*?^[)}]\n", text, re.MULTILINE | re.DOTALL
    )
    assert match, "backup_loop is not defined in the entrypoint"
    return re.sub(
        r"(?<![\w.])/(backups|state|offhost|tmp)\b",
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
    for directory in ("backups", "state", "offhost", "faults", "bin", "tmp"):
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
        "REAL_TIMEOUT": str(shutil.which("timeout")),
        "REAL_CP": str(shutil.which("cp")),
        "REAL_RM": str(shutil.which("rm")),
        "REAL_DATE": str(shutil.which("date")),
        "COUNTS": str(tmp_path / "counts"),
        "LISTING": str(tmp_path / "listing"),
        **(extra_environment or {}),
        "WORKER_PID": str(tmp_path / "worker.pid"),
        "FAULTS": str(tmp_path / "faults"),
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "POSTGRES_USER": "control",
        "REAL_MV": str(shutil.which("mv")),
        "REAL_SLEEP": str(shutil.which("sleep")),
        "SLEEPS": str(tmp_path / "sleeps"),
        "STALL_SECONDS": STALL_SECONDS,
        "LOOP_TEST_SLEEP": sleep_seconds,
        "VONK_BACKUP_OFFHOST_PATH": "/srv/offhost",
    }
    stalls = fault == "hangcp" or any(
        (extra_environment or {}).get(name) for name in ("DF_STALL", "RM_HANG")
    )
    environment["STUB_TIMEOUT"] = "1" if stalls else str(HANG_GUARD_SECONDS)
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
    deadline = time.monotonic() + HANG_GUARD_SECONDS
    while not condition():
        assert loop.poll() is None, "the backup loop exited"
        assert time.monotonic() < deadline, "the backup loop made no progress"
        time.sleep(0.05)


def _terminate(loop: subprocess.Popen[str]) -> str:
    try:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(loop.pid, signal.SIGTERM)
        _, stderr = loop.communicate(timeout=HANG_GUARD_SECONDS)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(loop.pid, signal.SIGKILL)
    return stderr


@pytest.mark.parametrize("fault", ["publish", "success-marker"])
def test_a_failed_attempt_is_retried_until_a_backup_is_verified(
    tmp_path: Path, fault: str
) -> None:
    # Guards the new dump-into-gzip pipe against hiding a failure of either end;
    # main's file-based flow failed these runs for other reasons, so this test
    # is not part of the "fails on main" set.
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
        loop.wait(timeout=HANG_GUARD_SECONDS)
        assert not temporary.exists(), "the loop's signal trap never ran"
    finally:
        _terminate(loop)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize(
    ("fault", "pid_file"),
    [("growcp", "cp.pid"), ("hangrestore", "initdb.pid")],
)
def test_a_term_to_the_worker_alone_ends_a_running_attempt_promptly(
    tmp_path: Path, fault: str, pid_file: str
) -> None:
    # The signal goes to the worker pid only (as forward_signal sends it), while
    # an attempt is inside a long foreground step. The trap must not wait for
    # that step: the worker exits, its cleanup runs and the step is ended too.
    loop, _ = _start(tmp_path, fault)
    pid_path = tmp_path / "faults" / pid_file
    try:
        _wait_for(pid_path.is_file, loop)
        step = int(pid_path.read_text())
        stale = tmp_path / "state/.last-successful-backup.epoch.tmp"
        stale.write_text("interrupted", encoding="ascii")
        worker = int((tmp_path / "worker.pid").read_text())
        os.kill(worker, signal.SIGTERM)
        loop.wait(timeout=HANG_GUARD_SECONDS)
        assert not stale.exists(), "the loop's signal trap never ran"
        if fault == "hangrestore":
            # The restore's own cleanup finished before the worker exited.
            log = tmp_path / "faults" / "pg_ctl.log"
            stops = [
                line
                for line in log.read_text(encoding="ascii").splitlines()
                if "-m immediate stop" in line
            ]
            assert len(stops) == 1, "the restore cleanup did not run exactly once"
            assert not list((tmp_path / "tmp").glob("vonk-backup-restore.*"))
        deadline = time.monotonic() + HANG_GUARD_SECONDS
        while _alive(step):
            assert time.monotonic() < deadline, "the stalled step was left running"
            time.sleep(0.05)
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


def _retries(tmp_path: Path, count: int) -> Callable[[], bool]:
    sleeps = tmp_path / "sleeps"
    return lambda: (
        sleeps.is_file()
        and sleeps.read_text(encoding="ascii").split().count("300") >= count
    )


def test_attempts_that_fail_after_publishing_cannot_pile_dumps_up_forever(
    tmp_path: Path,
) -> None:
    # Every failed off-host copy leaves a published dump behind and /backups
    # shrinks with each one; without pruning at the top of every pass the space
    # check would eventually refuse all attempts for good.
    loop, state = _start(
        tmp_path,
        "none",
        extra_environment={
            "CP_FAILURES": "14",
            "FREE_KB": "640000",
            "KB_PER_DUMP": "10000",
        },
    )
    stderr = _run_until_marker(loop, state)

    counts = [int(count) for count in (tmp_path / "counts").read_text().split()]
    assert len(counts) >= 15, stderr
    assert max(counts) <= 8, counts
    assert len(list((tmp_path / "backups").glob("postgres-*.sql.gz"))) <= 8


def test_the_newest_restore_verified_dump_survives_pruning(tmp_path: Path) -> None:
    base = int(time.time()) - 10000

    def seed(root: Path) -> None:
        for number in range(1, 10):
            dump = root / f"backups/postgres-2020010{number}T000000Z.sql.gz"
            dump.write_bytes(b"x")
            os.utime(dump, (base + number, base + number))
        (root / "state/last-backup-restore-verification.epoch").write_text(
            f"{base + 2}\n"
        )

    loop, _ = _start(tmp_path, "verify", seed=seed)
    try:
        _wait_for(_retries(tmp_path, 2), loop)
    finally:
        _terminate(loop)

    names = {path.name for path in (tmp_path / "backups").glob("postgres-*.sql.gz")}
    assert "postgres-20200102T000000Z.sql.gz" in names  # the verified one
    assert "postgres-20200101T000000Z.sql.gz" not in names
    assert "postgres-20200103T000000Z.sql.gz" not in names


def test_backup_volumes_on_different_devices_need_one_copy_each(tmp_path: Path) -> None:
    single = 268435456 + 1000
    loop, state = _start(
        tmp_path,
        "none",
        sleep_seconds="30",
        extra_environment={
            "FREE_KB": str((single + single // 2) // 1024),
            "OFFHOST_DEVICE": "other-pool",
        },
    )
    _run_until_marker(loop, state)

    assert (state / "last-backup-restore-verification.epoch").is_file()


@pytest.mark.parametrize("stall", ["", "1"])
def test_unreadable_offhost_space_continues_and_unreadable_local_space_is_short(
    tmp_path: Path, stall: str
) -> None:
    # An unreachable (failing or stalled) /offhost is reported unknown, never
    # "0 bytes free", and heals through the copy step; it does not block.
    loop, state = _start(
        tmp_path,
        "none",
        sleep_seconds="30",
        extra_environment={"DF_FAIL": "offhost", "DF_STALL": stall},
    )
    stderr = _run_until_marker(loop, state)

    assert "cannot read the free space" in stderr
    assert "offhost; continuing" in stderr
    assert "0 are free" not in stderr


@pytest.mark.parametrize("stall", ["", "1"])
def test_unreadable_space_on_the_database_volume_counts_as_short(
    tmp_path: Path, stall: str
) -> None:
    loop, _ = _start(
        tmp_path,
        "none",
        extra_environment={"DF_FAIL": "backups", "DF_STALL": stall},
    )
    try:
        _wait_for(_retries(tmp_path, 2), loop)
    finally:
        stderr = _terminate(loop)

    assert "treating it as short" in stderr
    assert not (tmp_path / "listing").exists(), "the dump started"


def test_unreadable_scratch_space_skips_restore_verification(tmp_path: Path) -> None:
    loop, state = _start(
        tmp_path, "none", sleep_seconds="30", extra_environment={"DF_FAIL": "/tmp"}
    )
    stderr = _run_until_marker(loop, state)

    assert "restore verification skipped" in stderr
    assert not (state / "last-backup-restore-verification.epoch").exists()


def test_a_hung_offhost_copy_fails_the_step_instead_of_hanging(tmp_path: Path) -> None:
    loop, _ = _start(tmp_path, "hangcp")
    try:
        _wait_for(_retries(tmp_path, 2), loop)
    finally:
        stderr = _terminate(loop)

    assert "failed at step off-host-copy" in stderr


def test_an_old_uncompressed_dump_from_a_killed_container_is_removed(
    tmp_path: Path,
) -> None:
    # The 2026-10-30 incident: the older version was killed mid-dump and left
    # this file, which then took the space the next attempt needs.
    def leftovers(root: Path) -> None:
        (root / "backups/.postgres-backup.tmp").write_text("8 GB of SQL")
        (root / "tmp/vonk-backup-restore.stale").mkdir()

    loop, state = _start(
        tmp_path,
        "none",
        sleep_seconds="30",
        seed=leftovers,
        extra_environment={"FREE_KB": "700000", "KB_STALE": "300000"},
    )
    _run_until_marker(loop, state)

    listing = (tmp_path / "listing").read_text().split()
    assert not [path for path in listing if path.endswith("/.postgres-backup.tmp")]
    assert not (tmp_path / "tmp/vonk-backup-restore.stale").exists()
    assert list((tmp_path / "backups").glob("postgres-*.sql.gz"))


def test_a_hung_mount_cannot_stall_cleanup_or_pruning(
    tmp_path: Path,
) -> None:
    loop, _ = _start(tmp_path, "hangcp", extra_environment={"RM_HANG": "1"})
    try:
        _wait_for(_retries(tmp_path, 2), loop)
    finally:
        stderr = _terminate(loop)

    # The failure is logged before the (bounded) cleanup, and the local dump
    # was published before the off-host step.
    assert "failed at step off-host-copy" in stderr
    assert list((tmp_path / "backups").glob("postgres-*.sql.gz"))


def test_a_slow_copy_that_keeps_growing_completes(tmp_path: Path) -> None:
    loop, state = _start(tmp_path, "slowcp", sleep_seconds="30")
    stderr = _run_until_marker(loop, state)

    assert "made no progress" not in stderr
    assert list((tmp_path / "offhost").glob("postgres-*.sql.gz"))


def test_a_truncated_offhost_copy_fails_the_size_check(tmp_path: Path) -> None:
    loop, _ = _start(tmp_path, "shortcp")
    try:
        _wait_for(_retries(tmp_path, 2), loop)
    finally:
        stderr = _terminate(loop)

    assert "failed at step off-host-copy" in stderr
    assert not list((tmp_path / "offhost").glob("postgres-*.sql.gz"))
