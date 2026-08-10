from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SUPERVISOR = ROOT / "deploy/compose/litellm/config_supervisor.py"


def _module():
    spec = importlib.util.spec_from_file_location(
        "litellm_config_supervisor", SUPERVISOR
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bundle(module, tmp_path: Path, *, now: datetime, expires_at: datetime):
    root = tmp_path / "routes"
    config = b'{"model_list":[{"model_name":"chat"}]}\n'
    routes = b'{"routes":{"chat":{}},"state":"published"}\n'
    manifest = {
        "schema_version": 1,
        "generation": 1,
        "state": "published",
        "reconciliation_id": "bb7aac18-edbf-4cc1-bafd-15e282557c53",
        "plan_digest": "a" * 64,
        "evidence_set_digest": "b" * 64,
        "routes_sha256": hashlib.sha256(routes).hexdigest(),
        "litellm_sha256": hashlib.sha256(config).hexdigest(),
        "issued_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    manifest_bytes = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    directory_name = "00000001-" + manifest_digest
    directory = root / "generations" / directory_name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "litellm.json").write_bytes(config)
    (directory / "routes.json").write_bytes(routes)
    (directory / "manifest.json").write_bytes(manifest_bytes)
    activation = {
        **manifest,
        "directory": directory_name,
        "manifest_sha256": manifest_digest,
    }
    (root / "activation.json").write_text(
        json.dumps(activation, sort_keys=True, separators=(",", ":")) + "\n"
    )
    bootstrap = tmp_path / "bootstrap.json"
    bootstrap.write_bytes(b'{"model_list":[]}\n')
    module.ROOT = root
    module.ACTIVATION = root / "activation.json"
    module.GENERATIONS = root / "generations"
    module.BOOTSTRAP = bootstrap
    return directory / "litellm.json", bootstrap, directory


def test_supervisor_selects_only_an_exact_fresh_activation_bundle(
    tmp_path: Path,
) -> None:
    module = _module()
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    generated, _bootstrap, _directory = _bundle(
        module,
        tmp_path,
        now=now - timedelta(seconds=30),
        expires_at=now + timedelta(seconds=120),
    )

    assert module._selected(now=now) == generated


def test_supervisor_falls_back_for_expired_or_hash_mismatched_bundle(
    tmp_path: Path,
) -> None:
    module = _module()
    now = datetime(2026, 8, 5, 12, 3, tzinfo=UTC)
    generated, bootstrap, _directory = _bundle(
        module,
        tmp_path,
        now=now - timedelta(seconds=180),
        expires_at=now - timedelta(seconds=30),
    )
    assert module._selected(now=now) == bootstrap

    generated, bootstrap, _directory = _bundle(
        module,
        tmp_path,
        now=now,
        expires_at=now + timedelta(seconds=150),
    )
    generated.write_bytes(b'{"model_list":[{"unsafe":true}]}\n')
    assert module._selected(now=now) == bootstrap


def test_supervisor_rejects_a_lease_beyond_the_production_bound(
    tmp_path: Path,
) -> None:
    module = _module()
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    _generated, bootstrap, _directory = _bundle(
        module,
        tmp_path,
        now=now,
        expires_at=now + timedelta(seconds=301),
    )

    assert module._selected(now=now) == bootstrap


def test_supervisor_falls_back_when_manifest_or_marker_is_not_exact(
    tmp_path: Path,
) -> None:
    module = _module()
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    _generated, bootstrap, directory = _bundle(
        module,
        tmp_path,
        now=now,
        expires_at=now + timedelta(seconds=150),
    )
    manifest = json.loads((directory / "manifest.json").read_bytes())
    manifest["plan_digest"] = "f" * 64
    (directory / "manifest.json").write_text(json.dumps(manifest))
    assert module._selected(now=now) == bootstrap

    _generated, bootstrap, _directory = _bundle(
        module,
        tmp_path,
        now=now,
        expires_at=now + timedelta(seconds=150),
    )
    activation = json.loads(module.ACTIVATION.read_bytes())
    module.ACTIVATION.write_text(json.dumps(activation, indent=2))
    assert module._selected(now=now) == bootstrap

    _generated, bootstrap, _directory = _bundle(
        module,
        tmp_path,
        now=now,
        expires_at=now + timedelta(seconds=150),
    )
    activation = json.loads(module.ACTIVATION.read_bytes())
    activation["unknown"] = True
    module.ACTIVATION.write_text(json.dumps(activation))
    assert module._selected(now=now) == bootstrap


def test_supervisor_ack_binds_a_live_child_to_the_exact_activation_request(
    tmp_path: Path,
) -> None:
    module = _module()
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    _generated, _bootstrap, _directory = _bundle(
        module,
        tmp_path,
        now=now - timedelta(seconds=1),
        expires_at=now + timedelta(seconds=120),
    )
    module.ACK_ROOT = tmp_path / "supervisor"
    module.ACK = module.ACK_ROOT / "ack.json"

    class Child:
        pid = 123

        @staticmethod
        def poll():
            return None

    request = module._active_request(now=now)
    assert request is not None
    module._write_ack(request, Child(), now=now)

    ack = json.loads(module.ACK.read_bytes())
    assert ack == {
        "acknowledged_at": now.isoformat(),
        "activation_sha256": hashlib.sha256(
            module.ACTIVATION.read_bytes()
        ).hexdigest(),
        "child_pid": 123,
        "expires_at": (now + timedelta(seconds=120)).isoformat(),
        "generation": 1,
        "litellm_sha256": hashlib.sha256(
            request.config.read_bytes()
        ).hexdigest(),
        "schema_version": 1,
        "state": "published",
    }
    assert module.ACK.read_bytes() == (
        json.dumps(ack, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def test_live_supervisor_removes_ack_when_the_acknowledged_child_crashes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _module()
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    _bundle(
        module,
        tmp_path,
        now=now - timedelta(seconds=1),
        expires_at=now + timedelta(seconds=120),
    )
    module.ACK_ROOT = tmp_path / "supervisor"
    module.ACK = module.ACK_ROOT / "ack.json"
    request = module._active_request(now=now)
    assert request is not None

    class CrashedChild:
        pid = 321
        returncode = 17

        def __init__(self) -> None:
            self.polls = 0

        def poll(self):
            self.polls += 1
            return None if self.polls == 1 else self.returncode

    child = CrashedChild()
    monkeypatch.setattr(module, "_active_request", lambda **_kwargs: request)
    monkeypatch.setattr(module, "_await_healthy", lambda _child: True)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(module.signal, "signal", lambda *_args: None)

    assert module.main() == 17
    assert not module.ACK.exists()


def test_live_supervisor_stops_published_child_at_exact_lease_expiry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _module()
    issued_at = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    expires_at = issued_at + timedelta(seconds=120)
    generated, bootstrap, _directory = _bundle(
        module,
        tmp_path,
        now=issued_at,
        expires_at=expires_at,
    )
    module.ACK_ROOT = tmp_path / "supervisor"
    module.ACK = module.ACK_ROOT / "ack.json"
    original_active_request = module._active_request
    original_selected = module._selected
    requests = iter(
        (
            original_active_request(now=issued_at),
            original_active_request(now=expires_at),
        )
    )
    assert original_active_request(now=issued_at) is not None
    assert original_active_request(now=expires_at) is None
    assert original_selected(now=expires_at) == bootstrap

    class LiveChild:
        pid = 654
        returncode = None

        def __init__(self) -> None:
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = 0

        def wait(self, timeout=None):
            del timeout
            return self.returncode

    class BootstrapCrash:
        pid = 987
        returncode = 23

        @staticmethod
        def poll():
            return 23

    published_child = LiveChild()
    bootstrap_child = BootstrapCrash()
    children = iter((published_child, bootstrap_child))
    commands: list[list[str]] = []

    def spawn(command, **_kwargs):
        commands.append(command)
        return next(children)

    monkeypatch.setattr(module, "_active_request", lambda **_kwargs: next(requests))
    monkeypatch.setattr(
        module,
        "_selected",
        lambda **_kwargs: bootstrap,
    )
    monkeypatch.setattr(module, "_await_healthy", lambda _child: True)
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    monkeypatch.setattr(module.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    assert module.main() == 23
    assert published_child.terminated is True
    assert commands[0][2] == str(generated)
    assert commands[1][2] == str(bootstrap)
    assert not module.ACK.exists()

def test_compose_mounts_one_read_only_route_volume_and_starts_bounded_supervisor() -> (
    None
):
    compose = (ROOT / "deploy/compose/compose.yaml").read_text()
    entrypoint = (ROOT / "deploy/compose/litellm/entrypoint.sh").read_text()
    source = SUPERVISOR.read_text()

    assert "route-publications:/routes" in compose
    assert "config_supervisor.py:/app/config-supervisor.py:ro" in compose
    assert "bootstrap-config.json:/app/bootstrap-config.json:ro" in compose
    assert "exec python /app/config-supervisor.py" in entrypoint
    assert "POLL_SECONDS = 2" in source
    assert "TERMINATE_SECONDS = 30" in source
    assert "shell=True" not in source


def test_compose_initializes_route_volume_for_unprivileged_control_worker() -> None:
    environment = os.environ.copy()
    for line in (ROOT / "deploy/compose/tests/test.env").read_text().splitlines():
        name, value = line.split("=", 1)
        environment[name] = value
    rendered = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(ROOT / "deploy/compose/compose.yaml"),
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    services = json.loads(rendered.stdout)["services"]
    initializer = services["route-publication-init"]

    assert initializer["network_mode"] == "none"
    assert initializer["user"] == "0:0"
    assert initializer["cap_drop"] == ["ALL"]
    assert set(initializer["cap_add"]) == {"CHOWN", "FOWNER"}
    command = initializer["command"][-1]
    reclaim = "os.chown('/routes', 0, 0)"
    child = "os.chown('/routes/generations', 10001, 10001)"
    root = "os.chown('/routes', 10001, 10001)"
    assert command.index(reclaim) < command.index("os.makedirs")
    assert command.index(child) < command.index(root)
    assert services["control-worker"]["depends_on"]["route-publication-init"] == {
        "condition": "service_completed_successfully",
        "required": True,
    }
    assert services["litellm"]["depends_on"]["route-publication-init"] == {
        "condition": "service_completed_successfully",
        "required": True,
    }
    litellm = services["litellm"]
    assert litellm["user"] == "10002:10001"
    assert litellm["cap_drop"] == ["ALL"]
    assert litellm["security_opt"] == ["no-new-privileges:true"]
    assert litellm["read_only"] is True
    assert "litellm-supervisor-state:/supervisor:rw" in (
        ROOT / "deploy/compose/compose.yaml"
    ).read_text()
    assert "litellm-supervisor-state:/supervisor:ro" in (
        ROOT / "deploy/compose/compose.yaml"
    ).read_text()


def test_development_image_compose_mounts_staged_acknowledging_supervisor() -> None:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(ROOT / "deploy/compose/compose.dev.images.yaml"),
            "config",
            "--format",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    services = json.loads(result.stdout)["services"]
    litellm = services["litellm"]
    worker = services["control-worker"]
    volumes = {volume["target"]: volume for volume in litellm["volumes"]}

    assert litellm["entrypoint"] == ["/app/vonk-entrypoint"]
    assert volumes["/app/bootstrap-config.json"]["volume"] == {
        "subpath": "litellm-bootstrap.json"
    }
    assert volumes["/app/vonk-entrypoint"]["volume"] == {
        "subpath": "litellm-entrypoint.sh"
    }
    assert volumes["/app/config-supervisor.py"]["volume"] == {
        "subpath": "litellm-supervisor.py"
    }
    assert volumes["/routes"]["read_only"] is True
    assert volumes["/supervisor"].get("read_only", False) is False

    worker_volumes = {
        volume["target"]: volume for volume in worker["volumes"]
    }
    assert worker_volumes["/routes"].get("read_only", False) is False
    assert worker_volumes["/supervisor"]["read_only"] is True
    assert worker["depends_on"]["litellm"] == {
        "condition": "service_healthy",
        "required": True,
    }
