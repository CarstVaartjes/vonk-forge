"""Validated atomic LiteLLM publication for database-authoritative recipe runs."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, sessionmaker

from .litellm import LiteLlmGeneration, LiteLlmPolicy, LiteLlmPublisher
from .models import (
    LocalRecipeRevision,
    RecipeInstallation,
    RecipeRun,
    Reconciliation,
    RoutePublication,
    RoutePublicationOwner,
    RunNode,
)
from .presence import ManagementAddressPolicy, PresenceError
from .route_runtime import RECIPE_ROUTE_AUTHORITY_ID, ActivationMarker
from .routes import RouteState

_ALIAS = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}\Z")
_UPSTREAM_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,119}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_HEALTH_RECOVERY_ERROR = "recipe rank health requires recovery"


class RecipeRouteError(RuntimeError):
    def __init__(self, message: str, *, run_id: str | None = None) -> None:
        super().__init__(message)
        self.run_id = run_id


@dataclass(frozen=True)
class _RecipeEndpoint:
    node_id: str
    address: str
    port: int
    observed_at: datetime
    evidence_digest: str
    operation_id: str

    @property
    def api_base(self) -> str:
        address = ipaddress.ip_address(self.address)
        host = (
            f"[{address}]"
            if isinstance(address, ipaddress.IPv6Address)
            else str(address)
        )
        return f"http://{host}:{self.port}/v1"

    def route_document(self) -> dict[str, object]:
        return {
            "address": self.address,
            "evidence_digest": self.evidence_digest,
            "node_id": self.node_id,
            "observed_at": self.observed_at.isoformat(),
            "operation_id": self.operation_id,
            "path": "/v1",
            "port": self.port,
            "scheme": "http",
            "verify_evidence_digest": self.evidence_digest,
        }


@dataclass(frozen=True)
class _RecipeCandidate:
    state: RouteState
    included: frozenset[str]
    policy: LiteLlmPolicy
    endpoints: dict[str, _RecipeEndpoint]
    expires_at: datetime


@dataclass(frozen=True)
class _AtomicRecipeGeneration(LiteLlmGeneration):
    activation_marker: ActivationMarker


class AtomicRecipeRoutePublisher:
    """Adapt recipe routes to the controller's one atomic live bundle."""

    _AUTHORITY_ID = RECIPE_ROUTE_AUTHORITY_ID

    def __init__(self, publisher: object, *, clock: Callable[[], datetime]) -> None:
        if not callable(getattr(publisher, "publish_compiled", None)):
            raise TypeError("atomic recipe route publisher is invalid")
        self._publisher = publisher
        self._clock = clock

    def publish_recipe(self, candidate: _RecipeCandidate) -> LiteLlmGeneration:
        return self._activate(
            candidate.state.digest,
            LiteLlmPublisher.render(candidate.state, candidate.policy),
            endpoints=candidate.endpoints,
            expires_at=candidate.expires_at,
            state="published",
        )

    def publish_empty(
        self, route_digest: str, *, expires_at: datetime
    ) -> LiteLlmGeneration:
        return self._activate(
            route_digest,
            LiteLlmPublisher.render_empty(),
            endpoints={},
            expires_at=expires_at,
            state="maintenance",
        )

    def _activate(
        self,
        route_digest: str,
        litellm: bytes,
        *,
        endpoints: dict[str, _RecipeEndpoint],
        expires_at: datetime,
        state: str,
    ) -> LiteLlmGeneration:
        required = (
            "_activate",
            "_identity",
            "_lease",
            "_locked",
            "_read_marker",
            "_require_supervisor_ack",
            "_require_update_boundary",
        )
        if any(not callable(getattr(self._publisher, name, None)) for name in required):
            raise TypeError(
                "atomic recipe route publisher lacks compiled route support"
            )
        self._publisher._identity(self._AUTHORITY_ID, route_digest, route_digest)
        with self._publisher._locked():
            self._publisher._require_update_boundary(None)
            issued, expires = self._publisher._lease(expires_at)
            current = self._publisher._read_marker(
                optional=True, verify_files=True, verify_lease=False
            )
            generation = (current.generation if current is not None else 0) + 1
            route_document: dict[str, object] = {
                "generation": generation,
                "routes": {
                    alias: endpoint.route_document()
                    for alias, endpoint in sorted(endpoints.items())
                },
                "schema_version": 1,
                "state": state,
            }
            if state == "maintenance":
                route_document["reason"] = "recipe routes withdrawn"
            routes = (
                json.dumps(route_document, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            marker = self._publisher._activate(
                generation=generation,
                state=state,
                reconciliation_id=self._AUTHORITY_ID,
                plan_digest=route_digest,
                evidence_set_digest=route_digest,
                routes=routes,
                litellm=litellm,
                issued=issued,
                expires=expires,
            )
            self._publisher._require_supervisor_ack(marker)
        config_sha256 = hashlib.sha256(litellm).hexdigest()
        root = getattr(self._publisher, "_root", None)
        path = (
            str(root / "generations" / marker.directory / "litellm.json")
            if hasattr(root, "joinpath")
            else marker.directory
        )
        return _AtomicRecipeGeneration(
            marker.generation,
            route_digest,
            config_sha256,
            path,
            marker,
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
        self._maximum_age_seconds = maximum_age_seconds
        self._maximum_age = timedelta(seconds=maximum_age_seconds)

    def publish_run(self, run_id: str) -> LiteLlmGeneration:
        with self.sessions() as session:
            run = session.get(RecipeRun, run_id)
            if run is None:
                raise KeyError(run_id)
            if run.state != "running":
                raise RecipeRouteError("recipe run is not ready for publication")
        candidate = self._candidate(include_run_id=run_id, exclude_run_ids=frozenset())
        if run_id not in candidate.included:
            raise RecipeRouteError("recipe run is absent from route candidate")
        generation = self._publish(candidate)
        with self.sessions.begin() as session:
            run = session.get(RecipeRun, run_id)
            if run is None or run.state != "running":
                raise RecipeRouteError("recipe run changed during publication")
            self._project_activation(session, generation, state="completed")
            for included_id in candidate.included:
                included = session.get(RecipeRun, included_id)
                if included is None or included.state != "running":
                    raise RecipeRouteError("recipe run changed during publication")
                included.route_state = "published"
                included.route_generation = generation.generation
                included.route_digest = generation.route_digest
                included.route_error = None
                included.updated_at = self._clock()
        return generation

    def withdraw_run(self, run_id: str) -> LiteLlmGeneration:
        with self.sessions() as session:
            if session.get(RecipeRun, run_id) is None:
                raise KeyError(run_id)
        return self._withdraw_runs(frozenset({run_id}))

    def _withdraw_runs(self, initial_run_ids: frozenset[str]) -> LiteLlmGeneration:
        excluded = set(initial_run_ids)
        while True:
            try:
                candidate = self._candidate(
                    include_run_id=None, exclude_run_ids=frozenset(excluded)
                )
                break
            except RecipeRouteError as error:
                if error.run_id is None or error.run_id in excluded:
                    raise
                excluded.add(error.run_id)
        generation = (
            self._publish(candidate)
            if candidate.state.aliases
            else self._publish_empty(candidate.state.digest)
        )
        with self.sessions.begin() as session:
            self._project_activation(
                session,
                generation,
                state=("completed" if candidate.state.aliases else "routes-withdrawn"),
            )
            for included_id in candidate.included:
                included = session.get(RecipeRun, included_id)
                if included is not None:
                    included.route_state = "published"
                    included.route_generation = generation.generation
                    included.route_digest = generation.route_digest
                    included.route_error = None
            for run_id in excluded:
                run = session.get(RecipeRun, run_id)
                if run is None:
                    if run_id in initial_run_ids:
                        raise RecipeRouteError("recipe run changed during withdrawal")
                    continue
                run.route_state = "withdrawn"
                run.route_generation = generation.generation
                run.route_digest = generation.route_digest
                run.route_error = None
                run.updated_at = self._clock()
        return generation

    def maintain(self, *, renew_before_seconds: int = 60) -> bool:
        if not 1 <= renew_before_seconds < self._maximum_age_seconds:
            raise ValueError("recipe route renewal window is invalid")
        with self.sessions() as session:
            published = tuple(
                session.scalars(
                    select(RecipeRun)
                    .where(RecipeRun.route_state == "published")
                    .order_by(RecipeRun.created_at, RecipeRun.id)
                )
            )
            recovering = tuple(
                session.scalars(
                    select(RecipeRun)
                    .where(
                        RecipeRun.state == "running",
                        RecipeRun.route_state == "withdrawn",
                        RecipeRun.route_error == _HEALTH_RECOVERY_ERROR,
                    )
                    .order_by(RecipeRun.created_at, RecipeRun.id)
                )
            )
        for run in recovering:
            try:
                self.publish_run(run.id)
            except RecipeRouteError:
                continue
            return True
        if not published:
            return False
        not_running = frozenset(run.id for run in published if run.state != "running")
        if not_running:
            self._withdraw_runs(not_running)
            return True
        try:
            candidate = self._candidate(
                include_run_id=None, exclude_run_ids=frozenset()
            )
        except RecipeRouteError as error:
            if error.run_id is None:
                raise
            published_ids = frozenset(run.id for run in published)
            self.withdraw_run(error.run_id)
            with self.sessions.begin() as session:
                withdrawn = tuple(
                    session.scalars(
                        select(RecipeRun).where(
                            RecipeRun.id.in_(published_ids),
                            RecipeRun.state == "running",
                            RecipeRun.route_state == "withdrawn",
                        )
                    )
                )
                for run in withdrawn:
                    run.route_error = _HEALTH_RECOVERY_ERROR
                    run.updated_at = self._clock()
            return True
        if not isinstance(self._publisher, AtomicRecipeRoutePublisher):
            if any(run.route_digest != candidate.state.digest for run in published):
                generation = self._publish(candidate)
                with self.sessions.begin() as session:
                    for run_id in candidate.included:
                        run = session.get(RecipeRun, run_id)
                        if run is not None:
                            run.route_generation = generation.generation
                            run.route_digest = generation.route_digest
                            run.route_error = None
                            run.updated_at = self._clock()
                return True
            return False
        now = _aware(self._clock())
        with self.sessions() as session:
            owner = session.get(RoutePublicationOwner, 1)
            publication = (
                session.get(RoutePublication, RECIPE_ROUTE_AUTHORITY_ID)
                if owner is not None
                and owner.reconciliation_id == RECIPE_ROUTE_AUTHORITY_ID
                else None
            )
            current_expiry = (
                _aware(publication.lease_expires_at)
                if publication is not None and publication.lease_expires_at is not None
                else None
            )
            current_digest = (
                publication.evidence_digest if publication is not None else None
            )
            durable_current = (
                owner is not None
                and publication is not None
                and publication.state == "completed"
                and publication.generation == owner.owner_generation
            )
        evidence_changed = current_digest != candidate.state.digest
        renewal_due = (
            current_expiry is not None
            and current_expiry - now <= timedelta(seconds=renew_before_seconds)
            and candidate.expires_at > current_expiry
        )
        if not durable_current or evidence_changed or renewal_due:
            generation = self._publish(candidate)
            with self.sessions.begin() as session:
                self._project_activation(session, generation, state="completed")
                for run_id in candidate.included:
                    run = session.get(RecipeRun, run_id)
                    if run is not None:
                        run.route_generation = generation.generation
                        run.route_digest = generation.route_digest
                        run.route_error = None
                        run.updated_at = self._clock()
            return True
        return False

    def _publish(self, candidate: _RecipeCandidate) -> LiteLlmGeneration:
        publish_recipe = getattr(self._publisher, "publish_recipe", None)
        if callable(publish_recipe):
            return publish_recipe(candidate)
        return self._publisher.publish(candidate.state, candidate.policy)

    def _publish_empty(self, route_digest: str) -> LiteLlmGeneration:
        publish_empty = self._publisher.publish_empty
        if isinstance(self._publisher, AtomicRecipeRoutePublisher):
            return publish_empty(
                route_digest, expires_at=_aware(self._clock()) + self._maximum_age
            )
        return publish_empty(route_digest)

    def _project_activation(
        self, session: Session, generation: LiteLlmGeneration, *, state: str
    ) -> None:
        marker = getattr(generation, "activation_marker", None)
        if not isinstance(marker, ActivationMarker):
            return
        now = _aware(self._clock())
        graph = {
            "base_commit": "recipe",
            "nodes": [],
            "schema_version": 1,
            "targets": [],
        }
        reconciliation = session.get(Reconciliation, RECIPE_ROUTE_AUTHORITY_ID)
        if reconciliation is None:
            reconciliation = Reconciliation(
                id=RECIPE_ROUTE_AUTHORITY_ID,
                base_commit="recipe",
                status="succeeded",
                summary={"authority": "recipe-routes"},
                graph=graph,
                graph_digest=hashlib.sha256(
                    json.dumps(graph, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                plan_digest=marker.plan_digest,
                current_phase="completed",
                created_at=now,
            )
            session.add(reconciliation)
            session.flush()
        else:
            reconciliation.status = "succeeded"
            reconciliation.plan_digest = marker.plan_digest
            reconciliation.current_phase = "completed"
            reconciliation.terminal_reason = None
        publication = session.get(RoutePublication, RECIPE_ROUTE_AUTHORITY_ID)
        values = {
            "state": state,
            "generation": marker.generation,
            "plan_digest": marker.plan_digest,
            "evidence_digest": marker.evidence_set_digest,
            "route_digest": marker.routes_sha256,
            "litellm_digest": marker.litellm_sha256,
            "bundle_digest": marker.manifest_sha256,
            "activation_marker": asdict(marker),
            "activation_marker_digest": marker.digest,
            "lease_issued_at": datetime.fromisoformat(marker.issued_at),
            "lease_expires_at": datetime.fromisoformat(marker.expires_at),
        }
        if publication is None:
            publication = RoutePublication(
                reconciliation_id=RECIPE_ROUTE_AUTHORITY_ID, **values
            )
            session.add(publication)
        else:
            for field, value in values.items():
                setattr(publication, field, value)
        owner = session.get(RoutePublicationOwner, 1)
        if owner is None:
            session.add(
                RoutePublicationOwner(
                    singleton_id=1,
                    reconciliation_id=RECIPE_ROUTE_AUTHORITY_ID,
                    owner_generation=marker.generation,
                    updated_at=now,
                )
            )
        else:
            owner.reconciliation_id = RECIPE_ROUTE_AUTHORITY_ID
            owner.owner_generation = marker.generation
            owner.updated_at = now

    def _candidate(
        self, *, include_run_id: str | None, exclude_run_ids: frozenset[str]
    ) -> _RecipeCandidate:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RecipeRouteError("recipe route clock must be timezone-aware")
        aliases: dict[str, str] = {}
        upstream_models: dict[str, str] = {}
        endpoints: dict[str, _RecipeEndpoint] = {}
        included: set[str] = set()
        node_ids: set[str] = set()
        evidence_times: list[datetime] = []
        run_identities: list[dict[str, object]] = []
        with self.sessions() as session:
            runs = tuple(
                session.scalars(
                    select(RecipeRun)
                    .where(
                        RecipeRun.state == "running",
                        or_(
                            RecipeRun.route_state == "published",
                            RecipeRun.id == include_run_id,
                        ),
                    )
                    .order_by(RecipeRun.alias, RecipeRun.id)
                )
            )
            for run in runs:
                if run.id in exclude_run_ids:
                    continue
                if _ALIAS.fullmatch(run.alias) is None or run.alias in aliases:
                    raise RecipeRouteError(
                        "recipe run alias is invalid or duplicated", run_id=run.id
                    )
                upstream_model = _primary_model_alias(session, run)
                nodes = tuple(
                    session.scalars(
                        select(RunNode)
                        .where(RunNode.run_id == run.id)
                        .order_by(RunNode.rank)
                    )
                )
                if not nodes or any(node.state != "running" for node in nodes):
                    raise RecipeRouteError(
                        "every recipe rank must be running", run_id=run.id
                    )
                if tuple(node.rank for node in nodes) != tuple(range(len(nodes))):
                    raise RecipeRouteError(
                        "recipe rank set is not exact", run_id=run.id
                    )
                expected = run.plan.get("nodes") if isinstance(run.plan, dict) else None
                if isinstance(expected, list):
                    expected_identity = (
                        {
                            (item.get("node_id"), item.get("rank"), item.get("role"))
                            for item in expected
                        }
                        if all(isinstance(item, Mapping) for item in expected)
                        else set()
                    )
                    actual_identity = {
                        (node.node_id, node.rank, node.role) for node in nodes
                    }
                    if (
                        len(expected) != len(nodes)
                        or expected_identity != actual_identity
                    ):
                        raise RecipeRouteError(
                            "recipe rank set does not match accepted plan",
                            run_id=run.id,
                        )
                entrypoints = [node for node in nodes if node.role == "entrypoint"]
                if len(entrypoints) != 1 or entrypoints[0].rank != 0:
                    raise RecipeRouteError(
                        "recipe run must have one rank-zero entrypoint", run_id=run.id
                    )
                for node in nodes:
                    observed = _aware(node.updated_at)
                    if (
                        observed > now.astimezone(UTC)
                        or now.astimezone(UTC) - observed >= self._maximum_age
                    ):
                        raise RecipeRouteError(
                            "recipe rank readiness evidence is stale", run_id=run.id
                        )
                    if (
                        not isinstance(node.evidence_digest, str)
                        or _DIGEST.fullmatch(node.evidence_digest) is None
                    ):
                        raise RecipeRouteError(
                            "recipe rank readiness identity is invalid", run_id=run.id
                        )
                    evidence_times.append(observed)
                    node_ids.add(node.node_id)
                entrypoint = entrypoints[0]
                endpoint = _endpoint(
                    entrypoint,
                    self._management_policy,
                    operation_id=f"recipe:{run.id}:rank:0",
                )
                aliases[run.alias] = endpoint.api_base
                upstream_models[run.alias] = upstream_model
                included.add(run.id)
                endpoints[run.alias] = endpoint
                run_identities.append(
                    {
                        "run_id": run.id,
                        "alias": run.alias,
                        "plan_digest": run.plan_digest,
                        "upstream_model": upstream_model,
                        # Observation time bounds the activation lease below;
                        # keeping it out of route identity avoids generating a
                        # new bundle for every otherwise identical heartbeat.
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
            health_timestamp=max(evidence_times).isoformat()
            if evidence_times
            else None,
            reason=None,
            digest=digest,
        )
        policy = LiteLlmPolicy(
            models={
                alias: {
                    "requests_per_minute": 60,
                    "tokens_per_minute": 1_000_000,
                    "upstream_model": upstream_models[alias],
                }
                for alias in aliases
            }
        )
        expires_at = (
            min(observed + self._maximum_age for observed in evidence_times)
            if evidence_times
            else _aware(now) + self._maximum_age
        )
        return _RecipeCandidate(
            state,
            frozenset(included),
            policy,
            endpoints,
            expires_at,
        )


def _primary_model_alias(session: Session, run: RecipeRun) -> str:
    installation = session.get(RecipeInstallation, run.installation_id)
    revision = (
        session.get(LocalRecipeRevision, installation.recipe_revision_id)
        if installation is not None
        else None
    )
    runtime = revision.document.get("runtime") if revision is not None else None
    endpoint = runtime.get("endpoint") if isinstance(runtime, Mapping) else None
    model_aliases = (
        endpoint.get("model_aliases") if isinstance(endpoint, Mapping) else None
    )
    primary = (
        model_aliases[0]
        if isinstance(model_aliases, list) and model_aliases
        else None
    )
    if not isinstance(primary, str) or _UPSTREAM_MODEL.fullmatch(primary) is None:
        raise RecipeRouteError(
            "recipe runtime model authority is invalid", run_id=run.id
        )
    return primary


def _aware(value: datetime) -> datetime:
    return (
        value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    ).astimezone(UTC)


def _endpoint(
    node: RunNode,
    management_policy: ManagementAddressPolicy,
    *,
    operation_id: str,
) -> _RecipeEndpoint:
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
        raise RecipeRouteError(
            "entrypoint endpoint is outside management policy"
        ) from error
    assert port is not None
    assert node.evidence_digest is not None
    return _RecipeEndpoint(
        node_id=node.node_id,
        address=str(address),
        port=port,
        observed_at=_aware(node.updated_at),
        evidence_digest=node.evidence_digest,
        operation_id=operation_id,
    )


__all__ = [
    "AtomicRecipeRoutePublisher",
    "RecipeRouteError",
    "RecipeRouteService",
]
