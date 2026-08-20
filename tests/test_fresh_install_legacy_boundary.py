from __future__ import annotations

import importlib.util
import json
import subprocess
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import yaml

from cluster_profiles.cli import main

ROOT = Path(__file__).resolve().parents[1]
OBSOLETE_CONTROL_MODULES = (
    "dev_litellm_database",
    "dev_runtime_assets",
    "development_tokens",
    "dev_cohort",
    "generation_launch",
    "host_backup",
    "host_commands",
    "host_state",
    "oci_bundle",
    "offline",
    "upgrade",
)

OBSOLETE_DEVELOPMENT_INSTALLERS = (
    "scripts/dev-admin-token",
    "scripts/dev-compose",
    "deploy/compose/compose.dev.images.yaml",
    "deploy/compose/compose.dev.yaml",
)


class _UnexpectedClient:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"obsolete update command reached client method {name}")


def test_operator_cli_rejects_removed_platform_update_commands() -> None:
    """Catches restoration of the app-managed platform update control surface."""
    stdout = StringIO()
    stderr = StringIO()

    with redirect_stdout(stdout), redirect_stderr(stderr):
        result = main(
            ("admin", "updates", "skew", "--json"),
            control_client=_UnexpectedClient(),
        )

    assert result == 2
    error = json.loads(stdout.getvalue())
    assert error["error_type"] == "arguments"
    assert "invalid choice: 'updates'" in error["error"]
    assert stderr.getvalue() == ""


def test_obsolete_control_host_modules_are_not_importable() -> None:
    """Catches shipping a compatibility implementation behind the fresh runtime."""
    for module in OBSOLETE_CONTROL_MODULES:
        assert not (ROOT / "control/src/vonk_control" / f"{module}.py").exists(), module

    assert importlib.util.find_spec("cluster_profiles.update_trust") is None


def test_no_alternate_development_installer_or_compose_overlay_remains() -> None:
    for relative in OBSOLETE_DEVELOPMENT_INSTALLERS:
        assert not (ROOT / relative).exists(), relative
    resources = ROOT / "control/src/vonk_control/resources/dev"
    assert not resources.exists() or not any(
        path.is_file() and "__pycache__" not in path.parts
        for path in resources.rglob("*")
    )


def test_control_wheel_has_no_offline_updater_entrypoint_or_modules(
    tmp_path: Path,
) -> None:
    """Catches obsolete root-host commands returning in the installable wheel."""
    output = tmp_path / "dist"
    subprocess.run(
        [
            "uv",
            "build",
            "--offline",
            "--wheel",
            "--project",
            str(ROOT / "control"),
            "--out-dir",
            str(output),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(output.glob("vonk_control-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        entry_points = [
            archive.read(name).decode()
            for name in names
            if name.endswith(".dist-info/entry_points.txt")
        ]

    assert entry_points == []
    for module in OBSOLETE_CONTROL_MODULES:
        assert f"vonk_control/{module}.py" not in names


def test_compose_runtime_has_no_host_generation_identity() -> None:
    """Catches coupling the NAS stack to an external host-generation selector."""
    compose = yaml.safe_load((ROOT / "deploy/compose/compose.yaml").read_text())
    forbidden = {
        "VONK_CONTROL_GENERATION_ID",
        "VONK_CONTROL_IDENTITY_ROOT",
        "VONK_CONTROL_OPERATION_ID",
        "VONK_CONTROL_PROCESS_IMAGE",
        "VONK_CONTROL_START_NONCE",
        "VONK_CONTROL_STARTUP_MODE",
        "VONK_DATABASE_REVISION",
        "VONK_DEV_SELECTED_COHORT_FILE",
        "VONK_PLATFORM_BUILD_DIGEST",
        "VONK_PLATFORM_RELEASE_DIGEST",
        "VONK_PLATFORM_VERSION",
    }

    services = compose["services"]
    for service_name in ("control-api", "control-worker"):
        environment = services[service_name]["environment"]
        assert forbidden.isdisjoint(environment), service_name
        assert all(
            "control-identity" not in str(volume)
            for volume in services[service_name].get("volumes", [])
        )


def test_release_workflow_has_no_host_updater_or_platform_tuf_channel() -> None:
    """Catches publishing artifacts that no supported installer consumes."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    jobs = workflow["jobs"]

    assert "attest-host-updater" not in jobs
    assert "publish-platform-target" not in jobs
    serialized = repr(workflow)
    for obsolete in (
        "build-host-updater-artifact",
        "platform-release-authority",
        "publish-platform-target publish-channel",
        "publish-platform-target publish-target",
        "vonk-forge-host-updater.tar",
    ):
        assert obsolete not in serialized
