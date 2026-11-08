"""NAS daemon housekeeping for the platform's own Compose project.

Hourly, non-forcing collection of unused platform images, and a 30-second
repair loop that re-attaches project containers to networks they lost (a
network recreated under them, or an endpoint the network no longer lists).
`compose up` and restarts reuse such a container: a stopped one is started
as-is, and only a running one is checked for missing networks.
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import os
import random
import re
import sched
import signal
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import FrameType
from typing import Any, NotRequired, TypedDict
from urllib.parse import quote

INTERVAL = 3600
NETWORK_INTERVAL = 30
PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"
ONEOFF_LABEL = "com.docker.compose.oneoff"
# The services the rendered bundle defines; empty (unrendered) retires nothing.
COMPOSE_SERVICES = frozenset(
    name for name in os.environ.get("VONK_COMPOSE_SERVICES", "").split(",") if name
)
# A redeploy that adds a service creates it while the previous janitor, with its
# older list, still runs; only containers older than this process are retired.
STARTED = time.time()
GRACE = 24 * INTERVAL
SOURCE = "https://github.com/CarstVaartjes/vonk-forge"
# "hermes" is a retired service; it stays listed so its old image is reclaimed.
REPOSITORIES = frozenset(
    f"ghcr.io/carstvaartjes/vonk-forge-{role}"
    for role in ("api", "worker", "litellm", "hermes")
)
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
STATE = Path("/state/unused.json")
REPAIRING = Path("/state/repairing.json")
LOGGER = logging.getLogger("image-janitor")


class Row(TypedDict):
    """The fields of a Docker container listing row this module reads."""

    Id: str
    Names: NotRequired[list[str]]
    Created: NotRequired[int]
    Labels: NotRequired[dict[str, str] | None]


class Reason(StrEnum):
    OUTSIDE_SCOPE = "outside-platform-scope"
    IN_USE = "container-reference"
    GRACE = "unused-grace"
    DELETE_FAILED = "delete-failed"
    SCAN_FAILED = "scan-failed"
    STATE_INVALID = "state-invalid-grace-restarted"
    REMOVED = "removed"
    NETWORK_WAIT = "network-absent-wait-for-compose"
    STATE_WAIT = "container-state-owned-by-docker"
    BACKOFF_WAIT = "repair-backoff"
    NETWORK_REPAIRED = "network-reattached"
    START_RETRIED = "start-retried"
    REPAIR_FAILED = "network-repair-failed"
    NO_PROJECT = "not-in-a-compose-project"
    RETIRED = "retired-service-container-removed"
    MARKS_INVALID = "repair-journal-invalid-reset"


@dataclass(frozen=True)
class Image:
    identity: str
    references: tuple[str, ...]
    labels: dict[str, str]
    size: int

    @property
    def managed(self) -> bool:
        # An exact repository identity also covers historical images without
        # labels. The exact source label covers dangling platform builds.
        return self.labels.get("org.opencontainers.image.source") == SOURCE or any(
            reference.split("@", 1)[0].split(":", 1)[0] in REPOSITORIES
            for reference in self.references
        )


class DockerError(RuntimeError):
    pass


class DockerConnection(http.client.HTTPConnection):
    def __init__(self) -> None:
        super().__init__("localhost", timeout=30)

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/var/run/docker.sock")


class DockerNotFound(DockerError):
    pass


class Docker:
    def request(self, method: str, path: str, body: Any = None) -> Any:
        connection = DockerConnection()
        try:
            if body is None:
                connection.request(method, path)
            else:
                connection.request(
                    method,
                    path,
                    json.dumps(body),
                    {"Content-Type": "application/json"},
                )
            response = connection.getresponse()
            raw = response.read()
            if response.status == 404:
                raise DockerNotFound(f"Docker HTTP 404: {raw.decode()}")
            if not 200 <= response.status < 300:
                raise DockerError(f"Docker HTTP {response.status}: {raw.decode()}")
            return json.loads(raw) if raw.strip() else None
        finally:
            connection.close()

    def images(self) -> list[Image]:
        rows = self.request("GET", "/images/json?all=1")
        result = []
        for row in rows:
            identity, size = row["Id"], row["Size"]
            labels = row.get("Labels") or {}
            references = tuple(
                ref
                for ref in (row.get("RepoTags") or []) + (row.get("RepoDigests") or [])
                if ref not in {"<none>:<none>", "<none>@<none>"}
            )
            if (
                not isinstance(identity, str)
                or IMAGE_ID.fullmatch(identity) is None
                or not isinstance(size, int)
                or not isinstance(labels, dict)
                or any(
                    not isinstance(k, str) or not isinstance(v, str)
                    for k, v in labels.items()
                )
                or any(not isinstance(ref, str) for ref in references)
            ):
                raise ValueError("Invalid Docker image inventory")
            result.append(Image(identity, references, labels, size))
        return result

    def in_use(self) -> set[str]:
        # all=1 includes stopped containers, including another Compose project.
        rows = self.request("GET", "/containers/json?all=1")
        identities = {row["ImageID"] for row in rows}
        if any(
            not isinstance(value, str) or IMAGE_ID.fullmatch(value) is None
            for value in identities
        ):
            raise ValueError("Invalid Docker container inventory")
        return identities

    def remove(self, image: Image) -> list[dict[str, str]]:
        # Address content identity only. Docker refuses running AND stopped
        # container conflicts and ambiguous multi-repository tags without
        # force. Re-plan from inventory on the next hourly scan on any refusal;
        # never untag a mutable name that may now identify a new release.
        if image.identity in self.in_use():
            raise DockerError(f"{Reason.IN_USE}: {image.identity}")
        receipts = self.request(
            "DELETE",
            f"/images/{quote(image.identity, safe='')}?force=false&noprune=true",
        )
        if image.identity in {item.identity for item in self.images()}:
            raise DockerError(f"Image still present: {image.identity}")
        return receipts


def log(reason: Reason, **fields: Any) -> None:
    LOGGER.info(json.dumps({"reason": reason, **fields}, sort_keys=True))


def load_state(path: Path) -> dict[str, float]:
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict) or any(
            IMAGE_ID.fullmatch(key) is None
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            for key, value in raw.items()
        ):
            raise ValueError("Invalid first-seen-unused state")
        return raw
    except FileNotFoundError:
        return {}
    except (ValueError, TypeError) as error:
        log(Reason.STATE_INVALID, detail=str(error))
        return {}


def save_state(path: Path, unused: dict[str, float]) -> None:
    publish(path, unused)


def publish(path: Path, value: dict[str, float] | dict[str, Pending]) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, sort_keys=True, allow_nan=False, default=vars)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sweep(docker: Docker, path: Path, now: float) -> None:
    images, in_use = docker.images(), docker.in_use()
    previous = load_state(path)
    unused: dict[str, float] = {}
    for image in images:
        if not image.managed:
            log(Reason.OUTSIDE_SCOPE, image=image.identity)
        elif image.identity in in_use:
            log(Reason.IN_USE, image=image.identity)
        else:
            unused[image.identity] = min(previous.get(image.identity, now), now)
    # Persist observed reuse/removal and new clocks BEFORE any deletion. A
    # process death must never retain a pre-reuse grace window.
    save_state(path, unused)
    for image in images:
        first_seen = unused.get(image.identity)
        if first_seen is None:
            continue
        if now - first_seen < GRACE:
            log(Reason.GRACE, image=image.identity, next_attempt=first_seen + GRACE)
            continue
        try:
            receipts = docker.remove(image)
        except (DockerError, OSError, ValueError, http.client.HTTPException) as error:
            # Re-scan hourly rather than retrying a stale selection in a loop.
            if image.identity in docker.in_use():
                unused.pop(image.identity, None)
                save_state(path, unused)
            log(
                Reason.DELETE_FAILED,
                image=image.identity,
                detail=str(error),
                next_attempt=now + INTERVAL,
            )
        else:
            unused.pop(image.identity, None)
            # Image size includes shared layers; it is NOT measured freed space.
            log(
                Reason.REMOVED,
                image=image.identity,
                image_size_bytes=image.size,
                receipts=receipts,
            )
    save_state(path, unused)


class State(StrEnum):
    """Every Docker container state."""

    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    RESTARTING = "restarting"
    REMOVING = "removing"
    EXITED = "exited"
    DEAD = "dead"


# Docker itself owns these: its restart policy, a deliberate pause, a removal.
DOCKER_OWNED = frozenset({State.PAUSED, State.RESTARTING, State.REMOVING, State.DEAD})


class Policy(StrEnum):
    """Restart policies under which a container should be running."""

    ALWAYS = "always"
    UNLESS_STOPPED = "unless-stopped"


class SharedMode(StrEnum):
    """Network modes without own attachments; `container:<id>` is the third."""

    HOST = "host"
    NONE = "none"


@dataclass(frozen=True)
class Network:
    identity: str
    members: frozenset[str]

    @classmethod
    def inspect(cls, docker: Docker, name: str) -> Network | None:
        try:
            row = docker.request("GET", f"/networks/{quote(name, safe='')}")
        except DockerNotFound:
            return None
        return cls(str(row["Id"]), frozenset(row.get("Containers") or {}))


class Action(StrEnum):
    NONE = "none"
    REATTACH = "reattach"
    START = "start"
    WAIT_STATE = "wait-state"
    WAIT_NETWORK = "wait-network"
    WAIT_BACKOFF = "wait-backoff"


@dataclass(frozen=True)
class Container:
    identity: str
    name: str
    state: State
    error: str
    policy: str
    mode: str
    # State.StartedAt: tells a restarted container from the one inspected.
    started: str
    # Network name -> (recorded network ID, recorded aliases).
    networks: dict[str, tuple[str, tuple[str, ...]]]

    @classmethod
    def inspect(cls, docker: Docker, identity: str) -> Container:
        row = docker.request("GET", f"/containers/{quote(identity, safe='')}/json")
        networks = {}
        for name, endpoint in (row["NetworkSettings"]["Networks"] or {}).items():
            endpoint = endpoint or {}
            aliases = endpoint.get("Aliases") or []
            if not isinstance(name, str) or any(
                not isinstance(alias, str) for alias in aliases
            ):
                raise ValueError("Invalid Docker container networks")
            networks[name] = (str(endpoint.get("NetworkID") or ""), tuple(aliases))
        return cls(
            identity=str(row["Id"]),
            name=str(row["Name"]).lstrip("/"),
            state=State(row["State"]["Status"]),
            error=str(row["State"].get("Error") or ""),
            policy=str(row["HostConfig"]["RestartPolicy"]["Name"]),
            mode=str(row["HostConfig"]["NetworkMode"]),
            started=str(row["State"].get("StartedAt") or ""),
            networks=networks,
        )

    @property
    def own_networks(self) -> bool:
        return self.mode not in set(SharedMode) and not self.mode.startswith(
            "container:"
        )


@dataclass(frozen=True)
class Pending:
    """A repair begun and not yet confirmed, journalled before its first effect.

    Docker records an API stop as a manual stop and a disconnect drops the
    network from the container record, so a janitor that dies mid-repair
    would otherwise leave the container stopped or short of a network.
    """

    start: bool
    # Network name -> aliases to re-attach with.
    networks: dict[str, tuple[str, ...]]

    @classmethod
    def parse(cls, raw: object) -> Pending:
        if not isinstance(raw, dict) or not isinstance(raw.get("start"), bool):
            raise TypeError("Invalid pending repair")
        networks = raw.get("networks")
        if not isinstance(networks, dict) or any(
            not isinstance(aliases, list)
            or any(not isinstance(alias, str) for alias in aliases)
            for aliases in networks.values()
        ):
            raise TypeError("Invalid pending repair networks")
        return cls(raw["start"], {str(k): tuple(v) for k, v in networks.items()})


def decide(
    container: Container,
    *,
    broken: list[str] | None,
    pending: Pending | None,
    held_until: float,
    now: float,
) -> Action:
    """The one transition: observed container and network facts -> action.

    `broken` is None while a network to attach does not exist (Compose owns
    networks), else the networks whose attachment is lost or still owed by a
    `pending` repair. `held_until` is the backoff of the planned repair.
    """
    if container.state in DOCKER_OWNED:
        return Action.WAIT_STATE
    if broken is None:
        return Action.WAIT_NETWORK
    if broken:
        planned = Action.REATTACH
    elif should_run(container, pending) and container.state != State.RUNNING:
        planned = Action.START
    else:
        return Action.NONE
    return Action.WAIT_BACKOFF if now < held_until else planned


def should_run(container: Container, pending: Pending | None) -> bool:
    # A failed start is not retried by the restart policy, and a stop by this
    # janitor's repair counts as manual to Docker.
    return (
        container.state == State.RUNNING
        or (pending is not None and pending.start)
        or (bool(container.error) and container.policy in set(Policy))
    )


class Backoff:
    """Per-repair exponential backoff with equal jitter, in memory.

    A repair that does not converge (the start does not rebuild the endpoint)
    must not bounce a container, and the routes through it, every tick.
    """

    def __init__(self, jitter: Callable[[], float]) -> None:
        self.jitter = jitter
        self.failures: dict[tuple[str, str], int] = {}
        self.until: dict[tuple[str, str], float] = {}

    def held_until(self, keys: list[tuple[str, str]]) -> float:
        return max((self.until.get(key, 0.0) for key in keys), default=0.0)

    def attempted(self, keys: list[tuple[str, str]], now: float) -> float:
        for key in keys:
            failures = self.failures.get(key, 0) + 1
            self.failures[key] = failures
            delay = min(INTERVAL, NETWORK_INTERVAL * 2 ** (failures - 1))
            self.until[key] = now + delay / 2 + self.jitter() * delay / 2
        return self.held_until(keys)

    def healthy(self, identity: str, keep: set[tuple[str, str]]) -> None:
        for key in [key for key in self.failures if key[0] == identity]:
            if key not in keep:
                del self.failures[key]
                self.until.pop(key, None)


class Reconciler:
    def __init__(
        self,
        docker: Docker,
        journal: Path,
        *,
        clock: Callable[[], float] = time.time,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.docker = docker
        self.journal = journal
        self.clock = clock
        self.backoff = Backoff(jitter)

    def pending(self) -> dict[str, Pending]:
        try:
            raw = json.loads(self.journal.read_text())
            if not isinstance(raw, dict):
                raise TypeError("pending repairs are not a mapping")
            return {str(k): Pending.parse(v) for k, v in raw.items()}
        except FileNotFoundError:
            return {}
        except (ValueError, TypeError) as error:
            log(Reason.MARKS_INVALID, detail=str(error))
            return {}

    def record(self, identity: str, entry: Pending | None) -> None:
        journal = self.pending()
        if journal.get(identity) == entry:
            return
        if entry is None:
            del journal[identity]
        else:
            journal[identity] = entry
        publish(self.journal, journal)

    def retire(
        self, rows: list[Row], services: frozenset[str], started: float = STARTED
    ) -> list[Row]:
        """Remove project containers of services the bundle no longer defines.

        An upgrade redeploys the new Compose file but never removes the
        containers of a service it dropped. Only this project's containers, only
        a service named by its Compose label and absent from `services`, created before
        this process started, never a one-off; the volumes stay.
        """
        kept = []
        for row in rows:
            labels = row.get("Labels") or {}
            service = labels.get(SERVICE_LABEL)
            if (
                not services
                or service is None
                or service in services
                or labels.get(ONEOFF_LABEL) == "True"
                or row.get("Created", started) >= started
            ):
                kept.append(row)
                continue
            identity = quote(row["Id"], safe="")
            try:
                self.docker.request("POST", f"/containers/{identity}/stop?t=30")
            except DockerNotFound:
                pass
            except DockerError as error:
                # Docker answers 304 for an already stopped container.
                if "304" not in str(error):
                    raise
            try:
                self.docker.request("DELETE", f"/containers/{identity}?v=false")
            except DockerNotFound:
                pass  # Gone already: Compose or an operator removed it first.
            log(Reason.RETIRED, service=service, container=row.get("Names"))
        return kept

    def tick(self, project: str, services: frozenset[str] = frozenset()) -> None:
        selector = quote(json.dumps({"label": [f"{PROJECT_LABEL}={project}"]}), safe="")
        rows = self.docker.request("GET", f"/containers/json?all=1&filters={selector}")
        rows = self.retire(rows, services)
        # One inspect per network per tick, shared by every container.
        networks: dict[str, Network | None] = {}
        for row in rows:
            try:
                self.repair(Container.inspect(self.docker, row["Id"]), networks)
            except (
                DockerError,
                OSError,
                ValueError,
                KeyError,
                TypeError,
                http.client.HTTPException,
            ) as error:
                # Re-observe on the next tick; Docker or Compose may be mid-change.
                log(
                    Reason.REPAIR_FAILED,
                    container=row.get("Names"),
                    detail=str(error),
                    next_attempt=self.clock() + NETWORK_INTERVAL,
                )
        present = {row["Id"] for row in rows}
        for identity in set(self.pending()) - present:
            self.record(identity, None)

    def broken(
        self,
        container: Container,
        pending: Pending | None,
        networks: dict[str, Network | None],
    ) -> list[str] | None:
        """Networks to (re-)attach, or None while one of them does not exist.

        The container's own network entries are the declared set: the daemon
        keeps them until an explicit disconnect, even when the network or the
        endpoint is gone. An entry is broken when it names an older network ID
        (the network was recreated) or, while running, when the network lists
        no endpoint for it. A pending repair also owes the networks it
        disconnected and has not yet seen attached again.
        """
        owed = [
            n
            for n in (pending.networks if pending else {})
            if n not in container.networks
        ]
        broken = []
        for name in [*container.networks, *owed]:
            if name not in networks:
                networks[name] = Network.inspect(self.docker, name)
            network = networks[name]
            if network is None:
                return None
            if name not in owed and self.detached(container, name, network):
                # The snapshot is shared by the tick and may predate this
                # container joining; condemn only on a fresh read, and only if
                # the container is still in the state it was observed in.
                network = networks[name] = Network.inspect(self.docker, name)
                if network is None:
                    return None
                if not (
                    self.detached(container, name, network) and self.observed(container)
                ):
                    continue
            if name in owed or self.detached(container, name, network):
                broken.append(name)
        return broken

    @staticmethod
    def detached(container: Container, name: str, network: Network) -> bool:
        recorded = container.networks.get(name, ("", ()))[0]
        return bool(recorded and recorded != network.identity) or (
            container.state == State.RUNNING
            and container.identity not in network.members
        )

    def observed(self, container: Container) -> bool:
        """The container is still in the state `container` was inspected in."""
        now = Container.inspect(self.docker, container.identity)
        return (now.state, now.started) == (container.state, container.started)

    def repair(self, container: Container, networks: dict[str, Network | None]) -> None:
        if not container.own_networks:
            return
        identity, now = container.identity, self.clock()
        pending = self.pending().get(identity)
        broken = self.broken(container, pending, networks)
        keys = [(identity, network) for network in broken or [""]]
        action = decide(
            container,
            broken=broken,
            pending=pending,
            held_until=self.backoff.held_until(keys),
            now=now,
        )
        if action == Action.NONE:
            self.backoff.healthy(identity, set())
            self.record(identity, None)
            return
        if action == Action.WAIT_STATE:
            log(Reason.STATE_WAIT, container=container.name, state=container.state)
            return
        if action == Action.WAIT_NETWORK:
            log(Reason.NETWORK_WAIT, container=container.name)
            return
        if action == Action.WAIT_BACKOFF:
            log(
                Reason.BACKOFF_WAIT,
                container=container.name,
                networks=broken,
                next_attempt=self.backoff.held_until(keys),
            )
            return
        self.backoff.healthy(identity, set(keys))
        next_attempt = self.backoff.attempted(keys, now)
        target = f"/containers/{quote(identity, safe='')}"
        start = should_run(container, pending)
        if action == Action.START:
            self.docker.request("POST", f"{target}/start")
            self.record(identity, None)
            log(
                Reason.START_RETRIED,
                container=container.name,
                previous_error=container.error,
                next_attempt_if_unhealed=next_attempt,
            )
            return
        # Journalled before the first effect, so a janitor that dies mid-way
        # leaves the next one the rest. A running container cannot leave a
        # network whose endpoint is gone; stopped, the daemon edits only its
        # record, and start rebuilds endpoints and published ports from it.
        # The project declares no static addresses; aliases keep the service
        # name resolvable on the re-attached network.
        owed = dict(pending.networks) if pending else {}
        for network in broken or []:
            owed.setdefault(network, container.networks.get(network, ("", ()))[1])
        self.record(identity, Pending(start, owed))
        try:
            if container.state == State.RUNNING:
                self.docker.request("POST", f"{target}/stop?t=10")
            for network in broken or []:
                path = f"/networks/{quote(network, safe='')}"
                if network in container.networks:
                    self.docker.request(
                        "POST",
                        f"{path}/disconnect",
                        {"Container": identity, "Force": True},
                    )
                self.docker.request(
                    "POST",
                    f"{path}/connect",
                    {
                        "Container": identity,
                        "EndpointConfig": {"Aliases": list(owed[network])},
                    },
                )
        finally:
            # Never leave a container stopped by this repair, even short of a
            # network; the journal still owes that network to the next tick.
            if start:
                self.docker.request("POST", f"{target}/start")
        self.record(identity, None)
        log(
            Reason.NETWORK_REPAIRED,
            container=container.name,
            networks=broken,
            started=start,
            next_attempt_if_unhealed=next_attempt,
        )


def own_project(docker: Docker) -> str | None:
    # Docker names the container's hostname after its ID by default.
    labels = docker.request("GET", f"/containers/{socket.gethostname()}/json")[
        "Config"
    ]["Labels"]
    return (labels or {}).get(PROJECT_LABEL)


def terminate(signum: int, frame: FrameType | None) -> None:
    # Unwind through `finally`, so a repair in progress starts its container.
    raise SystemExit(0)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    signal.signal(signal.SIGTERM, terminate)
    docker = Docker()
    reconciler = Reconciler(docker, REPAIRING)
    scheduler = sched.scheduler()

    def network_tick() -> None:
        try:
            project = own_project(docker)
            if project is None:
                log(Reason.NO_PROJECT, next_attempt=time.time() + NETWORK_INTERVAL)
            else:
                reconciler.tick(project, COMPOSE_SERVICES)
        except (
            DockerError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            http.client.HTTPException,
        ) as error:
            log(
                Reason.REPAIR_FAILED,
                detail=str(error),
                next_attempt=time.time() + NETWORK_INTERVAL,
            )
        scheduler.enter(NETWORK_INTERVAL, 2, network_tick)

    def tick() -> None:
        now = time.time()
        try:
            sweep(docker, STATE, now)
        except (
            DockerError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            http.client.HTTPException,
        ) as error:
            log(Reason.SCAN_FAILED, detail=str(error), next_attempt=now + INTERVAL)
        scheduler.enter(INTERVAL, 1, tick)

    network_tick()
    tick()
    scheduler.run()


if __name__ == "__main__":
    main()
