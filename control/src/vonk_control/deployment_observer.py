"""Background publication observations; never authority for a runtime decision."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError

from .deployment_provenance_contract import Commit, PlatformObservation, Sha256
from .models import Observation

KINDS = ("repository", "publication")


class RepositoryHead(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    sha: Commit


class PublishedRelease(BaseModel):
    # The publisher owns additional release fields; this reader consumes only
    # these current structured observation fields, never executable instructions.
    model_config = ConfigDict(strict=True, extra="ignore")
    schema_version: Literal[2]
    channel: Literal["dev", "stable"]
    generation: Sha256
    source_sha: Commit
    images: dict[str, str]


def observation_id(boundary: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"vonk-forge:deployment-observation:{boundary}"))


def stored_observation(session, boundary: str) -> PlatformObservation | None:
    row = session.get(Observation, observation_id(boundary))
    return PlatformObservation.model_validate_json(json.dumps(row.payload)) if row is not None else None


class DeploymentObserver:
    def __init__(self, sessions, *, channel="dev", transport=None, clock=None):
        if channel not in {"dev", "stable"}:
            raise ValueError("deployment observation channel is invalid")
        self.sessions = sessions
        self.channel = channel
        self.clock = clock or (lambda: datetime.now(UTC))
        self.client = httpx.Client(transport=transport, timeout=5, follow_redirects=False, trust_env=False)
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="deployment-observer")
        self.pending: Future | None = None
        self.next_poll = 0.0

    def tick(self) -> bool:
        if self.pending is not None:
            if not self.pending.done():
                return False
            self.pending.result()
            self.pending = None
            return True
        if time.monotonic() >= self.next_poll:
            self.next_poll = time.monotonic() + 300
            self.pending = self.pool.submit(self.refresh)
        return False

    def close(self) -> None:
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.client.close()

    def refresh(self) -> None:
        # Each boundary succeeds independently. An outage retains its last
        # observation time, so the normal UI displays stale evidence honestly.
        for boundary, read in (("repository", self.repository), ("publication", self.publication)):
            try:
                value = read()
                with self.sessions.begin() as session:
                    row = session.get(Observation, observation_id(boundary))
                    if row is None:
                        row = Observation(id=observation_id(boundary), node_id="controller", kind=f"deployment.{boundary}")
                        session.add(row)
                    row.payload = value.model_dump(mode="json")
                    row.observed_at = value.observed_at
            except (httpx.HTTPError, ValueError, KeyError, TypeError, SQLAlchemyError):
                continue

    def repository(self) -> PlatformObservation:
        response = self.client.get("https://api.github.com/repos/CarstVaartjes/vonk-forge/commits/main", headers={"Accept": "application/vnd.github+json"})
        response.raise_for_status()
        head = RepositoryHead.model_validate_json(response.content)
        return PlatformObservation(source="GitHub repository main (HTTPS observation)", observed_at=self.clock(), source_commit=head.sha)

    def publication(self) -> PlatformObservation:
        origin = "https://install.vonkforge.ai"
        response = self.client.get(f"{origin}/artifacts/{self.channel}/current.manifest")
        response.raise_for_status()
        lines = [line.split("=", 1) for line in response.text.splitlines() if line]
        pointer = dict(lines)
        if len(pointer) != len(lines) or pointer.get("schema_version") != "2" or pointer.get("channel") != self.channel:
            raise ValueError("publication pointer structure is invalid")
        if int(pointer["expires_at"]) <= int(self.clock().timestamp()):
            raise ValueError("publication pointer has expired")
        generation = pointer["generation"]
        path = f"artifacts/{self.channel}/releases/{generation}/release.json"
        if len(generation) != 64 or any(char not in "0123456789abcdef" for char in generation) or pointer["release_path"] != path:
            raise ValueError("publication pointer identity is invalid")
        release = self.client.get(f"{origin}/{path}")
        release.raise_for_status()
        if hashlib.sha256(release.content).hexdigest() != pointer["release_sha256"]:
            raise ValueError("publication changed during observation")
        document = PublishedRelease.model_validate_json(release.content)
        if document.generation != generation or document.channel != self.channel or document.source_sha != pointer["source_sha"]:
            raise ValueError("publication identities disagree")
        image = document.images["api"].rsplit("@", 1)[-1]
        return PlatformObservation(source="Public installer channel (HTTPS observation)", observed_at=self.clock(), source_commit=document.source_sha, image_digest=image, manifest_sha256=hashlib.sha256(response.content).hexdigest())
