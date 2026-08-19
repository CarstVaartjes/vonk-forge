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
    for name in (
        "database-url",
        "token-signing-key",
        "metrics-token",
        "git-signing-key",
        "agent-client-ca",
        "agent-intermediate-certificate",
        "controller-ca",
        "agent-proxy-auth",
        "worker-api-token",
    ):
        stage_private_key(
            Path(f"/run/secrets/{name}"),
            Path(f"/normalized/{name}"),
            owner_uid=10001,
            owner_gid=10001,
            mode=0o400,
        )
    for name in (
        "litellm-master-key",
        "litellm-upstream-key",
        "litellm-database-url",
    ):
        stage_private_key(
            Path(f"/run/secrets/{name}"),
            Path(f"/normalized/{name}"),
            owner_uid=10002,
            owner_gid=10001,
            mode=0o400,
        )
    stage_private_key(
        Path("/run/secrets/metrics-token"),
        Path("/normalized/prometheus-metrics-token"),
        owner_uid=65534,
        owner_gid=65534,
        mode=0o400,
    )
    stage_private_key(
        Path("/run/secrets/grafana-admin-password"),
        Path("/normalized/grafana-admin-password"),
        owner_uid=472,
        owner_gid=472,
        mode=0o400,
    )
    for name in (
        "agent-ca-credential",
        "agent-ca-provisioner-public-jwk",
        "step-ca-root-certificate",
        "agent-intermediate-key",
    ):
        source = Path(f"/run/secrets/{name}")
        if source.exists():
            stage_private_key(
                source,
                Path(f"/normalized/{name}"),
                owner_uid=10001,
                owner_gid=10001,
                mode=0o400,
            )
    for name in (
        "step-ca-root-certificate",
        "agent-intermediate-certificate",
        "step-ca-intermediate-key",
        "step-ca-password",
    ):
        source = Path(f"/run/secrets/{name}")
        if source.exists():
            destination_name = {
                "step-ca-root-certificate": "root-certificate",
                "agent-intermediate-certificate": "intermediate-certificate",
                "step-ca-intermediate-key": "intermediate-key",
                "step-ca-password": "password",
            }[name]
            stage_private_key(
                source,
                Path(f"/normalized/step-ca/{destination_name}"),
                owner_uid=1000,
                owner_gid=1000,
                mode=0o400,
            )


if __name__ == "__main__":
    prepare()
