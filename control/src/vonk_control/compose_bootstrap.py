"""Prepare shared Compose volumes and remain healthy for NAS UIs."""

from __future__ import annotations

import os
import time
from pathlib import Path

from .runtime_init import install_admin_grant_key


def _directory(path: str, uid: int, gid: int, mode: int) -> Path:
    target = Path(path)
    target.mkdir(mode=mode, parents=True, exist_ok=True)
    os.chown(target, uid, gid)
    os.chmod(target, mode)
    return target


def prepare() -> None:
    routes = _directory("/routes", 10001, 10001, 0o750)
    _directory(str(routes / "generations"), 10001, 10001, 0o750)
    _directory("/supervisor", 10002, 10001, 0o750)

    os.chown("/update-socket", 10003, 10001)
    os.chmod("/update-socket", 0o710)
    os.chown("/verifier", 10003, 10001)
    os.chmod("/verifier", 0o700)

    agent = _directory("/agent-publication", 10001, 10001, 0o750)
    for name in ("metadata", "targets"):
        _directory(str(agent / name), 10001, 10001, 0o750)

    os.chown("/workload-signer-socket", 10003, 10001)
    os.chmod("/workload-signer-socket", 0o710)
    workload = _directory("/workload-publication", 10001, 10001, 0o750)
    for name in ("metadata", "targets"):
        _directory(str(workload / name), 10003, 10001, 0o750)

    install_admin_grant_key(
        Path("/normalized/admin-grant-private-key"),
        Path("/api-runtime"),
    )
    Path("/tmp/bootstrap-ready").touch()


def main() -> None:
    prepare()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
