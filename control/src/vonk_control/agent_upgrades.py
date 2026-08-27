"""Controller-owned rolling upgrades for enrolled Spark agents."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import AgentResult, canonical_message

from .agent_jobs import AgentJobService
from .models import AgentNode, AgentOperation, AgentOperationAttempt, Job

_PACKAGE_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+~-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_BUILD_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SIGNATURE = re.compile(r"[0-9a-f]{128}\Z")
_ONLINE_WINDOW = timedelta(seconds=150)


class AgentUpgradeConflict(RuntimeError):
    """An agent upgrade plan is invalid, stale, or not safely executable."""


@dataclass(frozen=True, slots=True)
class AgentUpgradePlan:
    authority_revision: str
    node_ids: tuple[str, ...]
    package: dict[str, object]
    plan_digest: str
    strategy: str


class AgentUpgradeService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        operations: AgentJobService,
        *,
        clock: Callable[[], datetime],
        current_revision: Callable[[], str],
        channel: str = "dev",
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._sessions = sessions
        self._operations = operations
        self._clock = clock
        self._current_revision = current_revision
        if channel not in {"dev", "stable"}:
            raise ValueError("agent upgrade channel is invalid")
        self._channel = channel
        self._http = httpx.Client(
            base_url="https://install.vonkforge.ai",
            follow_redirects=False,
            timeout=httpx.Timeout(15.0, connect=5.0),
            trust_env=False,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def current_package(self) -> dict[str, object]:
        prefix = f"/artifacts/{self._channel}"
        try:
            manifest_response = self._http.get(f"{prefix}/current.manifest")
            manifest_response.raise_for_status()
            if len(manifest_response.content) > 64 * 1024:
                raise AgentUpgradeConflict("agent release manifest is too large")
            manifest = dict(
                line.split("=", 1)
                for line in manifest_response.text.splitlines()
                if "=" in line
            )
            release_path = manifest.get("release_path", "")
            generation = manifest.get("generation", "")
            if (
                _SHA256.fullmatch(generation) is None
                or release_path
                != f"artifacts/{self._channel}/releases/{generation}/release.json"
            ):
                raise AgentUpgradeConflict("agent release manifest is invalid")
            release_response = self._http.get(f"/{release_path}")
            release_response.raise_for_status()
            if len(release_response.content) > 256 * 1024:
                raise AgentUpgradeConflict("agent release document is too large")
            release = release_response.json()
            artifact = release["artifacts"]["agent-package-linux-arm64"]
            signature_record = release["artifacts"][
                "agent-package-signature-linux-arm64"
            ]
            signature_response = self._http.get(f"/{signature_record['path']}")
            signature_response.raise_for_status()
            signature = signature_response.text.strip()
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
            raise AgentUpgradeConflict("current agent release is unavailable") from error
        if (
            release.get("channel") != self._channel
            or release.get("generation") != generation
            or artifact.get("host_signature") != signature
            or signature_record.get("sha256")
            != hashlib.sha256(signature_response.content).hexdigest()
            or signature_record.get("size") != len(signature_response.content)
        ):
            raise AgentUpgradeConflict("current agent release is inconsistent")
        return self._package(
            {
                "architecture": artifact.get("architecture"),
                "package_bytes": artifact.get("size"),
                "package_sha256": artifact.get("sha256"),
                "package_signature": signature,
                "package_url": f"https://install.vonkforge.ai/{artifact.get('path')}",
                "package_version": artifact.get("package_version"),
                "schema_version": 1,
                "target_binary_digest": artifact.get("target_binary_digest"),
                "target_build_digest": artifact.get("target_build_digest"),
            }
        )

    def preview(
        self,
        node_ids: Sequence[str] | None,
        package: Mapping[str, object],
        *,
        strategy: str = "one-at-a-time",
    ) -> AgentUpgradePlan:
        if strategy not in {"one-at-a-time", "all-at-once"}:
            raise AgentUpgradeConflict("agent upgrade rollout strategy is invalid")
        payload = self._package(package)
        authority_revision = self._current_revision()
        now = self._clock()
        with self._sessions() as session:
            requested = None if node_ids is None else tuple(node_ids)
            if requested is not None and (
                not requested
                or len(requested) != len(set(requested))
                or len(requested) > 64
            ):
                raise AgentUpgradeConflict("agent upgrade targets are invalid")
            candidates = list(
                session.scalars(
                    select(AgentNode)
                    if requested is None
                    else select(AgentNode).where(AgentNode.node_id.in_(requested))
                )
            )
            nodes = {node.node_id: node for node in candidates}
            targets = (
                tuple(
                    sorted(
                        node.node_id
                        for node in candidates
                        if self._eligible(node, payload, now)
                        and not self._at_target(node, payload)
                    )
                )
                if requested is None
                else requested
            )
            if not targets:
                raise AgentUpgradeConflict("no outdated upgrade-capable Sparks were found")
            if requested is not None and set(nodes) != set(targets):
                raise AgentUpgradeConflict("agent upgrade target does not exist")
            for node_id in targets:
                node = nodes[node_id]
                reason = self._ineligible_reason(node, payload, now)
                if reason is not None:
                    raise AgentUpgradeConflict(f"Spark {node_id} {reason}")
                if self._at_target(node, payload):
                    raise AgentUpgradeConflict(
                        f"Spark {node_id} already runs the requested agent build"
                    )
        document = {
            "authority_revision": authority_revision,
            "node_ids": list(targets),
            "package": payload,
            "strategy": strategy,
        }
        return AgentUpgradePlan(
            authority_revision=authority_revision,
            node_ids=targets,
            package=payload,
            plan_digest=hashlib.sha256(canonical_message(document)).hexdigest(),
            strategy=strategy,
        )

    def apply(
        self,
        node_ids: Sequence[str] | None,
        package: Mapping[str, object],
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        strategy: str = "one-at-a-time",
    ) -> Job:
        plan = self.preview(node_ids, package, strategy=strategy)
        if plan.plan_digest != plan_digest:
            raise AgentUpgradeConflict("agent upgrade preview is stale")
        now = self._clock()
        job = Job(
            request_id=request_id,
            kind="agent-upgrade",
            state="queued",
            actor=actor,
            authority_revision=plan.authority_revision,
            targets=list(plan.node_ids),
            payload_digest=plan.plan_digest,
            payload={
                "node_order": list(plan.node_ids),
                "package": plan.package,
                "strategy": plan.strategy,
            },
            current_attempt=0,
            created_at=now,
            updated_at=now,
        )
        with self._sessions.begin() as session:
            session.add(job)
            session.flush()
            if plan.strategy == "all-at-once":
                for node_id in plan.node_ids:
                    self._enqueue_node(session, job, node_id)
            else:
                self._enqueue_next(session, job)
        self._operations.notify_available()
        return job

    def consume_agent_result(
        self,
        session: Session,
        operation: AgentOperation,
        _attempt: AgentOperationAttempt,
        message: AgentResult,
    ) -> None:
        if operation.kind != "agent.upgrade.v1" or message.state != "succeeded":
            return
        parent = session.scalar(
            select(Job).where(Job.id == operation.parent_job_id).with_for_update(of=Job)
        )
        if parent is None or parent.kind != "agent-upgrade":
            return
        if parent.payload.get("strategy") == "one-at-a-time":
            self._enqueue_next(session, parent)

    def _enqueue_next(self, session: Session, parent: Job) -> None:
        package = parent.payload.get("package")
        order = parent.payload.get("node_order")
        if not isinstance(package, dict) or not isinstance(order, list):
            raise AgentUpgradeConflict("stored agent upgrade plan is invalid")
        existing = set(
            session.scalars(
                select(AgentOperation.node_id).where(
                    AgentOperation.parent_job_id == parent.id
                )
            )
        )
        next_node = next(
            (node_id for node_id in order if isinstance(node_id, str) and node_id not in existing),
            None,
        )
        if next_node is None:
            return
        self._enqueue_node(session, parent, next_node)

    def _enqueue_node(self, session: Session, parent: Job, node_id: str) -> None:
        package = parent.payload.get("package")
        if not isinstance(package, dict):
            raise AgentUpgradeConflict("stored agent upgrade package is invalid")
        self._operations.enqueue_in_session(
            session,
            parent.id,
            node_id,
            "agent.upgrade.v1",
            parent.authority_revision,
            package,
            operation_id=str(uuid.uuid4()),
        )

    @staticmethod
    def _package(value: Mapping[str, object]) -> dict[str, object]:
        document = dict(value)
        required = {
            "architecture",
            "package_bytes",
            "package_sha256",
            "package_signature",
            "package_url",
            "package_version",
            "schema_version",
            "target_binary_digest",
            "target_build_digest",
        }
        url = document.get("package_url")
        if (
            set(document) != required
            or document.get("schema_version") != 1
            or document.get("architecture") != "linux-arm64"
            or not isinstance(document.get("package_bytes"), int)
            or isinstance(document.get("package_bytes"), bool)
            or not 1 <= int(document["package_bytes"]) <= 1024**3
            or not isinstance(document.get("package_sha256"), str)
            or _SHA256.fullmatch(str(document["package_sha256"])) is None
            or not isinstance(document.get("package_signature"), str)
            or _SIGNATURE.fullmatch(str(document["package_signature"])) is None
            or not isinstance(document.get("package_version"), str)
            or _PACKAGE_VERSION.fullmatch(str(document["package_version"])) is None
            or not isinstance(document.get("target_binary_digest"), str)
            or _SHA256.fullmatch(str(document["target_binary_digest"])) is None
            or not isinstance(document.get("target_build_digest"), str)
            or _BUILD_DIGEST.fullmatch(str(document["target_build_digest"])) is None
            or not isinstance(url, str)
            or not url.startswith("https://install.vonkforge.ai/")
            or not url.endswith("/vonk-forge-agent.deb")
            or any(marker in url for marker in ("?", "#", "@"))
        ):
            raise AgentUpgradeConflict("agent upgrade package is invalid")
        return document

    @classmethod
    def _eligible(
        cls,
        node: AgentNode,
        package: Mapping[str, object],
        now: datetime,
    ) -> bool:
        return cls._ineligible_reason(node, package, now) is None

    @staticmethod
    def _ineligible_reason(
        node: AgentNode,
        package: Mapping[str, object],
        now: datetime,
    ) -> str | None:
        if node.state != "active" or node.revoked_at is not None:
            return "is not active"
        if "agent.upgrade.v1" not in set(node.capabilities or ()):
            return "does not support controller upgrades"
        if node.architecture != package["architecture"]:
            return "has an incompatible architecture"
        current = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
        last_seen = node.last_seen_at
        if last_seen is None:
            return "has never reported online"
        seen = (
            last_seen
            if last_seen.tzinfo is not None
            else last_seen.replace(tzinfo=UTC)
        )
        if seen > current or current - seen > _ONLINE_WINDOW:
            return "is not currently online"
        return None

    @staticmethod
    def _at_target(node: AgentNode, package: Mapping[str, object]) -> bool:
        return bool(
            node.build_digest == package["target_build_digest"]
            and node.binary_digest == package["target_binary_digest"]
            and node.self_test_passed is True
        )
