"""Validated atomic LiteLLM publication for database-authoritative recipe runs."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from datetime import datetime

from vonk_agent_protocol import GatewayRouteState
from vonk_agent_protocol.outcome import UnknownError

from ..bounded_retry import bounded_attempts
from ..litellm import (
    LiteLlmGeneration,
    LiteLlmPolicy,
    RouteState,
    render_config,
    render_empty_config,
)
from ..presence import ManagementAddressPolicy, PresenceError
from ..route_bundle_contract import (
    RouteBundleDocument,
)
from ..route_runtime import (
    RECIPE_ROUTE_AUTHORITY_ID,
    AtomicRouteBundlePublisher,
    RouteRuntimeError,
    recipe_route_run_id,
    verify_active_route_bundle,
)
from .shared import (
    _ALIAS,
    _NODE_ID,
    _UPSTREAM_MODEL,
    RecipeEndpointAuthorityRefused,
    RecipeRouteNotReady,
    _ActivatedRecipeRouteError,
    _AtomicRecipeGeneration,
    _aware,
    _RecipeCandidate,
    _RecipeEndpoint,
)


class AtomicRecipeRoutePublisher:
    """Adapt recipe routes to the controller's one atomic live bundle."""

    _AUTHORITY_ID = RECIPE_ROUTE_AUTHORITY_ID

    def __init__(self, publisher: AtomicRouteBundlePublisher) -> None:
        self._publisher = publisher

    def publish_recipe(self, candidate: _RecipeCandidate) -> LiteLlmGeneration:
        return self._activate(
            candidate.state.digest,
            render_config(candidate.state, candidate.policy),
            endpoints=candidate.endpoints,
            state=GatewayRouteState.PUBLISHED,
        )

    def publish_empty(self, route_digest: str) -> LiteLlmGeneration:
        return self._activate(
            route_digest,
            render_empty_config(),
            endpoints={},
            state=GatewayRouteState.MAINTENANCE,
        )

    def active_marker_digest(self) -> str | None:
        """Return the live marker digest, or ``None`` when none is readable."""

        try:
            marker = self._publisher._read_marker(optional=True, verify_files=True)
        except RouteRuntimeError:
            return None
        if marker is None or isinstance(marker, UnknownError):
            return None
        return marker.digest

    def accepted_run(
        self, run_id: str, policy: ManagementAddressPolicy
    ) -> _RecipeCandidate | None:
        """Reuse only the checksum-verified immutable route owned by this run."""
        if not (self._publisher._root / "activation.json").exists():
            return None
        bundle = verify_active_route_bundle(self._publisher._root)
        if isinstance(bundle, UnknownError):
            return None
        if (
            bundle.marker.authority_id != self._AUTHORITY_ID
            or bundle.marker.state != GatewayRouteState.PUBLISHED
        ):
            return None
        routes, runtime = bundle.routes, bundle.litellm
        if (
            routes is None
            or runtime is None
            or routes.schema_version != 2
            or routes.state != GatewayRouteState.PUBLISHED
            or routes.generation != bundle.marker.generation
            or bundle.marker.evidence_set_digest != bundle.marker.plan_digest
        ):
            return None
        found = [
            (alias, endpoint)
            for alias, endpoint in routes.routes.items()
            if recipe_route_run_id(endpoint.operation_id) == run_id
        ]
        if not found:
            return None
        if len(found) != 1:
            return None
        alias, raw = found[0]
        if _ALIAS.fullmatch(alias) is None or raw.scheme != "http" or raw.path != "/v1":
            return None
        try:
            policy.validate(raw.address)
        except PresenceError as error:
            raise RecipeEndpointAuthorityRefused(
                "accepted endpoint is outside management policy", run_id=run_id
            ) from error
        try:
            endpoint = _RecipeEndpoint(
                _NODE_ID.validate_python(raw.node_id),
                str(ipaddress.ip_address(raw.address)),
                raw.port,
                _aware(datetime.fromisoformat(raw.observed_at)),
                raw.operation_id,
            )
        except ValueError:
            return None
        entries = [item for item in runtime.model_list if item.model_name == alias]
        if len(entries) != 1:
            return None
        params = entries[0].litellm_params
        model, rpm, tpm = params.model, params.rpm, params.tpm
        if (
            not model.startswith("openai/")
            or _UPSTREAM_MODEL.fullmatch(model[7:]) is None
            or params.api_base != endpoint.api_base.rstrip("/")
            or params.api_key != "os.environ/LITELLM_UPSTREAM_KEY"
        ):
            return None
        quota = LiteLlmPolicy(
            {
                alias: {
                    "upstream_model": model[7:],
                    "requests_per_minute": rpm,
                    "tokens_per_minute": tpm,
                }
            }
        )
        # The renderer owns policy validation, including resource bounds.
        state = RouteState({alias: endpoint.api_base}, bundle.marker.plan_digest)
        try:
            render_config(state, quota)
        except ValueError:
            return None
        return _RecipeCandidate(state, frozenset({run_id}), quota, {alias: endpoint})

    def _next_generation(self) -> int:
        """Allocate above every staged generation, even past an unreadable marker."""

        highest = 0
        for directory in self._publisher._generations.iterdir():
            prefix = directory.name.partition("-")[0]
            if len(prefix) == 8 and prefix.isdigit():
                highest = max(highest, int(prefix))
        return highest + 1

    def _activate(
        self,
        route_digest: str,
        litellm: bytes,
        *,
        endpoints: dict[str, _RecipeEndpoint],
        state: GatewayRouteState,
    ) -> LiteLlmGeneration:
        """Reconcile the same exact activated bytes after a lost acknowledgement."""
        last: _ActivatedRecipeRouteError | None = None
        not_ready: RecipeRouteNotReady | None = None
        for _attempt in bounded_attempts():
            try:
                result = self._activate_once(
                    route_digest,
                    litellm,
                    endpoints=endpoints,
                    state=state,
                    expected_marker_digest=(
                        last.generation.activation_marker.digest
                        if last is not None
                        and isinstance(last.generation, _AtomicRecipeGeneration)
                        else None
                    ),
                )
                if result is None:
                    break
                return result
            except _ActivatedRecipeRouteError as error:
                last = error
            except RecipeRouteNotReady as error:
                # Keep any activated generation: a busy retry must not discard
                # the exact marker fence or hide an already completed effect.
                not_ready = error
        if last is not None:
            raise last
        assert not_ready is not None
        raise not_ready

    def _activate_once(
        self,
        route_digest: str,
        litellm: bytes,
        *,
        endpoints: dict[str, _RecipeEndpoint],
        state: GatewayRouteState,
        expected_marker_digest: str | None = None,
    ) -> LiteLlmGeneration | None:
        self._publisher._identity(self._AUTHORITY_ID, route_digest, route_digest)
        acknowledgement_error: Exception | None = None
        with self._publisher._locked() as uncertainty:
            if uncertainty is not None:
                raise RecipeRouteNotReady(uncertainty.reason)
            try:
                current = self._publisher._read_marker(optional=True, verify_files=True)
            except RouteRuntimeError:
                current = None
            current = None if isinstance(current, UnknownError) else current
            # A retry observes only its exact activation. A newer bundle wins;
            # the service fences the old claim and reports typed supersession.
            if expected_marker_digest is not None and (
                current is None or current.digest != expected_marker_digest
            ):
                return None

            def route_bytes(generation: int) -> bytes:
                document = RouteBundleDocument(
                    generation=generation,
                    routes={
                        alias: endpoint.route_document()
                        for alias, endpoint in sorted(endpoints.items())
                    },
                    schema_version=2,
                    state=state,
                    reason=(
                        "recipe routes withdrawn"
                        if state == GatewayRouteState.MAINTENANCE
                        else None
                    ),
                )
                return (
                    json.dumps(
                        document.model_dump(mode="json", exclude_none=True),
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode()

            # Adopt a lost acknowledgement only by exact verified bundle content.
            reuse_current = (
                current is not None
                and current.state == state
                and current.authority_id == self._AUTHORITY_ID
                and current.plan_digest == route_digest
                and current.evidence_set_digest == route_digest
                and current.routes_sha256
                == hashlib.sha256(route_bytes(current.generation)).hexdigest()
                and current.litellm_sha256 == hashlib.sha256(litellm).hexdigest()
            )
            if reuse_current:
                assert current is not None
                marker = current
            else:
                generation = self._next_generation()
                marker = self._publisher._activate(
                    generation=generation,
                    state=state,
                    authority_id=self._AUTHORITY_ID,
                    plan_digest=route_digest,
                    evidence_set_digest=route_digest,
                    routes=route_bytes(generation),
                    litellm=litellm,
                )
        if isinstance(marker, UnknownError):
            raise RecipeRouteNotReady(marker.reason)
        # Release the file lock before waiting for a live acknowledgement.
        try:
            uncertainty = self._publisher._require_supervisor_ack(marker)
            if uncertainty is not None:
                acknowledgement_error = RecipeRouteNotReady(uncertainty.reason)
        except Exception as error:  # noqa: BLE001
            acknowledgement_error = error
        config_sha256 = hashlib.sha256(litellm).hexdigest()
        path = str(
            self._publisher._root / "generations" / marker.directory / "litellm.json"
        )
        result = _AtomicRecipeGeneration(
            marker.generation,
            route_digest,
            config_sha256,
            path,
            marker,
        )
        if acknowledgement_error is not None:
            raise _ActivatedRecipeRouteError(
                "recipe route activation acknowledgement failed: "
                f"{acknowledgement_error}",
                generation=result,
            ) from acknowledgement_error
        return result
