"""NAS daemon retention at the inventory/delete/persistent-state boundary."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import re
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


class Networks(janitor.Docker):
    """Daemon network semantics the repair relies on, as observed on Docker 29."""

    def __init__(self) -> None:
        self.networks: dict[str, dict[str, Any]] = {}
        self.containers: dict[str, dict[str, Any]] = {}
        self.mutations: list[tuple[str, str]] = []
        self.refuse_connect = False

    def network(self, name: str) -> None:
        self.networks[name] = {"Id": uuid.uuid4().hex, "Containers": {}}

    def container(
        self, name: str, networks: list[str], *, mode: str | None = None
    ) -> str:
        identity = uuid.uuid4().hex
        self.containers[identity] = {
            "Id": identity,
            "Name": f"/{name}",
            "Labels": {janitor.PROJECT_LABEL: "vonk-forge"},
            "State": {"Status": "created", "Running": False, "Error": ""},
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

    def start(self, identity: str) -> None:
        container = self.containers[identity]
        entries = container["NetworkSettings"]["Networks"]
        for name, entry in entries.items():
            current = self.networks.get(name)
            if current is None or entry["NetworkID"] not in ("", current["Id"]):
                container["State"].update(
                    Status="exited", Running=False, Error="network not found"
                )
                raise janitor.DockerError("Docker HTTP 500: network not found")
        for name, entry in entries.items():
            entry["NetworkID"] = self.networks[name]["Id"]
            self.networks[name]["Containers"][identity] = {}
        container["State"].update(Status="running", Running=True, Error="")

    def stop(self, identity: str) -> None:
        self.containers[identity]["State"].update(Status="exited", Running=False)
        for network in self.networks.values():
            network["Containers"].pop(identity, None)

    def request(self, method: str, path: str, body: Any = None) -> Any:
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
            if name not in self.networks:
                raise janitor.DockerNotFound("Docker HTTP 404: network not found")
            return json.loads(json.dumps(self.networks[name]))
        self.mutations.append((action[0], name))
        if kind == "containers":
            (self.start if action == ["start"] else self.stop)(name)
            return None
        record = self.containers[body["Container"]]
        assert not record["State"]["Running"]  # A running one cannot leave.
        if action == ["disconnect"]:
            del record["NetworkSettings"]["Networks"][name]
            return None
        if self.refuse_connect:
            raise janitor.DockerError("Docker HTTP 500: connect refused")
        record["NetworkSettings"]["Networks"][name] = {
            "NetworkID": "",
            **body["EndpointConfig"],
        }
        return None


def fleet() -> tuple[Networks, str, str]:
    daemon = Networks()
    for name in ("vonk-forge_ingress", "vonk-forge_agent-proxy", "vonk-forge_data"):
        daemon.network(name)
    caddy = daemon.container(
        "vonk-forge-caddy-1", ["vonk-forge_ingress", "vonk-forge_agent-proxy"]
    )
    api = daemon.container(
        "vonk-forge-api-1", ["vonk-forge_agent-proxy", "vonk-forge_data"]
    )
    return daemon, caddy, api


def test_recreated_network_reattaches_the_same_stopped_container_and_runs_it() -> None:
    """Catches the NAS update fault that only a manual container delete used to clear."""
    daemon, caddy, api = fleet()
    daemon.stop(caddy)
    daemon.network("vonk-forge_ingress")  # Recreated with a new ID.
    with pytest.raises(janitor.DockerError):
        daemon.start(caddy)  # What `compose up` and a restart do.
    janitor.reconcile_networks(daemon, "vonk-forge")
    record = daemon.containers[caddy]
    assert record["State"]["Running"]
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
    janitor.reconcile_networks(daemon, "vonk-forge")
    assert daemon.mutations == []
    assert daemon.containers[api]["State"]["Running"]


def test_running_container_without_its_endpoint_is_reattached() -> None:
    """Catches trusting the container record while the network has no endpoint."""
    daemon, caddy, _ = fleet()
    daemon.networks["vonk-forge_ingress"]["Containers"].clear()
    janitor.reconcile_networks(daemon, "vonk-forge")
    assert daemon.mutations == [
        ("stop", caddy),
        ("disconnect", "vonk-forge_ingress"),
        ("connect", "vonk-forge_ingress"),
        ("start", caddy),
    ]
    assert caddy in daemon.networks["vonk-forge_ingress"]["Containers"]


def test_repair_waits_for_compose_and_respects_intentional_stops() -> None:
    """Catches inventing networks, starting manual stops, and touching shared netns."""
    daemon, caddy, api = fleet()
    daemon.stop(caddy)
    del daemon.networks["vonk-forge_ingress"]
    daemon.stop(api)  # Manual stop: no start error.
    configurator = daemon.container(
        "vonk-forge-tailscale-configurator-1", [], mode=f"container:{api}"
    )
    daemon.stop(configurator)
    daemon.containers[configurator]["State"]["Error"] = "netns gone"
    janitor.reconcile_networks(daemon, "vonk-forge")
    assert daemon.mutations == []
    daemon.network("vonk-forge_ingress")  # The next `compose up` creates it.
    with pytest.raises(janitor.DockerError):
        daemon.start(caddy)
    daemon.containers[api]["State"]["Error"] = "failed to start"
    daemon.containers[api]["HostConfig"]["RestartPolicy"]["Name"] = "no"
    janitor.reconcile_networks(daemon, "vonk-forge")
    assert daemon.containers[caddy]["State"]["Running"]
    assert not daemon.containers[api]["State"]["Running"]
    daemon.containers[api]["HostConfig"]["RestartPolicy"]["Name"] = "always"
    janitor.reconcile_networks(daemon, "vonk-forge")
    assert daemon.containers[api]["State"]["Running"]


def test_failed_reattach_never_leaves_the_container_stopped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Catches a repair that turns a degraded container into a stopped one."""
    daemon, caddy, _ = fleet()
    daemon.networks["vonk-forge_ingress"]["Containers"].clear()
    daemon.refuse_connect = True
    with caplog.at_level("INFO", logger=janitor.LOGGER.name):
        janitor.reconcile_networks(daemon, "vonk-forge")
    assert daemon.containers[caddy]["State"]["Running"]
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
