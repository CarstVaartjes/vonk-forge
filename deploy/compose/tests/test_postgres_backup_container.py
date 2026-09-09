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
def test_postgres_backup_restore():
    root = pathlib.Path(__file__).resolve().parents[3]
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="vonk-backup-test-"))
    (tmp / "backups").mkdir()
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
            "VONK_BACKUP_INTERVAL_SECONDS=2",
            "-e",
            "VONK_BACKUP_KEEP=2",
            "-v",
            f"{tmp}/backups:/backups",
            "-v",
            f"{tmp}/secret:/run/secrets/litellm-database-password:ro",
            "-v",
            f"{root}/deploy/compose/postgres/entrypoint.sh:/entrypoint:ro",
            "-v",
            f"{root}/deploy/compose/postgres/init-databases.sh:/docker-entrypoint-initdb.d/10-vonk-forge-databases.sh:ro",
            "--entrypoint",
            "/entrypoint",
            "postgres:18",
            "postgres",
        )
        for _ in range(60):
            if list((tmp / "backups").glob("*.gz")):
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
        time.sleep(7)
        files = sorted((tmp / "backups").glob("*.gz"))
        assert len(files) == 2, files
        assert all(
            (path.stat().st_mode & 0o777) == 0o600 and path.stat().st_uid == os.getuid()
            for path in files
        ), [(path, path.stat().st_uid, oct(path.stat().st_mode & 0o777)) for path in files]
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
            "PASS: startup backup, scheduled backups, retention=2, full restore and bootstrap login disabled"
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
