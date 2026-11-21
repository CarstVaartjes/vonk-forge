"""Staged runtime assets exist before their consumers start, without polling."""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

from vonk_control.runtime_init import STAGED_MARKER

ROOT = Path(__file__).resolve().parents[3]
STAGED = "runtime-init"
STAGED_VOLUMES = ("runtime-assets", "normalized-private-keys")
# Readers that load their kit only at start restart once a new one is staged.
# PostgreSQL must not bounce with every release; the API image changes with
# runtime-init's, so Compose recreates it anyway.
RESTART_WITH_KIT = {"litellm", "prometheus", "caddy", "tailscale-configurator"}


def _services() -> dict[str, dict]:
    loader = importlib.machinery.SourceFileLoader(
        "runtime_asset_compose", str(ROOT / "scripts/render-dev-compose")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module._compose_document(ROOT / "deploy/compose/compose.yaml")["services"]


def _volume_mounts(service: dict) -> list[tuple[str, bool]]:
    """(source, read_only) for each mount, in short or long syntax."""
    mounts = []
    for entry in service.get("volumes", []):
        if isinstance(entry, dict):
            mounts.append(
                (str(entry.get("source", "")), entry.get("read_only") is True)
            )
        else:
            fields = entry.split(":")
            mounts.append((fields[0], fields[-1] == "ro" and len(fields) == 3))
    return mounts


def test_every_staged_volume_reader_starts_after_runtime_init_and_never_polls() -> None:
    """Catches a consumer that can start before staging, or waits in a loop.

    The edge orders startup and never gates it (required: false): a failed
    staging run leaves the previous kit in the volume and its readers still
    start on it.
    """
    services = _services()
    readers = {
        name
        for name, service in services.items()
        if name != STAGED
        and any(volume in STAGED_VOLUMES for volume, _ in _volume_mounts(service))
    }
    assert readers == RESTART_WITH_KIT | {"postgres", "control-api", "control-worker"}
    for name in readers:
        service = services[name]
        assert service["depends_on"][STAGED] == {
            "condition": "service_healthy",
            "required": False,
            **({"restart": True} if name in RESTART_WITH_KIT else {}),
        }, name
        assert "until [" not in str(service.get("entrypoint", "")), name


def test_runtime_init_is_the_one_staging_writer_and_healthy_per_run() -> None:
    """Catches a second staging writer, a stale health marker, or network use."""
    services = _services()
    init = services[STAGED]
    assert init["image"] == services["control-api"]["image"]
    assert init["entrypoint"] == ["python", "-m", "vonk_control.runtime_init"]
    # Resident: an exited container shows the project as failed in NAS UIs.
    # A failed run exits and the restart policy runs it again.
    assert init["restart"] == "unless-stopped"
    assert init["network_mode"] == "none"
    # The marker is on a tmpfs, so every start begins unhealthy.
    assert init["tmpfs"] == ["/tmp"]
    assert init["healthcheck"]["test"] == ["CMD", "cat", str(STAGED_MARKER)]
    # Without a grace Compose gives up after three probes (15 s) and starts
    # the readers on the previous kit; restart: true then never fires.
    assert "start_period" in init["healthcheck"]
    assert init["cap_add"] == ["CHOWN", "FOWNER", "DAC_OVERRIDE"]
    writers = {
        name
        for name, service in services.items()
        for volume, read_only in _volume_mounts(service)
        if volume in STAGED_VOLUMES and not read_only
    }
    assert writers == {STAGED}
