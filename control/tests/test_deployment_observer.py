import hashlib
import json
import threading
from datetime import timedelta

import httpx
from sqlalchemy import func, select
from vonk_control.deployment_observer import DeploymentObserver
from vonk_control.deployment_provenance import DeploymentProvenanceService
from vonk_control.models import Observation

from .test_deployment_provenance import deployment


def test_background_observation_reaches_api_projection_and_retains_age_on_outage(tmp_path):
    sessions, now, _, _ = deployment(tmp_path)
    generation = "a" * 64
    release = json.dumps({
        "schema_version": 2, "channel": "dev", "generation": generation,
        "source_sha": "b" * 40, "images": {"api": "ghcr.io/example/api@sha256:" + "c" * 64},
        "artifacts": {},
    }).encode()
    manifest = "\n".join([
        "schema_version=2", "channel=dev", f"generation={generation}",
        "source_sha=" + "b" * 40, f"expires_at={int(now.timestamp()) + 3600}",
        f"release_path=artifacts/dev/releases/{generation}/release.json",
        f"release_sha256={hashlib.sha256(release).hexdigest()}",
    ])
    entered, proceed = threading.Event(), threading.Event()
    unavailable = False

    def respond(request):
        if unavailable:
            return httpx.Response(503)
        if request.url.host == "api.github.com":
            entered.set()
            assert proceed.wait(5)
            return httpx.Response(200, json={"sha": "d" * 40})
        if request.url.path.endswith("current.manifest"):
            return httpx.Response(200, text=manifest)
        return httpx.Response(200, content=release)

    observer = DeploymentObserver(sessions, channel="dev", transport=httpx.MockTransport(respond), clock=lambda: now)
    try:
        assert observer.tick() is False
        assert entered.wait(5)
        assert observer.tick() is False
        proceed.set()
        observer.pending.result(timeout=5)
        assert observer.tick() is True
        projected = DeploymentProvenanceService(sessions, clock=lambda: now).snapshot()
        assert projected.platform[0].state == "repository_not_published"
        assert projected.platform[1].source_commit == "b" * 40
        assert projected.platform[1].image_digest == "sha256:" + "c" * 64
        assert projected.platform[2].image_digest is None
        unavailable = True
        now += timedelta(minutes=10)
        observer.refresh()
        projected = DeploymentProvenanceService(sessions, clock=lambda: now).snapshot()
        assert projected.platform[1].evidence.age_seconds == 600
        assert projected.platform[1].evidence.freshness == "stale"
        with sessions() as session:
            assert session.scalar(select(func.count()).select_from(Observation).where(Observation.kind.like("deployment.%"))) == 2
    finally:
        proceed.set()
        observer.close()


def test_inconsistent_publication_does_not_become_deployment_evidence(tmp_path):
    sessions, now, _, _ = deployment(tmp_path)
    observer = DeploymentObserver(sessions, transport=httpx.MockTransport(lambda _: httpx.Response(200, text="schema_version=1")), clock=lambda: now)
    try:
        observer.refresh()
        projected = DeploymentProvenanceService(sessions, clock=lambda: now).snapshot()
        assert all(boundary.state == "unknown" for boundary in projected.platform)
    finally:
        observer.close()
