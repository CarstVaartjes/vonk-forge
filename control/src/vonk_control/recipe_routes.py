"""Validated atomic LiteLLM publication for database-authoritative recipe runs."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .litellm import LiteLlmGeneration, LiteLlmPolicy, LiteLlmPublisher
from .models import AgentNode, RecipeRun, RunNode
from .presence import ManagementAddressPolicy, PresenceError
from .routes import RouteState

_ALIAS = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class RecipeRouteError(RuntimeError):
    pass


class AtomicRecipeRoutePublisher:
    """Adapt recipe routes to the controller's one atomic live bundle."""

    _AUTHORITY_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "https://vonkforge.ai/local-recipes"))

    def __init__(self, publisher: object, *, clock: Callable[[], datetime]) -> None:
        if not callable(getattr(publisher, "publish_compiled", None)):
            raise TypeError("atomic recipe route publisher is invalid")
        self._publisher = publisher
        self._clock = clock

    def publish(self, routes: RouteState, policy: LiteLlmPolicy) -> LiteLlmGeneration:
        return self._activate(
            routes.digest,
            LiteLlmPublisher.render(routes, policy),
            state="published",
        )

    def publish_empty(self, route_digest: str) -> LiteLlmGeneration:
        return self._activate(
            route_digest,
            LiteLlmPublisher.render_empty(),
            state="maintenance",
        )

    def _activate(
        self, route_digest: str, litellm: bytes, *, state: str
    ) -> LiteLlmGeneration:
        now = self._clock()
        routes = (
            json.dumps(
                {
                    "schema_version": 1,
                    "state": "static-proxy",
                    "routes": {},
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        marker = self._publisher.publish_compiled(
            authority_id=self._AUTHORITY_ID,
            plan_digest=route_digest,
            evidence_set_digest=route_digest,
            routes=routes,
            litellm=litellm,
            expires_at=now + timedelta(seconds=300),
            state=state,
        )
        config_sha256 = hashlib.sha256(litellm).hexdigest()
        root = getattr(self._publisher, "_root", None)
        path = (
            str(root / "generations" / marker.directory / "litellm.json")
            if hasattr(root, "joinpath")
            else marker.directory
        )
        return LiteLlmGeneration(
            marker.generation, route_digest, config_sha256, path
        )


class RecipeRouteService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        publisher: object,
        management_policy: ManagementAddressPolicy,
        clock: Callable[[], datetime],
        maximum_age_seconds: int = 300,
    ) -> None:
        if not 1 <= maximum_age_seconds <= 300:
            raise ValueError("recipe route evidence age is invalid")
        self.sessions = sessions
        self._publisher = publisher
        self._management_policy = management_policy
        self._clock = clock
        self._maximum_age = maximum_age_seconds

    def publish_run(self, run_id: str) -> LiteLlmGeneration:
        with self.sessions() as session:
            run = session.get(RecipeRun, run_id)
            if run is None:
                raise KeyError(run_id)
            if run.state != "running":
                raise RecipeRouteError("recipe run is not ready for publication")
        routes = self._candidate(exclude_run_id=None, include_run_id=run_id)
        if run_id not in routes[1]:
            raise RecipeRouteError("recipe run is absent from route candidate")
        generation = self._publisher.publish(routes[0], routes[2])
        with self.sessions.begin() as session:
            run = session.get(RecipeRun, run_id)
            if run is None or run.state != "running":
                raise RecipeRouteError("recipe run changed during publication")
            run.route_state = "published"
            run.route_generation = generation.generation
            run.route_digest = generation.route_digest
            run.route_error = None
            run.updated_at = self._clock()
        return generation

    def withdraw_run(self, run_id: str) -> LiteLlmGeneration:
        with self.sessions() as session:
            if session.get(RecipeRun, run_id) is None:
                raise KeyError(run_id)
        state, _included, policy = self._candidate(exclude_run_id=run_id)
        generation = (
            self._publisher.publish(state, policy)
            if state.aliases
            else self._publisher.publish_empty(state.digest)
        )
        with self.sessions.begin() as session:
            run = session.get(RecipeRun, run_id)
            if run is None:
                raise RecipeRouteError("recipe run changed during withdrawal")
            run.route_state = "withdrawn"
            run.route_generation = generation.generation
            run.route_digest = generation.route_digest
            run.route_error = None
            run.updated_at = self._clock()
        return generation

    def ranks_present(self, run_id: str) -> bool:
        now = self._now()
        with self.sessions() as session:
            nodes = tuple(
                session.scalars(
                    select(RunNode)
                    .where(RunNode.run_id == run_id)
                    .order_by(RunNode.rank)
                )
            )
            return bool(nodes) and all(
                self._agent_present(session.get(AgentNode, node.node_id), now)
                for node in nodes
            )

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RecipeRouteError("recipe route clock must be timezone-aware")
        return now.astimezone(UTC)

    def _agent_present(self, agent: AgentNode | None, now: datetime) -> bool:
        if (
            agent is None
            or agent.state != "active"
            or agent.revoked_at is not None
            or agent.last_seen_at is None
        ):
            return False
        observed = _aware(agent.last_seen_at)
        return observed <= now and now - observed <= timedelta(
            seconds=self._maximum_age
        )

    def _candidate(
        self, *, exclude_run_id: str | None, include_run_id: str | None = None
    ) -> tuple[RouteState, set[str], LiteLlmPolicy]:
        now = self._now()
        aliases: dict[str, str] = {}
        included: set[str] = set()
        node_ids: set[str] = set()
        evidence_times: list[datetime] = []
        run_identities: list[dict[str, object]] = []
        with self.sessions() as session:
            runs = tuple(
                session.scalars(
                    select(RecipeRun)
                    .where(RecipeRun.state == "running")
                    .order_by(RecipeRun.alias, RecipeRun.id)
                )
            )
            for run in runs:
                if run.id == exclude_run_id:
                    continue
                if (
                    run.id != include_run_id
                    and run.route_state not in {"pending", "published"}
                ):
                    continue
                if _ALIAS.fullmatch(run.alias) is None or run.alias in aliases:
                    raise RecipeRouteError("recipe run alias is invalid or duplicated")
                nodes = tuple(
                    session.scalars(
                        select(RunNode)
                        .where(RunNode.run_id == run.id)
                        .order_by(RunNode.rank)
                    )
                )
                if not nodes or any(node.state != "running" for node in nodes):
                    raise RecipeRouteError("every recipe rank must be running")
                expected = run.plan.get("nodes") if isinstance(run.plan, dict) else None
                if isinstance(expected, list) and len(expected) != len(nodes):
                    raise RecipeRouteError("recipe rank set does not match accepted plan")
                entrypoints = [node for node in nodes if node.role == "entrypoint"]
                if len(entrypoints) != 1 or entrypoints[0].rank != 0:
                    raise RecipeRouteError("recipe run must have one rank-zero entrypoint")
                for node in nodes:
                    agent = session.get(AgentNode, node.node_id)
                    if not self._agent_present(agent, now):
                        raise RecipeRouteError("recipe rank presence is stale")
                    assert agent is not None and agent.last_seen_at is not None
                    observed = _aware(agent.last_seen_at)
                    if not isinstance(node.evidence_digest, str) or _DIGEST.fullmatch(node.evidence_digest) is None:
                        raise RecipeRouteError("recipe rank readiness identity is invalid")
                    evidence_times.append(observed)
                    node_ids.add(node.node_id)
                entrypoint = entrypoints[0]
                aliases[run.alias] = _endpoint(entrypoint, self._management_policy)
                included.add(run.id)
                run_identities.append(
                    {
                        "run_id": run.id,
                        "alias": run.alias,
                        "plan_digest": run.plan_digest,
                        "ranks": [
                            {
                                "node_id": node.node_id,
                                "rank": node.rank,
                                "role": node.role,
                                "evidence_digest": node.evidence_digest,
                            }
                            for node in nodes
                        ],
                    }
                )
        identity = {
            "schema_version": 1,
            "runs": run_identities,
            "aliases": aliases,
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        state = RouteState(
            generation=0,
            state="published",
            commit=None,
            profile="recipe",
            workload="recipe",
            node_ids=tuple(sorted(node_ids)),
            aliases=aliases,
            health_timestamp=max(evidence_times).isoformat() if evidence_times else None,
            reason=None,
            digest=digest,
        )
        policy = LiteLlmPolicy(
            models={
                alias: {"requests_per_minute": 60, "tokens_per_minute": 1_000_000}
                for alias in aliases
            }
        )
        return state, included, policy


def _aware(value: datetime) -> datetime:
    return (value if value.tzinfo is not None else value.replace(tzinfo=UTC)).astimezone(UTC)


def _endpoint(node: RunNode, management_policy: ManagementAddressPolicy) -> str:
    raw = node.endpoint.get("url") if isinstance(node.endpoint, dict) else None
    if not isinstance(raw, str):
        raise RecipeRouteError("entrypoint endpoint evidence is missing")
    try:
        parsed = urlsplit(raw)
        raw_address = parsed.hostname or ""
        address = ipaddress.ip_address(raw_address)
        port = parsed.port
    except ValueError as error:
        raise RecipeRouteError("entrypoint endpoint is invalid") from error
    if (
        parsed.scheme != "http"
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or port != node.port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") not in {"", "/v1"}
    ):
        raise RecipeRouteError("entrypoint endpoint is outside management policy")
    try:
        management_policy.validate(str(address))
    except PresenceError as error:
        raise RecipeRouteError("entrypoint endpoint is outside management policy") from error
    host = f"[{address}]" if isinstance(address, ipaddress.IPv6Address) else str(address)
    return f"http://{host}:{port}/v1"


__all__ = [
    "AtomicRecipeRoutePublisher",
    "RecipeRouteError",
    "RecipeRouteService",
]
