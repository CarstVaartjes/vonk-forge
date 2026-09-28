import gzip
import os
import pathlib
import subprocess as s
import tempfile
import time
import uuid

import pytest


@pytest.mark.skipif(
    os.environ.get("VONK_RUN_BACKUP_CONTAINER_TEST") != "1",
    reason="requires Docker; set VONK_RUN_BACKUP_CONTAINER_TEST=1",
)
@pytest.mark.needs_docker
@pytest.mark.needs_backup_container
def test_postgres_backup_restore():
    root = pathlib.Path(__file__).resolve().parents[3]
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="vonk-backup-test-"))
    (tmp / "backups").mkdir()
    (tmp / "offhost").mkdir()
    (tmp / "state").mkdir()
    (tmp / "step-ca-data").mkdir()
    (tmp / "step-ca-data" / "ca-state.txt").write_text("fixture CA state")
    (tmp / "secret").write_text("a" * 64)

    def run(*args, input=None):
        return s.check_output(["docker", *args], input=input, stderr=s.STDOUT)

    suffix = uuid.uuid4().hex[:12]
    a = "vonk-backup-source-" + suffix
    b = "vonk-backup-restore-" + suffix
    try:
        run(
            "run",
            "-d",
            "--name",
            a,
            "--network",
            "none",
            "-e",
            "POSTGRES_USER=control",
            "-e",
            "POSTGRES_DB=control",
            "-e",
            "POSTGRES_PASSWORD_FILE=/run/secrets/postgres-password",
            "-v",
            f"{tmp}/secret:/run/secrets/postgres-password:ro",
            "-e",
            "VONK_BACKUP_OFFHOST_PATH=/offhost",
            "-v",
            f"{tmp}/backups:/backups",
            "-v",
            f"{tmp}/offhost:/offhost",
            "-v",
            f"{tmp}/state:/state",
            "-v",
            f"{tmp}/step-ca-data:/step-ca-data:ro",
            "-v",
            f"{tmp}/secret:/run/secrets/litellm-database-password:ro",
            "-v",
            f"{root}/deploy/compose/postgres/entrypoint.sh:/entrypoint:ro",
            "-v",
            f"{root}/deploy/compose/postgres/init-databases.sh:/run/vonk-source-assets/postgres/init-databases.sh:ro",
            "--entrypoint",
            "/entrypoint",
            "postgres:18",
            "postgres",
        )
        for _ in range(60):
            if (tmp / "state" / "last-successful-backup.epoch").is_file():
                break
            time.sleep(1)
        else:
            raise RuntimeError(run("logs", a).decode())
        run(
            "exec",
            a,
            "psql",
            "-U",
            "control",
            "-d",
            "control",
            "-c",
            "CREATE TABLE backup_probe(value text); INSERT INTO backup_probe VALUES ('restored');",
        )
        # Every start takes a backup; restart for a second one with the probe.
        run("restart", a)
        deadline = time.monotonic() + 60
        files = []
        while time.monotonic() < deadline:
            files = sorted((tmp / "backups").glob("postgres-*.sql.gz"))
            if len(files) == 2:
                break
            time.sleep(1)
        assert len(files) == 2, files
        assert all(
            (path.stat().st_mode & 0o777) == 0o600 and path.stat().st_uid == os.getuid()
            for path in files
        ), [
            (path, path.stat().st_uid, oct(path.stat().st_mode & 0o777))
            for path in files
        ]
        ca_files = list((tmp / "backups").glob("step-ca-postgres-*.tar.gz"))
        assert ca_files
        assert "./ca-state.txt" in s.check_output(
            ["tar", "-tzf", str(ca_files[-1])], text=True
        )
        assert sorted(path.name for path in (tmp / "offhost").glob("*.gz"))
        assert (tmp / "offhost" / files[-1].name).is_file()
        assert (tmp / "offhost" / ca_files[-1].name).is_file()
        assert (tmp / "state" / "last-backup-restore-verification.epoch").is_file()
        data = gzip.decompress(files[-1].read_bytes())
        run(
            "run",
            "-d",
            "--name",
            b,
            "--network",
            "none",
            "-e",
            "POSTGRES_USER=vonk_restore_admin",
            "-e",
            "POSTGRES_PASSWORD_FILE=/run/secrets/postgres-password",
            "-v",
            f"{tmp}/secret:/run/secrets/postgres-password:ro",
            "postgres:18",
        )
        for _ in range(60):
            try:
                run(
                    "exec",
                    b,
                    "psql",
                    "-U",
                    "vonk_restore_admin",
                    "-d",
                    "postgres",
                    "-c",
                    "SELECT 1",
                )
                break
            except s.CalledProcessError:
                time.sleep(1)
        time.sleep(2)
        run(
            "exec",
            "-i",
            b,
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "vonk_restore_admin",
            "-d",
            "postgres",
            input=data,
        )
        assert (
            run(
                "exec",
                b,
                "psql",
                "-U",
                "control",
                "-d",
                "control",
                "-tAc",
                "SELECT value FROM backup_probe",
            ).strip()
            == b"restored"
        )
        run(
            "exec",
            b,
            "psql",
            "-U",
            "control",
            "-d",
            "postgres",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            "DROP DATABASE vonk_restore_admin;",
            "-c",
            "ALTER ROLE vonk_restore_admin NOLOGIN;",
        )
        print(
            "PASS: startup and scheduled backups, step-ca archive, off-host retention, isolated restore verification, full restore and bootstrap login disabled"
        )
    except s.CalledProcessError as e:
        print(e.output.decode())
        raise
    finally:
        for name in (a, b):
            s.run(
                ["docker", "rm", "-fv", name],
                stdout=s.DEVNULL,
                stderr=s.DEVNULL,
                check=False,
            )


@pytest.mark.skipif(
    os.environ.get("VONK_RUN_BACKUP_CONTAINER_TEST") != "1",
    reason="requires Docker; set VONK_RUN_BACKUP_CONTAINER_TEST=1",
)
def test_postgres_backup_worker_resumes_when_directory_appears():
    root = pathlib.Path(__file__).resolve().parents[3]
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="vonk-backup-retry-test-"))
    (tmp / "state").mkdir()
    (tmp / "secret").write_text("a" * 64)

    def run(*args):
        return s.check_output(["docker", *args], stderr=s.STDOUT)

    name = "vonk-backup-retry-" + uuid.uuid4().hex[:12]
    try:
        run(
            "run",
            "-d",
            "--name",
            name,
            "--network",
            "none",
            "-e",
            "POSTGRES_USER=control",
            "-e",
            "POSTGRES_DB=control",
            "-e",
            "POSTGRES_PASSWORD_FILE=/run/secrets/postgres-password",
            "-v",
            f"{tmp}/secret:/run/secrets/postgres-password:ro",
            "-v",
            f"{tmp}/secret:/run/secrets/litellm-database-password:ro",
            "-v",
            f"{tmp}/state:/state",
            "-v",
            f"{root}/deploy/compose/postgres/entrypoint.sh:/entrypoint:ro",
            "-v",
            f"{root}/deploy/compose/postgres/init-databases.sh:/run/vonk-source-assets/postgres/init-databases.sh:ro",
            "--entrypoint",
            "/entrypoint",
            "postgres:18",
            "postgres",
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            running = run("inspect", "--format", "{{.State.Running}}", name).strip()
            if running == b"true":
                break
            time.sleep(0.5)
        else:
            raise RuntimeError(run("logs", name).decode(errors="replace"))

        time.sleep(6)
        run("exec", name, "mkdir", "-p", "/backups")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result = s.run(
                [
                    "docker",
                    "exec",
                    name,
                    "test",
                    "-f",
                    "/state/last-successful-backup.epoch",
                ],
                stdout=s.DEVNULL,
                stderr=s.DEVNULL,
                check=False,
            )
            if result.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError(run("logs", name).decode(errors="replace"))
    finally:
        s.run(
            ["docker", "rm", "-fv", name],
            stdout=s.DEVNULL,
            stderr=s.DEVNULL,
            check=False,
        )
