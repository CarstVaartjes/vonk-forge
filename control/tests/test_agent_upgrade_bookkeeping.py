"""Agent-upgrade bookkeeping reconciles; a request the Controller cannot plan refuses.

A stored package or rollback source that cannot be used skips that Spark with a
recorded reason and the rollout goes on; the release channel answering
inconsistently asks the caller to retry.  The request intent, package descriptor
and target list a caller sends are validated at submit time as typed invalid
requests.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

import httpx2
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import InvalidRequestError, UnknownOutcomeError
from vonk_control.agent_jobs import AgentJobService
from vonk_control.agent_upgrades import (
    AgentUpgradeConflict,
    AgentUpgradeInvalid,
    AgentUpgradeRetryLater,
    AgentUpgradeService,
    _request_intent,
)
from vonk_control.bounded_json import require_mapping
from vonk_control.models import AgentNode, Base, Job

from .test_agent_upgrades import (  # noqa: F401  (the autouse fixture pins the rollback source)
    NODE_A,
    NODE_B,
    OLD_IDENTITY,
    PACKAGE,
    PACKAGE_MODEL,
    SOURCE,
    _rollout,
    _upgrade_node,
    published_source,
)


def _service(tmp_path, name="bookkeeping", transport=None):
    now = datetime(2026, 8, 27, tzinfo=UTC)
    engine = create_engine(f"sqlite:///{tmp_path / f'{name}.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    operations = AgentJobService(sessions, clock=lambda: now)
    upgrades = AgentUpgradeService(
        sessions,
        operations,
        clock=lambda: now,
        **(
            {"release_api_url": "http://caddy:8084", "transport": transport}
            if transport is not None
            else {}
        ),
    )
    return sessions, upgrades


@pytest.mark.parametrize(
    "intent",
    [
        "not an object",
        {"all": True},
        {"all": "yes", "selectors": None},
        {"all": True, "selectors": ["x"]},
        {"all": False, "selectors": []},
        {"all": False, "selectors": [""]},
    ],
)
def test_a_request_intent_that_is_not_one_is_an_invalid_request(intent) -> None:
    with pytest.raises(AgentUpgradeInvalid) as refused:
        _request_intent(intent, None)

    assert isinstance(refused.value, InvalidRequestError)
    # Existing handlers that catch the conflict keep catching it.
    assert isinstance(refused.value, AgentUpgradeConflict)


def test_a_package_or_targets_the_caller_sent_are_invalid_requests(tmp_path) -> None:
    _sessions, upgrades = _service(tmp_path)

    with pytest.raises(AgentUpgradeInvalid, match="package is invalid"):
        upgrades._package({**PACKAGE, "package_url": "http://example.test/x"})
    with pytest.raises(AgentUpgradeInvalid, match="targets are invalid"):
        upgrades.preview([NODE_A, NODE_A], PACKAGE_MODEL)


def test_an_inconsistent_release_asks_the_caller_to_retry(tmp_path) -> None:
    generation = "9" * 64
    package_path = (
        f"artifacts/dev/releases/{generation}/spark/current/"
        "linux-arm64/vonk-forge-agent.deb"
    )
    signature_raw = ("e" * 128 + "\n").encode()
    release = {
        "artifacts": {
            "agent-package-linux-arm64": {
                "architecture": "linux-arm64",
                # The package says one signature, the published file another.
                "host_signature": "f" * 128,
                "package_version": PACKAGE["package_version"],
                "path": package_path,
                "sha256": PACKAGE["package_sha256"],
                "size": PACKAGE["package_bytes"],
                "target_binary_digest": PACKAGE["target_binary_digest"],
                "target_build_digest": PACKAGE["target_build_digest"],
            },
            "agent-package-signature-linux-arm64": {
                "path": f"{package_path}.host.sig",
                "sha256": hashlib.sha256(signature_raw).hexdigest(),
                "size": len(signature_raw),
            },
        },
        "channel": "dev",
        "generation": generation,
    }

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/artifacts/dev/current.manifest":
            return httpx2.Response(
                200,
                text=(
                    f"generation={generation}\n"
                    f"release_path=artifacts/dev/releases/{generation}/release.json\n"
                ),
            )
        if request.url.path == f"/artifacts/dev/releases/{generation}/release.json":
            return httpx2.Response(200, content=json.dumps(release).encode())
        if request.url.path == f"/{package_path}.host.sig":
            return httpx2.Response(200, content=signature_raw)
        return httpx2.Response(404)

    _sessions, upgrades = _service(
        tmp_path, "inconsistent", transport=httpx2.MockTransport(handler)
    )

    with pytest.raises(AgentUpgradeRetryLater) as retry:
        upgrades.current_package()

    assert isinstance(retry.value, UnknownOutcomeError)
    assert isinstance(retry.value, AgentUpgradeConflict)
    assert "inconsistent" in str(retry.value)


def _node(tmp_path):
    sessions, upgrades = _service(tmp_path, "enqueue")
    with sessions.begin() as session:
        session.add(
            AgentNode(
                node_id=NODE_A,
                state="active",
                architecture="linux-arm64",
                semantic_version="0.1.0",
                build_digest=OLD_IDENTITY["build_digest"],
                binary_digest=OLD_IDENTITY["binary_digest"],
            )
        )
    return sessions, upgrades


def _parent(payload: dict[str, object]) -> Job:
    return Job(
        id=str(uuid.uuid4()),
        request_id=str(uuid.uuid4()),
        kind="agent-upgrade",
        state="running",
        actor="admin",
        authority_revision="d" * 64,
        targets=[NODE_A],
        payload_digest="0" * 64,
        payload=payload,
    )


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"package": "garbage"}, "stored agent upgrade package is invalid"),
        (
            {"package": dict(PACKAGE), "sources": {}},
            "stored rollback sources are invalid",
        ),
        (
            {"package": dict(PACKAGE), "sources": {NODE_A: "garbage"}},
            "stored rollback sources are invalid",
        ),
        (
            {
                "package": dict(PACKAGE),
                "sources": {
                    NODE_A: {
                        **SOURCE,
                        "build_digest": "sha256:" + "0" * 64,
                    }
                },
            },
            "rollback source no longer matches installed agent",
        ),
    ],
)
def test_a_stored_package_or_source_that_cannot_be_used_skips_the_spark(
    tmp_path, payload, reason
) -> None:
    sessions, upgrades = _node(tmp_path)
    with sessions() as session:
        outcome = upgrades._enqueue_node(session, _parent(payload), NODE_A)

    # Nothing is raised: the rollout records the reason and goes on to the next.
    assert outcome == reason


@pytest.mark.usefixtures("damaged_json_rows")
def test_a_rollout_with_a_damaged_source_skips_that_spark_and_finishes(
    tmp_path,
) -> None:
    sessions, operations, _upgrades, job = _rollout(tmp_path, "damaged-source")
    with sessions.begin() as session:
        parent = session.get(Job, job.id)
        assert parent is not None
        payload = dict(parent.payload)
        sources = dict(require_mapping(payload["sources"], "sources"))
        sources[NODE_B] = "garbage"
        payload["sources"] = sources
        parent.payload = payload

    _upgrade_node(operations, NODE_A, "serial-a")

    with sessions() as session:
        stored = session.get(Job, job.id)
        assert stored is not None
        assert stored.state == "succeeded"
        assert stored.result is not None
        assert stored.result["skipped"] == {
            NODE_B: "stored rollback sources are invalid"
        }
