"""Normalize file-backed Compose secrets into a Docker-managed volume."""

from __future__ import annotations

from pathlib import Path

from .runtime_init import stage_private_key


def prepare() -> None:
    stage_private_key(
        Path("/run/secrets/admin-grant-private-key"),
        Path("/normalized/admin-grant-private-key"),
    )
    for name in (
        "package-helper-grant-private-key",
        "package-helper-receipt-private-key",
        "host-runtime-grant-private-key",
    ):
        stage_private_key(
            Path(f"/run/secrets/{name}"),
            Path(f"/normalized/{name}"),
            owner_uid=10001,
            owner_gid=10001,
            mode=0o400,
        )
    for name in (
        "agent-update-authority-key",
        "admin-grant-public-key",
        "agent-tuf-bootstrap-root",
    ):
        stage_private_key(
            Path(f"/run/secrets/{name}"),
            Path(f"/normalized/{name}"),
            owner_uid=10003,
            owner_gid=10001,
            mode=0o400,
        )


if __name__ == "__main__":
    prepare()
