"""Exercise installer ownership against the real root pre-exec in Linux."""

from __future__ import annotations

import subprocess
from textwrap import dedent

import pytest


@pytest.mark.built_image
@pytest.mark.lane  # Real Linux uid/gid checks against the already-built API.
def test_gateway_authority_survives_installer_backup_and_cleanup(
    controller_image: str,
) -> None:
    # Catches root-owned authority directories, API-only file ownership, and
    # staging that resets the gateway bind owner. No mocked ownership calls.
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "python",
            controller_image,
            "-c",
            dedent(
                """
                import os, subprocess, sys, tempfile
                from pathlib import Path
                from vonk_control.runtime_init import stage_compose_secrets

                with tempfile.TemporaryDirectory(dir='/tmp') as directory:
                    root = Path(directory)
                    os.chown(root, 12000, 12000)
                    root.chmod(0o755)
                    source, shared, gateway = (root / name for name in ('source', 'normalized', 'gateway'))
                    for path in (source, shared, gateway):
                        path.mkdir()
                    os.chown(gateway, 12000, 10001)
                    gateway.chmod(0o770)
                    for name in (
                        'host-runtime-grant-private-key', 'database-url',
                        'token-signing-key', 'metrics-token', 'agent-client-ca',
                        'agent-intermediate-certificate', 'controller-ca',
                        'agent-proxy-auth', 'litellm-master-key', 'litellm-database-url',
                    ):
                        (source / name).write_bytes(b'preserved-secret')
                    key = gateway / 'authority/master-key'
                    stale = shared / 'gateway-litellm-master-key'
                    for repair in (False, True):
                        if repair:
                            os.chown(key.parent, 0, 10001)
                            key.parent.chmod(0o755)
                        stale.write_bytes(b'previous-shared-copy')
                        stage_compose_secrets(source, shared, gateway_master_key=key)
                        assert not stale.exists()
                        assert key.parent.stat().st_uid == gateway.stat().st_uid == 12000
                        subprocess.run(
                            [sys.executable, '-c', "from pathlib import Path; import sys; assert Path(sys.argv[1]).read_bytes() == b'preserved-secret'", str(key)],
                            user=10001, group=10001, extra_groups=[], check=True,
                        )
                    # Backup and the acceptance/uninstall directory walk run
                    # as the installer, with no administrative capabilities.
                    subprocess.run(
                        [sys.executable, '-c', '''
                import os, shutil, sys
                from pathlib import Path
                gateway = Path(sys.argv[1])
                backup = gateway.with_name('backup')
                shutil.copytree(gateway, backup)
                assert (backup / 'authority/master-key').read_bytes() == b'preserved-secret'
                for directory, _, _ in os.walk(gateway):
                    Path(directory).chmod(0o700)
                shutil.rmtree(gateway)
                shutil.rmtree(backup)
                ''', str(gateway)],
                        user=12000, group=12000, extra_groups=[], check=True,
                    )
                """
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
