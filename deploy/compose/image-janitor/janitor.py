"""NAS daemon housekeeping for the platform's own Compose project.

Hourly, non-forcing collection of unused platform images, and a short repair
loop that re-attaches project containers to networks they lost. The repair
exists because the daemon can lose an endpoint or recreate a network (seen
after NAS OS updates) while `compose up` and restarts reuse the broken
container: a stopped container is started as-is, and only a running one is
checked for missing networks.
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import os
import re
import sched
import socket
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import quote

INTERVAL = 3600
NETWORK_INTERVAL = 30
PROJECT_LABEL = "com.docker.compose.project"
RUN_POLICIES = frozenset({"always", "unless-stopped"})
GRACE = 24 * INTERVAL
SOURCE = "https://github.com/CarstVaartjes/vonk-forge"
REPOSITORIES = frozenset(
    f"ghcr.io/carstvaartjes/vonk-forge-{role}"
    for role in ("api", "worker", "litellm", "hermes")
)
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
STATE = Path("/state/unused.json")
LOGGER = logging.getLogger("image-janitor")


class Reason(StrEnum):
    OUTSIDE_SCOPE = "outside-platform-scope"
    IN_USE = "container-reference"
    GRACE = "unused-grace"
    DELETE_FAILED = "delete-failed"
    SCAN_FAILED = "scan-failed"
    STATE_INVALID = "state-invalid-grace-restarted"
    REMOVED = "removed"
    NETWORK_WAIT = "network-absent-wait-for-compose"
    NETWORK_REPAIRED = "network-reattached"
    START_RETRIED = "failed-start-retried"
    REPAIR_FAILED = "network-repair-failed"


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
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(unused, stream, sort_keys=True, allow_nan=False)
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


def _broken(docker: Docker, container: dict[str, Any]) -> list[str] | None:
    """Return the container's networks whose attachment is lost, or None to wait.

    The container's own network entries are the declared set: the daemon keeps
    them until an explicit disconnect, even when the network or the endpoint is
    gone. An entry is broken when it names an older network ID (the network was
    recreated) or, while running, when the network lists no endpoint for it.
    """
    running = container["State"]["Running"]
    broken = []
    for name, endpoint in container["NetworkSettings"]["Networks"].items():
        try:
            network = docker.request("GET", f"/networks/{quote(name, safe='')}")
        except DockerNotFound:
            log(Reason.NETWORK_WAIT, container=container["Name"], network=name)
            return None
        recorded = (endpoint or {}).get("NetworkID") or ""
        if (recorded and recorded != network["Id"]) or (
            running and container["Id"] not in (network.get("Containers") or {})
        ):
            broken.append(name)
    return broken


def repair(docker: Docker, identity: str) -> None:
    """Re-attach one container to its lost networks, then run it if it should."""
    container = docker.request("GET", f"/containers/{identity}/json")
    mode = container["HostConfig"]["NetworkMode"]
    if mode in {"host", "none"} or mode.startswith("container:"):
        return
    state = container["State"]
    if state["Status"] not in {"running", "exited"}:
        return
    broken = _broken(docker, container)
    if broken is None:
        return
    name = container["Name"]
    # A failed start is not retried by the restart policy; a manual stop
    # leaves no error. Run what the policy says should run.
    should_run = state["Running"] or (
        bool(state.get("Error"))
        and container["HostConfig"]["RestartPolicy"]["Name"] in RUN_POLICIES
    )
    if broken:
        # A running container cannot leave a network whose endpoint is gone;
        # stopped, the daemon edits only its record. Start rebuilds endpoints
        # and the published ports from that record.
        if state["Running"]:
            docker.request("POST", f"/containers/{identity}/stop?t=10")
        try:
            for network in broken:
                endpoint = container["NetworkSettings"]["Networks"][network] or {}
                target = f"/networks/{quote(network, safe='')}"
                docker.request(
                    "POST",
                    f"{target}/disconnect",
                    {"Container": identity, "Force": True},
                )
                docker.request(
                    "POST",
                    f"{target}/connect",
                    {
                        "Container": identity,
                        "EndpointConfig": {
                            "Aliases": endpoint.get("Aliases"),
                            "IPAMConfig": endpoint.get("IPAMConfig"),
                        },
                    },
                )
        finally:
            # Never leave a container stopped by this repair. If a re-attach
            # failed, it runs short of that network, and the next `compose up`
            # recreates a running container that lacks a declared network.
            if should_run:
                docker.request("POST", f"/containers/{identity}/start")
        log(Reason.NETWORK_REPAIRED, container=name, networks=broken)
    elif should_run and not state["Running"]:
        docker.request("POST", f"/containers/{identity}/start")
        log(Reason.START_RETRIED, container=name, previous_error=state["Error"])


def reconcile_networks(docker: Docker, project: str) -> None:
    selector = quote(json.dumps({"label": [f"{PROJECT_LABEL}={project}"]}), safe="")
    for row in docker.request("GET", f"/containers/json?all=1&filters={selector}"):
        try:
            repair(docker, row["Id"])
        except (
            DockerError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            http.client.HTTPException,
        ) as error:
            # Re-observe on the next tick; the daemon or Compose may be mid-change.
            log(
                Reason.REPAIR_FAILED,
                container=row.get("Names"),
                detail=str(error),
                next_attempt=time.time() + NETWORK_INTERVAL,
            )


def own_project(docker: Docker) -> str | None:
    # Docker names the container's hostname after its ID by default.
    labels = docker.request("GET", f"/containers/{socket.gethostname()}/json")[
        "Config"
    ]["Labels"]
    return (labels or {}).get(PROJECT_LABEL)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    docker = Docker()
    scheduler = sched.scheduler()

    def network_tick() -> None:
        try:
            project = own_project(docker)
            if project is not None:
                reconcile_networks(docker, project)
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
