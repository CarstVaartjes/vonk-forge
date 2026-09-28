from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SUPERVISOR = ROOT / "deploy/compose/litellm/config_supervisor.py"


def _module():
    sys.path.insert(0, str(ROOT / "agent_protocol/src/vonk_agent_protocol"))
    spec = importlib.util.spec_from_file_location(
        "litellm_config_supervisor", SUPERVISOR
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_supervisor_allows_first_run_database_migrations() -> None:
    module = _module()

    assert module.STARTUP_SECONDS == 120


def test_supervisor_prepares_the_non_root_prisma_query_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    cache = tmp_path / "prisma-cache"
    engine = cache / "nested" / "query-engine"
    monkeypatch.setattr(module, "_PRISMA_CACHE_ROOT", cache)
    monkeypatch.setenv(module._PRISMA_QUERY_ENGINE_ENV, str(engine))
    calls: list[dict[str, object]] = []

    def populate(command, **kwargs) -> None:
        calls.append({"command": command, **kwargs})
        engine.parent.mkdir(parents=True)
        engine.write_bytes(b"query-engine")
        engine.chmod(0o700)

    monkeypatch.setattr(module.subprocess, "run", populate)

    module._prepare_query_engine()

    assert [call["command"] for call in calls] == [["prisma", "-v"]]
    assert module._PRISMA_QUERY_ENGINE_ENV not in calls[0]["env"]


def test_supervisor_accepts_an_immutable_root_owned_query_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    cache = tmp_path / "prisma-cache"
    engine = cache / "query-engine"
    cache.mkdir()
    engine.write_bytes(b"query-engine")
    engine.chmod(0o555)
    monkeypatch.setattr(module, "_PRISMA_CACHE_ROOT", cache)
    monkeypatch.setenv(module._PRISMA_QUERY_ENGINE_ENV, str(engine))
    real_stat = Path.stat

    def root_owned_stat(path: Path, *, follow_symlinks: bool = True):
        metadata = real_stat(path, follow_symlinks=follow_symlinks)
        return types.SimpleNamespace(
            st_mode=metadata.st_mode,
            st_nlink=metadata.st_nlink,
            st_uid=0,
            st_size=metadata.st_size,
        )

    monkeypatch.setattr(Path, "stat", root_owned_stat)

    module._prepare_query_engine()


def test_supervisor_rejects_a_root_owned_writable_query_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    cache = tmp_path / "prisma-cache"
    engine = cache / "query-engine"
    cache.mkdir()
    engine.write_bytes(b"query-engine")
    engine.chmod(0o575)
    monkeypatch.setattr(module, "_PRISMA_CACHE_ROOT", cache)
    monkeypatch.setenv(module._PRISMA_QUERY_ENGINE_ENV, str(engine))
    real_stat = Path.stat

    def root_owned_stat(path: Path, *, follow_symlinks: bool = True):
        metadata = real_stat(path, follow_symlinks=follow_symlinks)
        return types.SimpleNamespace(
            st_mode=metadata.st_mode,
            st_nlink=metadata.st_nlink,
            st_uid=0,
            st_size=metadata.st_size,
        )

    monkeypatch.setattr(Path, "stat", root_owned_stat)

    with pytest.raises(RuntimeError, match="cache is unsafe"):
        module._prepare_query_engine()


def test_supervisor_selects_the_single_native_prisma_query_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    cache = tmp_path / "prisma-cache"
    configured = cache / "engines" / "query-engine-debian-openssl-3.0.x"
    native = cache / "engines" / "query-engine-linux-arm64-openssl-3.0.x"
    monkeypatch.setattr(module, "_PRISMA_CACHE_ROOT", cache)
    monkeypatch.setenv(module._PRISMA_QUERY_ENGINE_ENV, str(configured))

    def populate(command, **kwargs) -> None:
        assert command == ["prisma", "-v"]
        native.parent.mkdir(parents=True)
        native.write_bytes(b"native-query-engine")
        native.chmod(0o700)

    monkeypatch.setattr(module.subprocess, "run", populate)

    module._prepare_query_engine()

    assert os.environ[module._PRISMA_QUERY_ENGINE_ENV] == str(native)


def test_supervisor_rejects_ambiguous_native_prisma_query_engines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    cache = tmp_path / "prisma-cache"
    configured = cache / "engines" / "query-engine-configured"
    monkeypatch.setattr(module, "_PRISMA_CACHE_ROOT", cache)
    monkeypatch.setenv(module._PRISMA_QUERY_ENGINE_ENV, str(configured))

    def populate(command, **kwargs) -> None:
        assert command == ["prisma", "-v"]
        configured.parent.mkdir(parents=True)
        for name in ("query-engine-native-a", "query-engine-native-b"):
            candidate = configured.with_name(name)
            candidate.write_bytes(b"query-engine")
            candidate.chmod(0o700)

    monkeypatch.setattr(module.subprocess, "run", populate)

    with pytest.raises(RuntimeError, match="was not populated"):
        module._prepare_query_engine()


def test_supervisor_rejects_a_query_engine_outside_its_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    monkeypatch.setattr(module, "_PRISMA_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setenv(
        module._PRISMA_QUERY_ENGINE_ENV,
        str(tmp_path / "outside" / "query-engine"),
    )

    with pytest.raises(RuntimeError, match="outside its cache"):
        module._prepare_query_engine()


def test_supervisor_recovers_from_a_transient_pre_health_child_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    bootstrap = tmp_path / "bootstrap.json"
    bootstrap.write_text('{"model_list":[]}\n')
    module.ACK_ROOT = tmp_path / "supervisor"
    module.ACK = module.ACK_ROOT / "ack.json"
    module.ACK_ROOT.mkdir()
    module.ACK.write_text("stale\n")

    class Child:
        def __init__(self, *, pid: int, returncode: int) -> None:
            self.pid = pid
            self.returncode = returncode

        def poll(self) -> int:
            return self.returncode

    children = iter(
        (
            Child(pid=101, returncode=70),
            Child(pid=102, returncode=23),
        )
    )
    health = iter((False, True))
    spawns: list[Child] = []
    selections = 0
    retry_delays: list[float] = []

    def spawn(*_args, **_kwargs) -> Child:
        child = next(children)
        spawns.append(child)
        return child

    def active_request(**_kwargs):
        nonlocal selections
        selections += 1

    def retry_sleep(seconds: float) -> None:
        assert not module.ACK.exists()
        retry_delays.append(seconds)

    monkeypatch.setattr(module, "_active_request", active_request)
    monkeypatch.setattr(module, "_selected", lambda **_kwargs: bootstrap)
    monkeypatch.setattr(
        module, "_await_healthy", lambda _child, **_kwargs: next(health)
    )
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    monkeypatch.setattr(module.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(module.time, "sleep", retry_sleep)

    assert module._supervise() == 23
    assert [child.pid for child in spawns] == [101, 102]
    assert selections == 2
    assert retry_delays == [1]
    assert not module.ACK.exists()


def test_supervisor_bounds_pre_health_child_exit_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    bootstrap = tmp_path / "bootstrap.json"
    bootstrap.write_text('{"model_list":[]}\n')
    module.ACK_ROOT = tmp_path / "supervisor"
    module.ACK = module.ACK_ROOT / "ack.json"
    spawns = 0
    retry_delays: list[float] = []

    class Child:
        pid = 101
        returncode = 70

        @staticmethod
        def poll() -> int:
            return 70

    def spawn(*_args, **_kwargs) -> Child:
        nonlocal spawns
        spawns += 1
        return Child()

    monkeypatch.setattr(module, "_active_request", lambda **_kwargs: None)
    monkeypatch.setattr(module, "_selected", lambda **_kwargs: bootstrap)
    monkeypatch.setattr(module, "_await_healthy", lambda _child, **_kwargs: False)
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    monkeypatch.setattr(module.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(module.time, "sleep", retry_delays.append)

    assert module._supervise() == 1
    assert spawns == 10
    assert retry_delays == [1] * 9


def test_supervisor_does_not_retry_a_live_child_after_the_health_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    bootstrap = tmp_path / "bootstrap.json"
    bootstrap.write_text('{"model_list":[]}\n')
    module.ACK_ROOT = tmp_path / "supervisor"
    module.ACK = module.ACK_ROOT / "ack.json"
    spawns = 0

    class Child:
        pid = 101
        returncode = None
        terminated = False

        def poll(self):
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = 0

        def wait(self, timeout=None):
            del timeout
            return self.returncode

    child = Child()

    def spawn(*_args, **_kwargs) -> Child:
        nonlocal spawns
        spawns += 1
        return child

    monkeypatch.setattr(module, "_active_request", lambda **_kwargs: None)
    monkeypatch.setattr(module, "_selected", lambda **_kwargs: bootstrap)
    monkeypatch.setattr(module, "_await_healthy", lambda _child, **_kwargs: False)
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    monkeypatch.setattr(module.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(
        module.time,
        "sleep",
        lambda _seconds: pytest.fail("live unhealthy child was retried"),
    )

    assert module._supervise() == 1
    assert spawns == 1
    assert child.terminated is True


def _bundle(
    module,
    tmp_path: Path,
    *,
    generation: int = 1,
):
    root = tmp_path / "routes"
    config = b'{"model_list":[{"model_name":"chat"}]}\n'
    routes = b'{"routes":{"chat":{}},"state":"published"}\n'
    manifest = {
        "schema_version": 2,
        "generation": generation,
        "state": "published",
        "authority_id": "bb7aac18-edbf-4cc1-bafd-15e282557c53",
        "plan_digest": "a" * 64,
        "evidence_set_digest": "b" * 64,
        "routes_sha256": hashlib.sha256(routes).hexdigest(),
        "litellm_sha256": hashlib.sha256(config).hexdigest(),
    }
    manifest_bytes = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    directory_name = f"{generation:08d}-{manifest_digest}"
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


def test_supervisor_selects_only_an_exact_activation_bundle(
    tmp_path: Path,
) -> None:
    module = _module()
    generated, _bootstrap, _directory = _bundle(
        module,
        tmp_path,
    )

    assert module._selected() == generated


def test_supervisor_rejects_a_hash_mismatch(tmp_path: Path) -> None:
    module = _module()
    generated, bootstrap, _directory = _bundle(
        module,
        tmp_path,
    )
    generated.write_bytes(b'{"model_list":[{"unsafe":true}]}\n')
    assert module._selected() == bootstrap


def test_supervisor_falls_back_when_manifest_or_marker_is_not_exact(
    tmp_path: Path,
) -> None:
    module = _module()
    _generated, bootstrap, directory = _bundle(
        module,
        tmp_path,
    )
    manifest = json.loads((directory / "manifest.json").read_bytes())
    manifest["plan_digest"] = "f" * 64
    (directory / "manifest.json").write_text(json.dumps(manifest))
    assert module._selected() == bootstrap

    _generated, bootstrap, _directory = _bundle(
        module,
        tmp_path,
    )
    activation = json.loads(module.ACTIVATION.read_bytes())
    module.ACTIVATION.write_text(json.dumps(activation, indent=2))
    assert module._selected() == bootstrap

    _generated, bootstrap, _directory = _bundle(
        module,
        tmp_path,
    )
    activation = json.loads(module.ACTIVATION.read_bytes())
    activation["unknown"] = True
    module.ACTIVATION.write_text(json.dumps(activation))
    assert module._selected() == bootstrap


def test_supervisor_ack_binds_a_live_child_to_the_exact_activation_request(
    tmp_path: Path,
) -> None:
    module = _module()
    now = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    _generated, _bootstrap, _directory = _bundle(
        module,
        tmp_path,
    )
    module.ACK_ROOT = tmp_path / "supervisor"
    module.ACK = module.ACK_ROOT / "ack.json"

    class Child:
        pid = 123

        @staticmethod
        def poll():
            return None

    request = module._active_request()
    assert request is not None
    module._write_ack(request, Child(), now=now)

    ack = json.loads(module.ACK.read_bytes())
    assert ack == {
        "acknowledged_at": now.isoformat(),
        "activation_sha256": hashlib.sha256(module.ACTIVATION.read_bytes()).hexdigest(),
        "child_pid": 123,
        "generation": 1,
        "litellm_sha256": hashlib.sha256(request.config.read_bytes()).hexdigest(),
        "schema_version": 1,
        "state": "published",
    }
    assert (
        module.ACK.read_bytes()
        == (json.dumps(ack, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )


def test_live_supervisor_removes_ack_when_the_acknowledged_child_crashes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _module()
    _bundle(module, tmp_path)
    module.ACK_ROOT = tmp_path / "supervisor"
    module.ACK = module.ACK_ROOT / "ack.json"
    request = module._active_request()
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
    monkeypatch.setattr(module, "_await_healthy", lambda _child, **_kwargs: True)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(module.signal, "signal", lambda *_args: None)

    assert module.main() == 17
    assert not module.ACK.exists()


def _live_child(pid: int):
    class LiveChild:
        returncode = None
        terminated = False

        def poll(self):
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = 0

        def wait(self, timeout=None):
            del timeout
            return self.returncode

    child = LiveChild()
    child.pid = pid
    return child


def test_live_supervisor_reloads_only_when_a_new_generation_changes_routes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _module()
    _bundle(module, tmp_path)
    first = module._active_request()
    _bundle(
        module,
        tmp_path,
        generation=2,
    )
    renewed = module._active_request()
    withdrawn_config = tmp_path / "withdrawn.json"
    withdrawn_config.write_text('{"model_list":[]}\n')
    withdrawn = module.ActiveRequest(
        withdrawn_config, {**renewed.marker, "generation": 3}, "c" * 64
    )
    module.ACK_ROOT = tmp_path / "supervisor"
    module.ACK = module.ACK_ROOT / "ack.json"
    requests = iter((first, renewed, withdrawn, withdrawn))
    children = [_live_child(456), _live_child(457)]
    spawns: list[str] = []
    acknowledged: list[int] = []

    def spawn(command, **_kwargs):
        spawns.append(command[2])
        return children[len(spawns) - 1]

    def write_ack(request, loaded_child, *, now):
        del now
        acknowledged.append(request.marker["generation"])
        if loaded_child is children[1]:
            loaded_child.returncode = 31

    monkeypatch.setattr(module, "_active_request", lambda: next(requests))
    monkeypatch.setattr(module, "_await_healthy", lambda _child, **_kwargs: True)
    monkeypatch.setattr(module, "_healthy", lambda _child: True)
    monkeypatch.setattr(module, "_write_ack", write_ack)
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    monkeypatch.setattr(module.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    assert module.main() == 31
    # Same-config renewal is acknowledged in place; the withdrawal reloads.
    assert spawns == [str(first.config), str(withdrawn_config)]
    assert acknowledged == [1, 2, 3]
    assert children[0].terminated is True


def test_compose_mounts_one_read_only_route_volume_and_starts_bounded_supervisor() -> (
    None
):
    compose = (ROOT / "deploy/compose/compose.yaml").read_text()
    entrypoint = (ROOT / "deploy/compose/litellm/entrypoint.sh").read_text()
    source = SUPERVISOR.read_text()

    assert "route-publications:/routes" in compose
    assert "runtime-assets:/run/vonk-runtime-assets:ro" in compose
    dockerfile = (ROOT / "control/Dockerfile").read_text()
    assert "deploy/compose/litellm/config_supervisor.py" in dockerfile
    assert "deploy/compose/litellm/bootstrap-config.json" in dockerfile
    assert (
        "exec python /run/vonk-runtime-assets/litellm/config_supervisor.py"
        in entrypoint
    )
    assert "POLL_SECONDS = 2" in source
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
    api = services["control-api"]
    assert api["user"] == "0:0"
    assert api["cap_drop"] == ["ALL"]
    assert set(api["cap_add"]) == {
        "CHOWN",
        "FOWNER",
        "DAC_OVERRIDE",
        "SETUID",
        "SETGID",
    }
    assert services["control-worker"]["depends_on"]["control-api"] == {
        "condition": "service_healthy",
        "required": True,
    }
    assert services["litellm"]["depends_on"]["control-api"] == {
        "condition": "service_started",
        "required": False,
        "restart": True,
    }
    litellm = services["litellm"]
    assert litellm["user"] == "10002:10001"
    assert litellm["cap_drop"] == ["ALL"]
    assert litellm["security_opt"] == ["no-new-privileges:true"]
    assert litellm["read_only"] is True
    assert (
        "litellm-supervisor-state:/supervisor:rw"
        in (ROOT / "deploy/compose/compose.yaml").read_text()
    )
    assert (
        "litellm-supervisor-state:/supervisor:ro"
        in (ROOT / "deploy/compose/compose.yaml").read_text()
    )


def test_development_image_compose_mounts_staged_acknowledging_supervisor() -> None:
    environment = os.environ.copy()
    for line in (ROOT / "deploy/compose/tests/test.env").read_text().splitlines():
        if line and not line.startswith("#"):
            name, value = line.split("=", 1)
            environment[name] = value
    result = subprocess.run(
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
    services = json.loads(result.stdout)["services"]
    litellm = services["litellm"]
    worker = services["control-worker"]
    volumes = {volume["target"]: volume for volume in litellm["volumes"]}

    assert litellm["entrypoint"][:2] == ["/bin/sh", "-c"]
    assert litellm["entrypoint"][2].endswith(
        "exec /bin/sh /run/vonk-runtime-assets/litellm/entrypoint.sh"
    )
    assert volumes["/routes"]["read_only"] is True
    assert volumes["/supervisor"].get("read_only", False) is False
    assert volumes["/run/vonk-normalized-secrets"]["read_only"] is True

    worker_volumes = {volume["target"]: volume for volume in worker["volumes"]}
    assert worker_volumes["/routes"].get("read_only", False) is False
    assert worker_volumes["/supervisor"]["read_only"] is True
    assert "control-signer" not in worker["depends_on"]


@pytest.mark.parametrize("health_after,accepted", [(90, True), (121, False)])
def test_health_must_arrive_within_the_total_startup_window(
    monkeypatch, health_after, accepted
):
    module = _module()
    elapsed = [0.0]
    child = types.SimpleNamespace(poll=lambda: None)

    def healthy(_child):
        elapsed[0] += health_after
        return True

    monkeypatch.setattr(module.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(module, "_healthy", healthy)
    assert module._await_healthy(child, deadline=120) is accepted


def test_retries_share_one_cumulative_startup_deadline(tmp_path, monkeypatch):
    module = _module()
    bootstrap = tmp_path / "bootstrap.json"
    bootstrap.write_text('{"model_list":[]}\n')
    module.ACK = tmp_path / "ack.json"
    elapsed = [0.0]
    attempts = []

    def spawn(*_args, **_kwargs):
        attempts.append(elapsed[0])
        elapsed[0] += 60
        return types.SimpleNamespace(pid=123, poll=lambda: 70)

    monkeypatch.setattr(module, "_active_request", lambda **_kwargs: None)
    monkeypatch.setattr(module, "_selected", lambda **_kwargs: bootstrap)
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    monkeypatch.setattr(module.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(module.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(
        module.time,
        "sleep",
        lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
    )
    assert module._supervise() == 1
    assert attempts == [0, 61]


def test_preparation_consumes_the_same_startup_deadline(monkeypatch):
    module = _module()
    elapsed = [0.0]

    def prepare(*, deadline):
        assert deadline == 120
        elapsed[0] = 90

    def supervise(*, startup_deadline):
        assert startup_deadline - elapsed[0] == 30
        return 0

    monkeypatch.setattr(module.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(module, "_prepare_query_engine", prepare)
    monkeypatch.setattr(module, "_supervise", supervise)
    assert module.main() == 0


@pytest.mark.parametrize("reaped", [True, False])
def test_shutdown_reserves_bounded_kill_reap_and_requires_confirmed_exit(
    monkeypatch, reaped
):
    module = _module()
    elapsed = [0.0]
    killed = []
    waits = []
    exited = []

    def wait(*, timeout):
        waits.append(timeout)
        elapsed[0] += timeout
        if killed and reaped:
            exited.append(True)
            return -9
        raise subprocess.TimeoutExpired("child", timeout)

    child = types.SimpleNamespace(
        poll=lambda: -9 if exited else None,
        terminate=lambda: None,
        wait=wait,
        kill=lambda: killed.append(True),
    )
    monkeypatch.setattr(module.time, "monotonic", lambda: elapsed[0])
    if reaped:
        module._stop(child)
    else:
        with pytest.raises(RuntimeError, match="exit was not confirmed"):
            module._stop(child)
    assert elapsed[0] == 30
    assert waits == [25, 5]
    assert killed == [True]


@pytest.mark.parametrize("bootstrap", [True, False])
def test_new_generation_interrupts_old_startup_and_owns_a_full_budget(
    tmp_path, monkeypatch, bootstrap
):
    module = _module()
    config = tmp_path / "config.json"
    config.write_text('{"model_list":[]}\n')
    module.ACK = tmp_path / "ack.json"
    old = (
        None if bootstrap else module.ActiveRequest(config, {"generation": 1}, "a" * 64)
    )
    generation = 1 if bootstrap else 2
    new = module.ActiveRequest(config, {"generation": generation}, "b" * 64)
    current = [old]
    elapsed = [0.0]
    children = []
    spawns = []
    acknowledgements = []

    def spawn(*_args, **_kwargs):
        assert not children or children[-1].returncode == 0
        child = types.SimpleNamespace(pid=100 + len(children), returncode=None)
        child.poll = lambda: child.returncode

        def terminate():
            elapsed[0] += 2
            child.returncode = 0

        child.terminate = terminate
        children.append(child)
        spawns.append(elapsed[0])
        return child

    def healthy(child):
        elapsed[0] += 90
        if child is children[0]:
            current[0] = new
            return False
        return True

    def write_ack(request, child, **_kwargs):
        acknowledgements.append((request.marker["generation"], elapsed[0]))
        child.returncode = 23

    monkeypatch.setattr(module, "_active_request", lambda **_kwargs: current[0])
    monkeypatch.setattr(module, "_selected", lambda **_kwargs: config)
    monkeypatch.setattr(module, "_healthy", healthy)
    monkeypatch.setattr(module, "_write_ack", write_ack)
    monkeypatch.setattr(module.subprocess, "Popen", spawn)
    monkeypatch.setattr(module.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(module.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(
        module.time,
        "sleep",
        lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
    )
    assert module._supervise() == 23
    assert spawns == [0, 94]
    assert acknowledgements == [(generation, 184)]
