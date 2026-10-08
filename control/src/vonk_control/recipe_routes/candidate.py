"""Validated atomic LiteLLM publication for database-authoritative recipe runs."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from collections.abc import Mapping
from datetime import UTC
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from vonk_agent_protocol import RouteState as RunRouteState
from vonk_agent_protocol import RunState
from vonk_forge_contracts import read_recipe

from ..interface_adapters import InterfaceAdapterError, interface_adapter
from ..litellm import (
    LiteLlmPolicy,
    RouteState,
)
from ..models import (
    AgentNode,
    CatalogDocumentRevision,
    ClusterMapping,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from ..presence import ManagementAddressPolicy, PresenceError
from ..recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_run_endpoint,
    parse_stored_run_plan,
    run_plan_document,
)
from ..route_bundle_contract import (
    RouteAcceptedModelPolicy,
    RouteAcceptedRunIdentity,
    RouteIdentityDocument,
    RouteRankIdentity,
    RouteRunIdentity,
)
from .shared import (
    _ALIAS,
    _UPSTREAM_MODEL,
    RecipeEndpointAuthorityRefused,
    RecipeRankStopped,
    RecipeRouteError,
    RecipeRouteNotReady,
    _aware,
    _RecipeCandidate,
    _RecipeEndpoint,
)

if TYPE_CHECKING:
    from .service import RecipeRouteService


def _candidate_once(
    self: RecipeRouteService,
    session: Session,
    *,
    include_run_id: str | None,
    exclude_run_ids: frozenset[str],
    lock: bool,
) -> _RecipeCandidate:
    now = _aware(self._clock())
    aliases: dict[str, str] = {}
    upstream_models: dict[str, str] = {}
    model_policies: dict[str, Mapping[str, int | str]] = {}
    endpoints: dict[str, _RecipeEndpoint] = {}
    included: set[str] = set()
    run_identities: list[RouteRunIdentity | RouteAcceptedRunIdentity] = []
    run_statement = (
        select(RecipeRun)
        .where(
            RecipeRun.state == RunState.RUNNING,
            or_(
                RecipeRun.route_state.in_(
                    [RunRouteState.PUBLISHED, RunRouteState.FAILED]
                ),
                RecipeRun.id == include_run_id,
            ),
        )
        .order_by(RecipeRun.alias, RecipeRun.id)
    )
    if lock:
        run_statement = run_statement.with_for_update(of=RecipeRun)
    runs = tuple(session.scalars(run_statement))
    # The newest accepted intent owns an alias. An explicit publication takes
    # precedence over older persisted projections, including damaged ones.
    owners: dict[str, RecipeRun] = {}
    for run in sorted(
        runs,
        key=lambda item: (item.id == include_run_id, item.created_at, item.id),
        reverse=True,
    ):
        if run.id not in exclude_run_ids:
            owners.setdefault(run.alias, run)
    candidate_runs = tuple(owners.values())
    node_statement = (
        select(RunNode)
        .where(RunNode.run_id.in_([run.id for run in candidate_runs]))
        .order_by(RunNode.run_id, RunNode.rank, RunNode.node_id)
    )
    if lock:
        node_statement = node_statement.with_for_update(of=RunNode)
    nodes_by_run: dict[str, list[RunNode]] = {run.id: [] for run in candidate_runs}
    if candidate_runs:
        for node in session.scalars(node_statement):
            nodes_by_run[node.run_id].append(node)
    for run in candidate_runs:
        nodes = tuple(nodes_by_run[run.id])
        # A route that already serves stays published while its ranks
        # run: missing, late or unmatched observations are bookkeeping,
        # not evidence the endpoint is unhealthy. A rank that stopped or
        # stayed unready past its grace period is evidence, and fails
        # below as before. A route not yet published still needs current
        # proof before it is first served.
        serving = (
            run.route_state in {RunRouteState.PUBLISHED, RunRouteState.FAILED}
            and run.id != include_run_id
        )
        if any(node.state in {RunState.STOPPED, RunState.FAILED} for node in nodes):
            raise RecipeRankStopped(
                "recipe rank reports a stopped or failed workload",
                run_id=run.id,
            )
        for node in nodes:
            agent = session.get(AgentNode, node.node_id)
            if agent is not None and agent.revoked_at is not None:
                raise RecipeEndpointAuthorityRefused(
                    "recipe rank node is revoked", run_id=run.id
                )
        try:
            retained: list[str] = []
            if _ALIAS.fullmatch(run.alias) is None or run.alias in aliases:
                raise RecipeRouteNotReady(
                    "recipe run alias is invalid or duplicated", run_id=run.id
                )
            upstream_model = _primary_model_alias(session, run)
            if not nodes or any(node.state != RunState.RUNNING for node in nodes):
                raise RecipeRouteNotReady(
                    "every recipe rank must be running", run_id=run.id
                )
            if tuple(node.rank for node in nodes) != tuple(range(len(nodes))):
                raise RecipeRouteNotReady("recipe rank set is not exact", run_id=run.id)
            try:
                stored_run_plan = run_plan_document(run.plan)
            except RecipeExecutionContractError as error:
                raise RecipeRouteNotReady(
                    "stored recipe run plan is invalid", run_id=run.id
                ) from error
            expected = stored_run_plan.get("nodes")
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
                if len(expected) != len(nodes) or expected_identity != actual_identity:
                    raise RecipeRouteNotReady(
                        "recipe rank set does not match accepted plan",
                        run_id=run.id,
                    )
            exact_observations = stored_run_plan.get("observation_schema_version") == 2
            if len(nodes) > 1 and not exact_observations:
                raise RecipeRouteNotReady(
                    "distributed recipe route requires exact rank observations",
                    run_id=run.id,
                )
            mapping = session.get(ClusterMapping, run.mapping_id)
            entrypoints = [node for node in nodes if node.role == "entrypoint"]
            endpoint_owners = (
                [
                    node
                    for node in nodes
                    if node.node_id == mapping.endpoint_owner_node_id
                ]
                if mapping is not None and mapping.generation == run.mapping_generation
                else []
            )
            if (
                len(entrypoints) != 1
                or len(endpoint_owners) != 1
                or entrypoints[0] is not endpoint_owners[0]
            ):
                raise RecipeRouteNotReady(
                    "recipe run must have exactly one mapped endpoint-owner entrypoint",
                    run_id=run.id,
                )
            endpoint_owner = endpoint_owners[0]
            if exact_observations:
                for node in nodes:
                    if (
                        node.observed_run_generation != run.run_generation
                        or node.observation_observed_at is None
                    ):
                        if not serving:
                            raise RecipeRouteNotReady(
                                "recipe rank is awaiting current exact observation",
                                run_id=run.id,
                            )
                        retained.append(
                            f"rank {node.rank} has no current exact observation"
                        )
                    if node is endpoint_owner:
                        if node.observation_endpoint_ready is not True:
                            if not serving:
                                raise RecipeRouteNotReady(
                                    "recipe endpoint owner is awaiting exact readiness",
                                    run_id=run.id,
                                )
                            retained.append(
                                f"rank {node.rank} endpoint readiness is not proven"
                            )
                    elif node.observation_endpoint_ready is not None:
                        if not serving:
                            raise RecipeRouteNotReady(
                                "headless recipe rank exposed endpoint readiness",
                                run_id=run.id,
                            )
                        retained.append(
                            f"headless rank {node.rank} reported endpoint readiness"
                        )
            for node in nodes:
                observed = _aware(node.updated_at)
                if (
                    observed > now.astimezone(UTC)
                    or now.astimezone(UTC) - observed >= self._maximum_age
                ):
                    if not serving:
                        raise RecipeRouteNotReady(
                            "recipe rank readiness evidence is stale", run_id=run.id
                        )
                    age = int((now.astimezone(UTC) - observed).total_seconds())
                    retained.append(f"rank {node.rank} evidence is {age}s old")
            self._note_retained(run.id, retained)
            try:
                endpoint = _endpoint(
                    endpoint_owner,
                    self._management_policy,
                    operation_id=f"recipe:{run.id}:rank:{endpoint_owner.rank}",
                )
            except RecipeRouteError as error:
                # Bind the failure to its run so one bad endpoint withdraws
                # only that route instead of blocking every other one.
                error.run_id = run.id
                raise
            identity = RouteRunIdentity(
                run_id=run.id,
                alias=run.alias,
                plan_digest=run.plan_digest,
                run_generation=run.run_generation,
                upstream_model=upstream_model,
                ranks=[
                    RouteRankIdentity(
                        node_id=node.node_id, rank=node.rank, role=node.role
                    )
                    for node in nodes
                ],
            )
            model_policies[run.alias] = {
                "requests_per_minute": 60,
                "tokens_per_minute": 1_000_000,
                "upstream_model": upstream_model,
            }
            aliases[run.alias] = endpoint.api_base
            upstream_models[run.alias] = upstream_model
            included.add(run.id)
            endpoints[run.alias] = endpoint
            run_identities.append(identity)
        except (
            RecipeRouteError,
            ValidationError,
            RecipeExecutionContractError,
        ) as error:
            if not serving or isinstance(
                error, RecipeRankStopped | RecipeEndpointAuthorityRefused
            ):
                raise
            read_accepted = getattr(self._publisher, "accepted_run", None)
            accepted = (
                read_accepted(run.id, self._management_policy)
                if read_accepted is not None
                else None
            )
            if accepted is None:
                # Unknown history belongs to this owner. No unverifiable bytes
                # are copied into a new bundle and unrelated requests proceed.
                self._note_retained(run.id, [str(error)])
                continue
            retained_aliases: list[str] = []
            for accepted_alias, endpoint in accepted.endpoints.items():
                agent = session.get(AgentNode, endpoint.node_id)
                if agent is not None and agent.revoked_at is not None:
                    raise RecipeEndpointAuthorityRefused(
                        "accepted endpoint node is revoked", run_id=run.id
                    )
                if accepted_alias in aliases:
                    # A stale accepted projection cannot displace the owner
                    # already selected from current authorized intent.
                    continue
                try:
                    accepted_policy = RouteAcceptedModelPolicy.model_validate_json(
                        json.dumps(dict(accepted.policy.models[accepted_alias]))
                    )
                    accepted_identity = RouteAcceptedRunIdentity(
                        run_id=run.id,
                        alias=accepted_alias,
                        accepted_endpoint=endpoint.route_document(),
                        accepted_policy=accepted_policy,
                    )
                except ValidationError:
                    # Corruption belongs to this owner; never consume partial
                    # unverified identity or gate another owner's publication.
                    self._note_retained(run.id, [str(error)])
                    continue
                retained_aliases.append(accepted_alias)
                model_policies[accepted_alias] = accepted.policy.models[accepted_alias]
                aliases[accepted_alias] = endpoint.api_base
                endpoints[accepted_alias] = endpoint
                upstream_models[accepted_alias] = accepted_policy.upstream_model
                run_identities.append(accepted_identity)
            if retained_aliases:
                included.add(run.id)
                self._note_retained(run.id, [str(error)])
    identity = RouteIdentityDocument(runs=run_identities, aliases=aliases)
    digest = hashlib.sha256(
        json.dumps(
            identity.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    state = RouteState(aliases=aliases, digest=digest)
    policy = LiteLlmPolicy(models=model_policies)
    return _RecipeCandidate(state, frozenset(included), policy, endpoints)


def _primary_model_alias(session: Session, run: RecipeRun) -> str:
    stored = parse_stored_run_plan(run.plan)
    if stored.upstream_model is not None:
        return stored.upstream_model
    installation = session.get(RecipeInstallation, run.installation_id)
    revision = (
        session.get(CatalogDocumentRevision, installation.recipe_revision_id)
        if installation is not None
        else None
    )
    if revision is None or revision.kind != "recipe" or revision.schema_version != 2:
        raise RecipeRouteNotReady(
            "recipe runtime interface authority is stale", run_id=run.id
        )
    try:
        recipe = read_recipe(revision.document)
    except (TypeError, ValueError) as error:
        raise RecipeRouteNotReady(
            "recipe runtime interface authority is invalid", run_id=run.id
        ) from error
    interfaces = recipe.model_dump(mode="json").get("interfaces")
    interface = None
    if isinstance(interfaces, list):
        for value in interfaces:
            name = value.get("adapter") if isinstance(value, Mapping) else None
            if not isinstance(name, str):
                continue
            try:
                adapter = interface_adapter(name)
            except InterfaceAdapterError as error:
                raise RecipeRouteNotReady(
                    "recipe runtime interface authority is invalid", run_id=run.id
                ) from error
            if adapter.publication == "litellm":
                interface = value
                break
    if interface is None:
        raise RecipeRouteNotReady(
            "recipe run does not declare a LiteLLM interface", run_id=run.id
        )
    model_aliases = (
        interface.get("model_aliases") if isinstance(interface, Mapping) else None
    )
    primary = (
        model_aliases[0] if isinstance(model_aliases, list) and model_aliases else None
    )
    if not isinstance(primary, str) or _UPSTREAM_MODEL.fullmatch(primary) is None:
        raise RecipeRouteNotReady(
            "recipe runtime model authority is invalid", run_id=run.id
        )
    return primary


def _endpoint(
    node: RunNode,
    management_policy: ManagementAddressPolicy,
    *,
    operation_id: str,
) -> _RecipeEndpoint:
    try:
        endpoint = parse_stored_run_endpoint(node.endpoint)
    except RecipeExecutionContractError as error:
        raise RecipeRouteNotReady("entrypoint endpoint is invalid") from error
    if endpoint is None:
        raise RecipeRouteNotReady("entrypoint endpoint evidence is missing")
    raw = endpoint.url
    try:
        parsed = urlsplit(raw)
        raw_address = parsed.hostname or ""
        address = ipaddress.ip_address(raw_address)
        port = parsed.port
    except ValueError as error:
        raise RecipeRouteNotReady("entrypoint endpoint is invalid") from error
    if (
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
    ):
        raise RecipeEndpointAuthorityRefused(
            "entrypoint endpoint is outside management policy"
        )
    try:
        management_policy.validate(str(address))
    except PresenceError as error:
        raise RecipeEndpointAuthorityRefused(
            "entrypoint endpoint is outside management policy"
        ) from error
    if (
        parsed.scheme != "http"
        or port is None
        or port != node.port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path.rstrip("/") not in {"", "/v1"}
    ):
        raise RecipeRouteNotReady("entrypoint endpoint projection is unmatched")
    return _RecipeEndpoint(
        node_id=node.node_id,
        address=str(address),
        port=port,
        observed_at=_aware(node.updated_at),
        operation_id=operation_id,
    )


def candidate_in_session(
    self: RecipeRouteService,
    session: Session,
    *,
    include_run_id: str | None,
    exclude_run_ids: frozenset[str],
    lock: bool,
) -> _RecipeCandidate:
    """One authoritative observation; the request owner retries fresh transactions."""
    try:
        return _candidate_once(
            self,
            session,
            include_run_id=include_run_id,
            exclude_run_ids=exclude_run_ids,
            lock=lock,
        )
    except (ValidationError, RecipeExecutionContractError) as error:
        raise RecipeRouteNotReady(
            "stored route projection is unavailable", run_id=include_run_id
        ) from error
