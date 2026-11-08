"""NAS daemon retention at the inventory/delete/persistent-state boundary."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/compose/image-janitor/janitor.py"
spec = importlib.util.spec_from_file_location("nas_image_janitor", SCRIPT)
assert spec is not None and spec.loader is not None
janitor = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = janitor
spec.loader.exec_module(janitor)


def identity(number: int) -> str:
    return f"sha256:{number:064x}"


def image(
    number: int, *, source: str | None = janitor.SOURCE, reference: str | None = None
) -> dict[str, Any]:
    return {
        "Id": identity(number),
        "Size": 1234,
        "Labels": {"org.opencontainers.image.source": source} if source else {},
        "RepoDigests": [reference] if reference else [],
        "RepoTags": [],
    }


class Daemon(janitor.Docker):
    def __init__(self, images: list[dict[str, Any]]) -> None:
        self.inventory = {row["Id"]: row for row in images}
        self.containers: list[dict[str, str]] = []
        self.fail = False
        self.race = False
        self.deleted: list[str] = []

    def request(self, method: str, path: str, body: Any = None) -> Any:
        assert body is None
        if method == "GET":
            if path == "/images/json?all=1":
                return list(self.inventory.values())
            assert path == "/containers/json?all=1"  # A running-only scan is unsafe.
            return self.containers
        assert method == "DELETE" and path.endswith("?force=false&noprune=true")
        selected = unquote(path.split("/images/", 1)[1].split("?", 1)[0])
        assert janitor.IMAGE_ID.fullmatch(selected)  # Never delete a moved tag.
        if self.race:
            self.containers = [{"ImageID": selected, "State": "exited"}]
        if self.fail or any(row["ImageID"] == selected for row in self.containers):
            raise janitor.DockerError("Docker HTTP 409: conflict")
        del self.inventory[selected]
        self.deleted.append(selected)
        return [{"Deleted": selected}]


def test_expiry_uses_continuous_unused_grace_and_survives_process_restart(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Catches build-age expiry, pressure-only collection, lost clocks and running-only protection."""
    daemon = Daemon(
        [
            image(1),  # Dangling, source-labelled platform build.
            image(
                2,
                source=None,
                reference="ghcr.io/carstvaartjes/vonk-forge-api@sha256:" + "a" * 64,
            ),
            image(3, source="https://github.com/other/project"),
            image(4),
            image(5),
        ]
    )
    daemon.containers = [
        {"ImageID": identity(4), "State": "running"},
        {"ImageID": identity(5), "State": "exited"},
    ]
    state = tmp_path / "unused.json"
    now = 100_000
    janitor.sweep(daemon, state, now)
    janitor.sweep(daemon, state, now + janitor.GRACE - 1)
    assert daemon.deleted == []
    # Reconstruct the consumer like a fresh process. There is no disk-pressure
    # input: even abundant free space must not defer expiry.
    restarted = Daemon(list(daemon.inventory.values()))
    restarted.containers = daemon.containers
    with caplog.at_level("INFO", logger=janitor.LOGGER.name):
        janitor.sweep(restarted, state, now + janitor.GRACE)
    assert restarted.deleted == [identity(1), identity(2)]
    assert set(restarted.inventory) == {identity(3), identity(4), identity(5)}
    receipts = [
        json.loads(record.message)
        for record in caplog.records
        if json.loads(record.message)["reason"] == janitor.Reason.REMOVED
    ]
    assert receipts[0]["receipts"] == [{"Deleted": identity(1)}]
    assert json.loads(state.read_text()) == {}


def test_reuse_resets_grace_and_failed_delete_retries_after_restart(
    tmp_path: Path,
) -> None:
    """Catches premature removal after reuse and poisoning an image on a transient refusal."""
    daemon = Daemon([image(1)])
    state = tmp_path / "unused.json"
    now = 100_000
    janitor.sweep(daemon, state, now)
    daemon.containers = [{"ImageID": identity(1), "State": "exited"}]
    janitor.sweep(daemon, state, now + janitor.GRACE)
    daemon.containers = []
    janitor.sweep(daemon, state, now + janitor.GRACE + 1)
    daemon.fail = True
    janitor.sweep(daemon, state, now + 2 * janitor.GRACE + 1)
    assert daemon.deleted == []
    assert json.loads(state.read_text())[identity(1)] == now + janitor.GRACE + 1
    restarted = Daemon(list(daemon.inventory.values()))
    janitor.sweep(restarted, state, now + 2 * janitor.GRACE + 1 + janitor.INTERVAL)
    assert restarted.deleted == [identity(1)]


def test_container_creation_race_preserves_image_and_restarts_grace(
    tmp_path: Path,
) -> None:
    """Catches forcing deletion after a container appears between inventory and removal."""
    daemon = Daemon([image(1)])
    state = tmp_path / "unused.json"
    janitor.sweep(daemon, state, 100_000)
    daemon.race = True
    janitor.sweep(daemon, state, 100_000 + janitor.GRACE)
    assert daemon.deleted == []
    assert json.loads(state.read_text()) == {}
    daemon.race = False
    daemon.containers = []
    janitor.sweep(daemon, state, 100_001 + janitor.GRACE)
    assert daemon.deleted == []


def test_corrupt_state_restarts_grace_instead_of_deleting_or_stalling(
    tmp_path: Path,
) -> None:
    """Catches trusting damaged timestamps or requiring manual state repair."""
    daemon = Daemon([image(1)])
    state = tmp_path / "unused.json"
    state.write_text('{"broken": -1}')
    janitor.sweep(daemon, state, 100_000)
    assert daemon.deleted == []
    assert json.loads(state.read_text()) == {identity(1): 100_000}


class Killed(BaseException):
    """The janitor process dies (SIGKILL): no `finally` of it runs any more."""


class Networks(janitor.Docker):
    """Daemon network semantics the repair relies on, as observed on Docker 29."""

    def __init__(self) -> None:
        self.networks: dict[str, dict[str, Any]] = {}
        self.containers: dict[str, dict[str, Any]] = {}
        self.mutations: list[tuple[str, str]] = []
        self.network_reads: list[str] = []
        self.refuse_connect = False
        self.stop_times_out = False
        self.start_refusals = 0
        self.die_after: str | None = None
        self.dead = False
        self.endpoint_rebuilt = True

    def network(self, name: str) -> None:
        self.networks[name] = {"Id": uuid.uuid4().hex, "Containers": {}}

    def container(
        self, name: str, networks: list[str], *, mode: str | None = None
    ) -> str:
        identity = uuid.uuid4().hex
        self.containers[identity] = {
            "Id": identity,
            "Name": f"/{name}",
            "Created": "1970-01-01T00:00:00.123456789Z",
            "State": {"Status": "created", "Error": ""},
            "HostConfig": {
                "NetworkMode": mode or networks[0],
                "RestartPolicy": {"Name": "unless-stopped"},
            },
            "NetworkSettings": {
                "Networks": {
                    network: {"NetworkID": "", "Aliases": [name, name.split("-")[2]]}
                    for network in networks
                }
            },
        }
        self.start(identity)
        return identity

    def running(self, identity: str) -> bool:
        return self.containers[identity]["State"]["Status"] == "running"

    def start(self, identity: str) -> None:
        container = self.containers[identity]
        entries = container["NetworkSettings"]["Networks"]
        for name, entry in entries.items():
            current = self.networks.get(name)
            if current is None or entry["NetworkID"] not in ("", current["Id"]):
                container["State"].update(Status="exited", Error="network not found")
                raise janitor.DockerError("Docker HTTP 500: network not found")
        for name, entry in entries.items():
            entry["NetworkID"] = self.networks[name]["Id"]
            if self.endpoint_rebuilt:
                self.networks[name]["Containers"][identity] = {}
        container["State"].update(Status="running", Error="")

    def stop(self, identity: str) -> None:
        # An API stop is a manual stop: it leaves no error to retry on.
        self.containers[identity]["State"].update(Status="exited", Error="")
        for network in self.networks.values():
            network["Containers"].pop(identity, None)

    def request(self, method: str, path: str, body: Any = None) -> Any:
        if self.dead:
            raise Killed
        route = unquote(path.split("?", 1)[0])
        if method == "GET" and route == "/containers/json":
            assert "all=1" in path and "vonk-forge" in unquote(path)
            return [
                {"Id": identity, "Names": [row["Name"]]}
                for identity, row in self.containers.items()
            ]
        kind, name, *action = route.strip("/").split("/")
        if method == "GET" and kind == "containers":
            return json.loads(json.dumps(self.containers[name]))
        if method == "GET" and kind == "networks":
            self.network_reads.append(name)
            if name not in self.networks:
                raise janitor.DockerNotFound("Docker HTTP 404: network not found")
            return json.loads(json.dumps(self.networks[name]))
        self.mutations.append((action[0], name))
        try:
            return self._mutate(kind, name, action[0], body)
        finally:
            if self.die_after == action[0]:
                self.dead = True

    def _mutate(self, kind: str, name: str, action: str, body: Any) -> None:
        if kind == "containers" and action == "stop":
            self.stop(name)
            if self.stop_times_out:
                raise TimeoutError("timed out")  # The daemon finished anyway.
            return
        if kind == "containers":
            if self.start_refusals:
                self.start_refusals -= 1
                raise janitor.DockerError("Docker HTTP 500: container is stopping")
            self.start(name)
            return
        record = self.containers[body["Container"]]
        assert record["State"]["Status"] != "running"  # A running one cannot leave.
        if action == "disconnect":
            del record["NetworkSettings"]["Networks"][name]
            return
        if self.refuse_connect:
            raise janitor.DockerError("Docker HTTP 500: connect refused")
        record["NetworkSettings"]["Networks"][name] = {
            "NetworkID": "",
            **body["EndpointConfig"],
        }
        return


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def fleet(tmp_path: Path) -> tuple[Networks, Any, Clock, str, str]:
    daemon = Networks()
    for name in ("vonk-forge_ingress", "vonk-forge_agent-proxy", "vonk-forge_data"):
        daemon.network(name)
    caddy = daemon.container(
        "vonk-forge-caddy-1", ["vonk-forge_ingress", "vonk-forge_agent-proxy"]
    )
    api = daemon.container(
        "vonk-forge-api-1", ["vonk-forge_agent-proxy", "vonk-forge_data"]
    )
    clock = Clock()
    reconciler = janitor.Reconciler(
        daemon, tmp_path / "repairing.json", clock=clock, jitter=lambda: 1.0
    )
    return daemon, reconciler, clock, caddy, api


def ticks(reconciler: Any, clock: Clock, count: int) -> None:
    for _ in range(count):
        reconciler.tick("vonk-forge")
        clock.now += janitor.NETWORK_INTERVAL


def test_recreated_network_reattaches_the_same_stopped_container_and_runs_it(
    tmp_path: Path,
) -> None:
    """Catches the NAS update fault that only a manual container delete used to clear."""
    daemon, reconciler, clock, caddy, api = fleet(tmp_path)
    daemon.stop(caddy)
    daemon.network("vonk-forge_ingress")  # Recreated with a new ID.
    with pytest.raises(janitor.DockerError):
        daemon.start(caddy)  # What `compose up` and a restart do.
    ticks(reconciler, clock, 1)
    record = daemon.containers[caddy]
    assert daemon.running(caddy)
    assert caddy in daemon.networks["vonk-forge_ingress"]["Containers"]
    assert record["NetworkSettings"]["Networks"]["vonk-forge_ingress"]["Aliases"] == [
        "vonk-forge-caddy-1",
        "caddy",
    ]
    assert daemon.mutations == [
        ("disconnect", "vonk-forge_ingress"),
        ("connect", "vonk-forge_ingress"),
        ("start", caddy),
    ]
    daemon.mutations.clear()
    ticks(reconciler, clock, 2)
    assert daemon.mutations == []
    assert daemon.running(api)


def test_running_container_without_its_endpoint_is_reattached(tmp_path: Path) -> None:
    """Catches trusting the container record while the network has no endpoint."""
    daemon, reconciler, clock, caddy, _ = fleet(tmp_path)
    daemon.networks["vonk-forge_ingress"]["Containers"].clear()
    ticks(reconciler, clock, 1)
    assert daemon.mutations == [
        ("stop", caddy),
        ("disconnect", "vonk-forge_ingress"),
        ("connect", "vonk-forge_ingress"),
        ("start", caddy),
    ]
    assert caddy in daemon.networks["vonk-forge_ingress"]["Containers"]
    assert json.loads((tmp_path / "repairing.json").read_text()) == {}


def test_each_network_is_inspected_once_per_tick(tmp_path: Path) -> None:
    """Catches one network inspect per container sharing it."""
    daemon, reconciler, clock, _, _ = fleet(tmp_path)
    ticks(reconciler, clock, 1)
    assert sorted(daemon.network_reads) == sorted(daemon.networks)


def test_container_that_joined_after_the_shared_snapshot_is_left_alone(
    tmp_path: Path,
) -> None:
    """Catches judging a container detached from a network snapshot taken before it started."""
    daemon, reconciler, clock, _, api = fleet(tmp_path)
    daemon.containers[api]["State"]["Status"] = (
        "created"  # Compose has not started it yet.
    )
    del daemon.networks["vonk-forge_agent-proxy"]["Containers"][api]
    read = daemon.request

    def request(method: str, path: str, body: Any = None) -> Any:
        row = read(method, path, body)
        if method == "GET" and path.endswith("vonk-forge_agent-proxy"):
            daemon.request = read  # type: ignore[method-assign]
            daemon.start(api)  # Compose starts it after the snapshot was taken.
        return row

    daemon.request = request  # type: ignore[method-assign]
    daemon.mutations.clear()
    ticks(reconciler, clock, 1)
    assert daemon.mutations == []
    assert daemon.running(api)


def test_container_that_exits_between_its_inspect_and_the_fresh_read_is_left_alone(
    tmp_path: Path,
) -> None:
    """Catches condemning a container from an inspect that predates its exit."""
    daemon, reconciler, clock, _, api = fleet(tmp_path)
    del daemon.networks["vonk-forge_agent-proxy"]["Containers"][api]
    read = daemon.request
    reads: list[str] = []

    def request(method: str, path: str, body: Any = None) -> Any:
        row = read(method, path, body)
        if method == "GET" and path.endswith("vonk-forge_agent-proxy"):
            reads.append(path)
            if len(reads) == 2:  # The fresh read; the container exits meanwhile.
                daemon.containers[api]["State"]["Status"] = "exited"
        return row

    daemon.request = request  # type: ignore[method-assign]
    ticks(reconciler, clock, 1)
    assert len(reads) == 2
    assert daemon.mutations == []


def test_stop_that_times_out_after_the_daemon_stopped_is_started_later(
    tmp_path: Path,
) -> None:
    """Catches a repair stop that Docker records as manual and never restarts."""
    daemon, reconciler, clock, caddy, _ = fleet(tmp_path)
    daemon.networks["vonk-forge_ingress"]["Containers"].clear()
    daemon.stop_times_out = True
    daemon.start_refusals = 1  # The daemon is still finishing the stop.
    ticks(reconciler, clock, 1)
    assert not daemon.running(caddy)
    assert daemon.containers[caddy]["State"]["Error"] == ""
    daemon.stop_times_out = False
    ticks(reconciler, clock, 1)
    assert daemon.running(caddy)
    assert caddy in daemon.networks["vonk-forge_ingress"]["Containers"]


@pytest.mark.parametrize("death", ["stop", "disconnect", "connect"])
def test_janitor_killed_mid_repair_leaves_the_next_process_to_start_it(
    tmp_path: Path, death: str
) -> None:
    """Catches a container left stopped when the janitor dies between stop and start."""
    daemon, reconciler, clock, caddy, _ = fleet(tmp_path)
    daemon.networks["vonk-forge_ingress"]["Containers"].clear()
    daemon.die_after = death
    with pytest.raises(Killed):
        ticks(reconciler, clock, 1)
    assert not daemon.running(caddy)
    daemon.dead, daemon.die_after = False, None
    fresh = janitor.Reconciler(daemon, tmp_path / "repairing.json", clock=clock)
    ticks(fresh, clock, 2)
    assert daemon.running(caddy)
    assert caddy in daemon.networks["vonk-forge_ingress"]["Containers"]


def test_sigterm_unwinds_through_the_start(tmp_path: Path) -> None:
    """Catches a Redeploy that stops the janitor between stop and start."""
    daemon, reconciler, clock, caddy, _ = fleet(tmp_path)
    daemon.networks["vonk-forge_ingress"]["Containers"].clear()
    original = daemon._mutate

    def terminated(kind: str, name: str, action: str, body: Any) -> None:
        original(kind, name, action, body)
        if action == "disconnect":
            janitor.terminate(signal.SIGTERM, None)

    daemon._mutate = terminated  # type: ignore[method-assign]
    with pytest.raises(SystemExit):
        ticks(reconciler, clock, 1)
    assert daemon.running(caddy)


def test_failed_first_start_is_retried_and_docker_owned_states_wait(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Catches dropping `created` with an error, and acting on Docker-owned states."""
    daemon, reconciler, clock, caddy, api = fleet(tmp_path)
    daemon.containers[caddy]["State"].update(Status="created", Error="no such file")
    for state in ("restarting", "paused", "dead"):
        daemon.containers[api]["State"].update(Status=state, Error="boom")
        with caplog.at_level("INFO", logger=janitor.LOGGER.name):
            ticks(reconciler, clock, 1)
        assert ("start", api) not in daemon.mutations
        assert {
            "reason": janitor.Reason.STATE_WAIT,
            "container": "vonk-forge-api-1",
            "state": state,
        } in [json.loads(record.message) for record in caplog.records]
    assert daemon.running(caddy)


def test_repair_waits_for_compose_and_respects_stops_of_a_running_container(
    tmp_path: Path,
) -> None:
    """Catches inventing networks, starting ordinary stops, and touching shared netns."""
    daemon, reconciler, clock, caddy, api = fleet(tmp_path)
    daemon.stop(caddy)
    del daemon.networks["vonk-forge_ingress"]
    daemon.stop(api)  # Stopped after it ran: no start error.
    configurator = daemon.container(
        "vonk-forge-tailscale-configurator-1", [], mode=f"container:{api}"
    )
    daemon.stop(configurator)
    daemon.containers[configurator]["State"]["Error"] = "netns gone"
    ticks(reconciler, clock, 1)
    # Its failed start is retried; no network of the shared namespace is touched.
    assert daemon.mutations == [("start", configurator)]
    daemon.network("vonk-forge_ingress")  # The next `compose up` creates it.
    with pytest.raises(janitor.DockerError):
        daemon.start(caddy)
    daemon.containers[api]["State"]["Error"] = "failed to start"
    daemon.containers[api]["HostConfig"]["RestartPolicy"]["Name"] = "no"
    ticks(reconciler, clock, 1)
    assert daemon.running(caddy)
    assert not daemon.running(api)
    daemon.containers[api]["HostConfig"]["RestartPolicy"]["Name"] = "always"
    ticks(reconciler, clock, 1)
    assert daemon.running(api)


def test_repair_that_never_converges_backs_off_exponentially(tmp_path: Path) -> None:
    """Catches bouncing a container, and every route through it, on each tick."""
    daemon, reconciler, clock, caddy, _ = fleet(tmp_path)
    daemon.networks["vonk-forge_ingress"]["Containers"].clear()
    daemon.endpoint_rebuilt = False  # Start does not bring the endpoint back.
    ticks(reconciler, clock, 64)
    stops = [index for index, call in enumerate(daemon.mutations) if call[0] == "stop"]
    # Delays 30, 60, 120, ... seconds: 64 ticks of 30 s hold about six repairs.
    assert 4 <= len(stops) <= 7
    assert daemon.running(caddy)
    daemon.endpoint_rebuilt = True
    clock.now += janitor.INTERVAL
    ticks(reconciler, clock, 2)
    assert caddy in daemon.networks["vonk-forge_ingress"]["Containers"]


def test_failed_reattach_never_leaves_the_container_stopped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Catches a repair that turns a degraded container into a stopped one."""
    daemon, reconciler, clock, caddy, _ = fleet(tmp_path)
    daemon.networks["vonk-forge_ingress"]["Containers"].clear()
    daemon.refuse_connect = True
    with caplog.at_level("INFO", logger=janitor.LOGGER.name):
        ticks(reconciler, clock, 1)
    assert daemon.running(caddy)
    assert [json.loads(r.message)["reason"] for r in caplog.records] == [
        janitor.Reason.REPAIR_FAILED
    ]


@pytest.mark.parametrize(
    "reference",
    [
        "postgres:18.6",
        "caddy:2.11.7",
        "tailscale/tailscale:v1.102.5",
        "ghcr.io/carstvaartjes/vonk-forge-api-impostor:v1",
        "evil/ghcr.io/carstvaartjes/vonk-forge-api:v1",
    ],
)
def test_foreign_and_lookalike_repositories_are_not_managed(reference: str) -> None:
    """Catches prefix matching that authorizes unrelated or third-party image deletion."""
    daemon = Daemon([image(1, source=None, reference=reference)])
    assert not daemon.images()[0].managed


def test_signed_bundle_runs_standalone_janitor_without_giving_controller_socket(
    tmp_path: Path,
) -> None:
    """Catches shipping source without installing a working service in the NAS bundle."""
    loader = importlib.machinery.SourceFileLoader(
        "janitor_renderer", str(ROOT / "scripts/render-dev-compose")
    )
    renderer_spec = importlib.util.spec_from_loader(loader.name, loader)
    assert renderer_spec is not None
    renderer = importlib.util.module_from_spec(renderer_spec)
    loader.exec_module(renderer)
    document = renderer._compose_document(ROOT / "deploy/compose/compose.yaml")
    service = document["services"]["image-janitor"]
    source = service["command"][-1]
    # Execute the installed bytes, not just the source module used above.
    installed = tmp_path / "installed.py"
    installed.write_text(source.replace("$$", "$"))
    installed_spec = importlib.util.spec_from_file_location(
        "installed_janitor", installed
    )
    assert installed_spec is not None and installed_spec.loader is not None
    module = importlib.util.module_from_spec(installed_spec)
    sys.modules[installed_spec.name] = module
    installed_spec.loader.exec_module(module)
    daemon = Daemon([image(1)])
    state = tmp_path / "unused.json"
    module.sweep(daemon, state, 100_000)
    module.sweep(daemon, state, 100_000 + module.GRACE)
    assert daemon.deleted == [identity(1)]
    assert service["command"][-2] == "-c"
    for name in ("control-api", "control-worker"):
        assert "docker.sock" not in yaml.safe_dump(document["services"][name])


@pytest.mark.needs_docker
@pytest.mark.needs_buildx
def test_compose_digest_build_context_delivers_its_actual_copy_inputs(
    tmp_path: Path,
) -> None:
    """Catches new digest inputs excluded by Docker's context allowlist."""
    stage = re.search(
        r"(?ms)^FROM [^\n]+ AS compose-bundle-digest\n(?P<body>.*?)(?=^FROM |\Z)",
        (ROOT / "control/Dockerfile").read_text(),
    )
    assert stage is not None
    copies = [line for line in stage["body"].splitlines() if line.startswith("COPY ")]
    probe = tmp_path / "Dockerfile"
    probe.write_text("FROM scratch\n" + "\n".join(copies) + "\n")
    exported = tmp_path / "exported"
    result = subprocess.run(
        [
            "docker",
            "build",
            "--file",
            str(probe),
            "--output",
            f"type=local,dest={exported}",
            str(ROOT),
        ],
        env={**os.environ, "BUILDX_CONFIG": str(tmp_path / "buildx")},
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (
        exported / "compose/image-janitor/janitor.py"
    ).read_bytes() == SCRIPT.read_bytes()


@pytest.mark.needs_docker
@pytest.mark.slow(60)
def test_rendered_janitor_starts_with_a_read_only_root(tmp_path: Path) -> None:
    """Catches Compose config injection that parses but cannot start read-only."""
    loader = importlib.machinery.SourceFileLoader(
        "running_janitor_renderer", str(ROOT / "scripts/render-dev-compose")
    )
    renderer_spec = importlib.util.spec_from_loader(loader.name, loader)
    assert renderer_spec is not None
    renderer = importlib.util.module_from_spec(renderer_spec)
    loader.exec_module(renderer)
    document = renderer._compose_document(ROOT / "deploy/compose/compose.yaml")
    standalone = {
        "services": {"image-janitor": document["services"]["image-janitor"]},
        "volumes": {"image-janitor-state": {}},
    }
    if "configs" in document:
        standalone["configs"] = document["configs"]
    compose = tmp_path / "compose.yaml"
    compose.write_text(yaml.safe_dump(standalone))
    command = [
        "docker",
        "compose",
        "-p",
        f"nasimages-{uuid.uuid4().hex}",
        "-f",
        str(compose),
    ]
    try:
        result = subprocess.run(
            [*command, "up", "-d", "--wait", "--wait-timeout", "120"],
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
        assert result.returncode == 0, result.stderr
        container = subprocess.check_output(
            [*command, "ps", "-q", "image-janitor"], text=True, timeout=30
        ).strip()
        readonly = subprocess.check_output(
            [
                "docker",
                "inspect",
                "--format",
                "{{.HostConfig.ReadonlyRootfs}}",
                container,
            ],
            text=True,
            timeout=30,
        )
        assert readonly.strip() == "true"
        # A fresh, private state volume guarantees a new grace window. This
        # runtime smoke must not remove any images from the shared test daemon.
        logs = subprocess.check_output(
            [*command, "logs", "--no-log-prefix", "image-janitor"],
            text=True,
            timeout=30,
        )
        assert all(
            json.loads(line)["reason"] != janitor.Reason.REMOVED
            for line in logs.splitlines()
        )
    finally:
        subprocess.run(
            [*command, "down", "--volumes", "--remove-orphans"],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )


class Retiring(janitor.Docker):
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, path: str, body: Any = None) -> Any:
        if method == "GET":
            return [dict(row) for row in self.rows]
        self.calls.append((method, path))
        return None


def labelled(identity: str, service: str, **extra: str) -> dict[str, Any]:
    return {
        "Id": identity,
        "Created": 100,
        "Names": [f"/vonk-forge-{service}-1"],
        "Labels": {
            janitor.PROJECT_LABEL: "vonk-forge",
            janitor.SERVICE_LABEL: service,
            **extra,
        },
    }


def test_container_of_a_service_the_bundle_dropped_is_stopped_and_removed(
    tmp_path: Path,
) -> None:
    """Catches an upgrade leaving the dropped service's containers running."""
    daemon = Retiring(
        [
            labelled("a1", "caddy"),
            labelled("b2", "hermes-agent"),
            labelled("c3", "hermes-litellm-key-provisioner"),
            labelled("d4", "hermes-agent", **{janitor.ONEOFF_LABEL: "True"}),
            {"Id": "e5", "Names": ["/unlabelled"], "Labels": {}},
        ]
    )
    reconciler = janitor.Reconciler(daemon, tmp_path / "repairing.json")
    reconciler.retire(
        daemon.request("GET", ""), frozenset({"caddy", "image-janitor"}), 200
    )
    assert daemon.calls == [
        ("POST", "/containers/b2/stop?t=30"),
        ("DELETE", "/containers/b2?v=false"),
        ("POST", "/containers/c3/stop?t=30"),
        ("DELETE", "/containers/c3?v=false"),
    ]


def test_without_the_bundle_service_list_nothing_is_retired(tmp_path: Path) -> None:
    """Catches removing containers when the janitor does not know the bundle."""
    daemon = Retiring([labelled("b2", "hermes-agent")])
    janitor.Reconciler(daemon, tmp_path / "repairing.json").tick("vonk-forge")
    assert daemon.calls == []


def test_container_created_after_the_janitor_started_is_never_retired(
    tmp_path: Path,
) -> None:
    """Catches an old janitor removing a service that a newer bundle just added."""
    daemon = Retiring([labelled("n1", "brand-new-service")])
    reconciler = janitor.Reconciler(daemon, tmp_path / "repairing.json")
    reconciler.retire(daemon.request("GET", ""), frozenset({"caddy"}), 50)
    assert daemon.calls == []
    reconciler.retire(daemon.request("GET", ""), frozenset({"caddy"}), 200)
    assert daemon.calls == [
        ("POST", "/containers/n1/stop?t=30"),
        ("DELETE", "/containers/n1?v=false"),
    ]


def created(daemon: Networks, identity: str, stamp: str, **state: str) -> None:
    record = daemon.containers[identity]
    record["State"].update(Status="created", **state)
    record["Created"] = stamp  # clock.now = 1_000_000 is 1970-01-12T13:46:40Z


def test_container_left_created_by_an_aborted_up_is_started_after_the_grace(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Catches Created containers with a restart policy that nothing ever starts."""
    daemon, reconciler, clock, caddy, _ = fleet(tmp_path)
    created(daemon, caddy, "1970-01-12T13:46:40.987654321Z")
    with caplog.at_level("INFO", logger=janitor.LOGGER.name):
        ticks(reconciler, clock, 1)
    assert daemon.mutations == [] and not daemon.running(caddy)
    assert {
        "reason": janitor.Reason.CREATED_WAIT,
        "container": "vonk-forge-caddy-1",
        "next_attempt": 1_000_000.987654 + janitor.CREATED_GRACE,
    } in [
        {**row, "next_attempt": round(row["next_attempt"], 6)}
        if "next_attempt" in row
        else row
        for row in map(json.loads, (r.message for r in caplog.records))
    ]
    clock.now += janitor.CREATED_GRACE
    caplog.clear()
    with caplog.at_level("INFO", logger=janitor.LOGGER.name):
        ticks(reconciler, clock, 1)
    assert daemon.mutations == [("start", caddy)]
    assert daemon.running(caddy)
    assert janitor.Reason.CREATED_STARTED in [
        json.loads(r.message)["reason"] for r in caplog.records
    ]


def test_created_container_in_a_shared_network_namespace_is_started_after_the_grace(
    tmp_path: Path,
) -> None:
    """Catches skipping containers without networks of their own (tailscale-configurator)."""
    daemon, reconciler, clock, _, api = fleet(tmp_path)
    configurator = daemon.container(
        "vonk-forge-tailscale-configurator-1", [], mode=f"container:{api}"
    )
    created(daemon, configurator, "1970-01-01T00:00:00Z")
    ticks(reconciler, clock, 1)
    assert daemon.mutations == [("start", configurator)]
    assert daemon.running(configurator)


def test_created_container_that_should_not_be_running_is_not_abandoned(
    tmp_path: Path,
) -> None:
    """Catches starting a Created container without a restart policy, or retrying an error as abandoned."""
    daemon, reconciler, clock, caddy, api = fleet(tmp_path)
    created(daemon, caddy, "1970-01-01T00:00:00Z")
    daemon.containers[caddy]["HostConfig"]["RestartPolicy"]["Name"] = "no"
    created(daemon, api, "1970-01-01T00:00:00Z", Error="boom")
    ticks(reconciler, clock, 1)
    assert daemon.mutations == [
        ("start", api)
    ]  # The recorded-error path, not abandonment.
    assert not daemon.running(caddy)
