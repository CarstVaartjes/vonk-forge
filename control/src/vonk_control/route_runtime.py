"""Fail-closed, atomic route and LiteLLM bundle publication."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import stat
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from vonk_agent_protocol import canonical_message

from .litellm import LiteLlmDeployment, LiteLlmPublisher
from .presence import ManagementAddressPolicy, PresenceError

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40,64}\Z")
_NODE = re.compile(r"spk_[0-9a-f]{32}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z")
_OPERATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_DIRECTORY = re.compile(r"[0-9]{8}-[0-9a-f]{64}\Z")
_ROUTE_FIELDS = {
    "workload_id",
    "nodes",
    "entrypoint_node_id",
    "scheme",
    "port",
    "path",
    "quota",
    "quota_digest",
}
_QUOTA_FIELDS = {"requests_per_minute", "tokens_per_minute"}
_MARKER_FIELDS = {
    "schema_version",
    "generation",
    "state",
    "reconciliation_id",
    "plan_digest",
    "evidence_set_digest",
    "routes_sha256",
    "litellm_sha256",
    "issued_at",
    "expires_at",
    "directory",
    "manifest_sha256",
}

RECIPE_ROUTE_AUTHORITY_ID = str(
    uuid.uuid5(uuid.NAMESPACE_URL, "https://vonkforge.ai/local-recipes")
)
_ACK_FIELDS = {
    "acknowledged_at",
    "activation_sha256",
    "child_pid",
    "expires_at",
    "generation",
    "litellm_sha256",
    "schema_version",
    "state",
}
_UPDATE_BOUNDARY_FIELDS = {"key", "schema_version"}


class RouteRuntimeError(RuntimeError):
    """A route bundle could not be safely staged, activated, or inspected."""


def _encoded(value: Mapping[str, object]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_sha256(value: Mapping[str, object]) -> str:
    """Hash protocol documents without filesystem-only trailing whitespace."""

    return _sha256(canonical_message(value))


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RouteRuntimeError(f"{label} must include a timezone")
    return value.astimezone(UTC)


def _parse_time(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise RouteRuntimeError(f"activation {label} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise RouteRuntimeError(f"activation {label} is invalid") from error
    return _aware(parsed, f"activation {label}")


@dataclass(frozen=True)
class AcceptedEndpointEvidence:
    """Endpoint address carried by already-accepted, fenced operation evidence."""

    node_id: str
    address: str
    observed_at: datetime
    operation_id: str
    verify_evidence_digest: str
    evidence_digest: str


@dataclass(frozen=True)
class PublishedRoute:
    """A validated route safe to expose to commit-pinned LiteLLM policy."""

    alias: str
    workload_id: str
    api_base: str
    requests_per_minute: int
    tokens_per_minute: int


def build_published_route(
    alias: object,
    raw: object,
    address: object,
) -> PublishedRoute:
    """Build the sole canonical repository-policy view of a resolved route."""

    if (
        not isinstance(alias, str)
        or _IDENTIFIER.fullmatch(alias) is None
        or not isinstance(raw, Mapping)
        or set(raw) != _ROUTE_FIELDS
    ):
        raise RouteRuntimeError("route fields do not match the resolved plan")
    workload_id = raw.get("workload_id")
    scheme = raw.get("scheme")
    port = raw.get("port")
    path = raw.get("path")
    quota = raw.get("quota")
    if (
        not isinstance(workload_id, str)
        or _IDENTIFIER.fullmatch(workload_id) is None
        or scheme not in {"http", "https"}
        or isinstance(port, bool)
        or not isinstance(port, int)
        or not 1 <= port <= 65535
        or not isinstance(path, str)
        or not path.startswith("/")
        or "?" in path
        or "#" in path
        or "//" in path
        or "/../" in f"{path}/"
        or not isinstance(quota, Mapping)
        or set(quota) != _QUOTA_FIELDS
    ):
        raise RouteRuntimeError("route policy input is invalid")
    rpm = quota.get("requests_per_minute")
    tpm = quota.get("tokens_per_minute")
    if (
        isinstance(rpm, bool)
        or not isinstance(rpm, int)
        or not 1 <= rpm <= 100_000
        or isinstance(tpm, bool)
        or not isinstance(tpm, int)
        or not 1 <= tpm <= 100_000_000
    ):
        raise RouteRuntimeError("route policy quota is invalid")
    if not isinstance(address, str):
        raise RouteRuntimeError("route policy address is invalid")
    try:
        parsed_address = ipaddress.ip_address(address)
    except ValueError as error:
        raise RouteRuntimeError("route policy address is invalid") from error
    if parsed_address.compressed != address:
        raise RouteRuntimeError("route policy address is not canonical")
    host = (
        f"[{address}]" if isinstance(parsed_address, ipaddress.IPv6Address) else address
    )
    return PublishedRoute(
        alias=alias,
        workload_id=workload_id,
        api_base=f"{scheme}://{host}:{port}{path.rstrip('/')}",
        requests_per_minute=rpm,
        tokens_per_minute=tpm,
    )


def published_routes_digest(routes: tuple[PublishedRoute, ...]) -> str:
    """Hash the exact canonical route-policy input."""

    return hashlib.sha256(
        json.dumps(
            [asdict(route) for route in routes],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class RouteBundleRequest:
    reconciliation_id: str
    plan_digest: str
    evidence_set_digest: str
    routes: Mapping[str, object]
    endpoints: Mapping[str, AcceptedEndpointEvidence]
    expires_at: datetime
    base_commit: str = ""


@dataclass(frozen=True)
class ActivationMarker:
    schema_version: int
    generation: int
    state: str
    reconciliation_id: str
    plan_digest: str
    evidence_set_digest: str
    routes_sha256: str
    litellm_sha256: str
    issued_at: str
    expires_at: str
    directory: str
    manifest_sha256: str

    def canonical_bytes(self) -> bytes:
        """Return the exact representation persisted as the activation marker."""

        return _encoded(asdict(self))

    @property
    def digest(self) -> str:
        """Bind a durable database receipt to the exact activation marker bytes."""

        return _sha256(self.canonical_bytes())


@dataclass(frozen=True)
class VerifiedRouteBundle:
    """One canonical, checksum-bound, unexpired active route bundle."""

    marker: ActivationMarker
    routes: Mapping[str, object]
    litellm: Mapping[str, object]


class FileSupervisorAcknowledger:
    """Wait for a recent live-process ack bound to one exact marker request."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime],
        timeout_seconds: float = 30,
        maximum_ack_age_seconds: float = 5,
        poll_seconds: float = 0.1,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if (
            timeout_seconds <= 0
            or maximum_ack_age_seconds <= 0
            or poll_seconds <= 0
            or poll_seconds > timeout_seconds
        ):
            raise RouteRuntimeError("supervisor acknowledgement bounds are invalid")
        self._path = path
        self._clock = clock
        self._timeout_seconds = timeout_seconds
        self._maximum_age = timedelta(seconds=maximum_ack_age_seconds)
        self._poll_seconds = poll_seconds
        self._monotonic = monotonic
        self._sleep = sleep

    def __call__(self, marker: ActivationMarker) -> None:
        deadline = self._monotonic() + self._timeout_seconds
        expires = _parse_time(marker.expires_at, "expiry timestamp")
        while True:
            now = _aware(self._clock(), "supervisor acknowledgement clock")
            if now >= expires:
                raise RouteRuntimeError(
                    "active route lease expired during supervisor acknowledgement"
                )
            if self._matches(marker, now=now):
                return
            if self._monotonic() >= deadline:
                raise RouteRuntimeError(
                    "live LiteLLM supervisor acknowledgement timed out"
                )
            self._sleep(self._poll_seconds)

    def _matches(self, marker: ActivationMarker, *, now: datetime) -> bool:
        path = self._path
        if path.is_symlink() or not path.is_file() or path.parent.is_symlink():
            return False
        try:
            content = path.read_bytes()
            raw: Any = json.loads(content)
        except (OSError, json.JSONDecodeError):
            return False
        if (
            len(content) > 4096
            or not isinstance(raw, dict)
            or set(raw) != _ACK_FIELDS
            or content != _encoded(raw)
            or raw.get("schema_version") != 1
            or raw.get("state") != marker.state
            or raw.get("generation") != marker.generation
            or raw.get("activation_sha256") != marker.digest
            or raw.get("litellm_sha256") != marker.litellm_sha256
            or raw.get("expires_at") != marker.expires_at
            or isinstance(raw.get("child_pid"), bool)
            or not isinstance(raw.get("child_pid"), int)
            or raw["child_pid"] <= 0
        ):
            return False
        try:
            acknowledged = _parse_time(
                raw.get("acknowledged_at"), "acknowledgement timestamp"
            )
            issued = _parse_time(marker.issued_at, "issued timestamp")
            expires = _parse_time(marker.expires_at, "expiry timestamp")
        except RouteRuntimeError:
            return False
        return (
            issued <= acknowledged <= now < expires
            and now - acknowledged <= self._maximum_age
        )


def endpoint_evidence_digest(
    *,
    node_id: str,
    address: str,
    observed_at: datetime,
    operation_id: str,
    verify_evidence_digest: str,
) -> str:
    """Bind authenticated presence to the exact accepted verify evidence."""

    return _sha256(
        _encoded(
            {
                "address": address,
                "node_id": node_id,
                "observed_at": observed_at.astimezone(UTC).isoformat(),
                "operation_id": operation_id,
                "schema_version": 1,
                "verify_evidence_digest": verify_evidence_digest,
            }
        )
    )


class AtomicRouteBundlePublisher:
    """Stage a complete bundle and replace its sole activation marker last."""

    def __init__(
        self,
        root: Path,
        *,
        management_policy: ManagementAddressPolicy,
        clock: Callable[[], datetime],
        maximum_lease_seconds: int = 300,
        validate_routes: Callable[[bytes], bool] | None = None,
        validate_litellm: Callable[[bytes], bool] | None = None,
        await_supervisor_ack: Callable[[ActivationMarker], None] | None = None,
        litellm_deployments: Callable[
            [str, tuple[PublishedRoute, ...]],
            tuple[LiteLlmDeployment, ...],
        ]
        | None = None,
    ) -> None:
        if root.is_symlink():
            raise RouteRuntimeError("route runtime root must not be a symlink")
        if not 1 <= maximum_lease_seconds <= 3600:
            raise RouteRuntimeError("route lease bound is invalid")
        root.mkdir(parents=True, exist_ok=True, mode=0o750)
        root.chmod(0o750)
        generations = root / "generations"
        if generations.is_symlink():
            raise RouteRuntimeError("route generation root must not be a symlink")
        generations.mkdir(mode=0o750, exist_ok=True)
        generations.chmod(0o750)
        self._root = root
        self._generations = generations
        self._policy = management_policy
        self._clock = clock
        self._maximum_lease = timedelta(seconds=maximum_lease_seconds)
        self._validate_routes = validate_routes or self._valid_json_mapping
        self._validate_litellm = validate_litellm or self._valid_litellm
        self._await_supervisor_ack = await_supervisor_ack
        self._litellm_deployments = litellm_deployments

    def _require_supervisor_ack(self, marker: ActivationMarker) -> None:
        if self._await_supervisor_ack is None:
            return
        try:
            self._await_supervisor_ack(marker)
        except RouteRuntimeError:
            raise
        except Exception as error:
            raise RouteRuntimeError(
                "live LiteLLM supervisor acknowledgement is unavailable"
            ) from error

    def _read_update_boundary(self) -> str | None:
        path = self._root / ".update-boundary.json"
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise RouteRuntimeError("route update boundary is unsafe")
        try:
            content = path.read_bytes()
            raw: Any = json.loads(content)
        except (OSError, json.JSONDecodeError) as error:
            raise RouteRuntimeError("route update boundary is unreadable") from error
        if (
            len(content) > 256
            or not isinstance(raw, dict)
            or set(raw) != _UPDATE_BOUNDARY_FIELDS
            or raw.get("schema_version") != 1
            or not isinstance(raw.get("key"), str)
            or _DIGEST.fullmatch(raw["key"]) is None
            or content != _encoded(raw)
        ):
            raise RouteRuntimeError("route update boundary is invalid")
        return raw["key"]

    def _require_update_boundary(
        self,
        key: str | None,
    ) -> None:
        active = self._read_update_boundary()
        if active is None:
            if key is not None:
                raise RouteRuntimeError("route update boundary is not active")
            return
        if key != active:
            raise RouteRuntimeError("route publication is fenced by an update boundary")

    def claim_update_boundary(self, key: str) -> None:
        """Fence normal publication behind one durable update boundary key."""

        if _DIGEST.fullmatch(key) is None:
            raise RouteRuntimeError("route update boundary key is invalid")
        with self._locked():
            active = self._read_update_boundary()
            if active is not None:
                if active != key:
                    raise RouteRuntimeError(
                        "route publication belongs to a different update boundary"
                    )
                return
            self._atomic_write(
                self._root / ".update-boundary.json",
                _encoded({"key": key, "schema_version": 1}),
            )

    def inspect_update_boundary(self) -> str | None:
        """Return the validated active update fence, if one exists."""

        with self._locked():
            return self._read_update_boundary()

    def release_update_boundary(self, key: str) -> None:
        """Release only the exact durable update boundary key."""

        if _DIGEST.fullmatch(key) is None:
            raise RouteRuntimeError("route update boundary key is invalid")
        with self._locked():
            active = self._read_update_boundary()
            if active != key:
                raise RouteRuntimeError(
                    "route update boundary key does not own publication"
                )
            path = self._root / ".update-boundary.json"
            try:
                path.unlink()
                directory = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except OSError as error:
                raise RouteRuntimeError(
                    "route update boundary release is unavailable"
                ) from error

    @staticmethod
    def _valid_json_mapping(content: bytes) -> bool:
        try:
            return isinstance(json.loads(content), dict)
        except (TypeError, json.JSONDecodeError):
            return False

    @staticmethod
    def _valid_litellm(content: bytes) -> bool:
        try:
            document = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return False
        return isinstance(document, dict) and isinstance(
            document.get("model_list"), list
        )

    @staticmethod
    def empty_litellm() -> bytes:
        return _encoded(
            {
                "general_settings": {
                    "database_url": "os.environ/LITELLM_DATABASE_URL",
                    "disable_admin_ui": False,
                    "master_key": "os.environ/LITELLM_MASTER_KEY",
                    "store_model_in_db": False,
                },
                "litellm_settings": {
                    "drop_params": True,
                    "failure_callback": [],
                    "set_verbose": False,
                    "success_callback": [],
                },
                "model_list": [],
                "router_settings": {
                    "enable_pre_call_checks": True,
                    "routing_strategy": "simple-shuffle",
                },
            }
        )

    def _current_generation(self) -> int:
        marker = self._read_marker(
            optional=True, verify_files=False, verify_lease=False
        )
        return marker.generation if marker is not None else 0

    def _lease(self, expires_at: datetime) -> tuple[datetime, datetime]:
        issued = _aware(self._clock(), "route clock")
        expires = _aware(expires_at, "route lease expiry")
        if expires <= issued or expires - issued > self._maximum_lease:
            raise RouteRuntimeError(
                "route lease is invalid or exceeds its configured bound"
            )
        return issued, expires

    @staticmethod
    def _identity(
        reconciliation_id: str, plan_digest: str, evidence_digest: str
    ) -> None:
        try:
            parsed = uuid.UUID(reconciliation_id)
        except (TypeError, ValueError, AttributeError) as error:
            raise RouteRuntimeError("reconciliation ID is invalid") from error
        if str(parsed) != reconciliation_id:
            raise RouteRuntimeError("reconciliation ID is not canonical")
        if (
            _DIGEST.fullmatch(plan_digest) is None
            or _DIGEST.fullmatch(evidence_digest) is None
        ):
            raise RouteRuntimeError("publication digest identity is invalid")

    def _render_routes(
        self,
        generation: int,
        request: RouteBundleRequest,
        now: datetime,
        expires: datetime,
    ) -> tuple[bytes, bytes]:
        if not request.routes:
            raise RouteRuntimeError("published routes must not be empty")
        exact_endpoints: set[str] = set()
        rendered_routes: dict[str, object] = {}
        models: list[dict[str, object]] = []
        published_routes: list[PublishedRoute] = []
        for alias, raw in sorted(request.routes.items()):
            if not isinstance(alias, str) or _IDENTIFIER.fullmatch(alias) is None:
                raise RouteRuntimeError("route alias is invalid")
            if not isinstance(raw, Mapping) or set(raw) != _ROUTE_FIELDS:
                raise RouteRuntimeError("route fields do not match the resolved plan")
            workload_id = raw.get("workload_id")
            nodes = raw.get("nodes")
            node_id = raw.get("entrypoint_node_id")
            scheme = raw.get("scheme")
            port = raw.get("port")
            path = raw.get("path")
            quota = raw.get("quota")
            quota_digest = raw.get("quota_digest")
            if (
                not isinstance(workload_id, str)
                or _IDENTIFIER.fullmatch(workload_id) is None
                or not isinstance(nodes, (list, tuple))
                or not nodes
                or len(nodes) != len(set(nodes))
                or any(
                    not isinstance(node, str) or _NODE.fullmatch(node) is None
                    for node in nodes
                )
                or not isinstance(node_id, str)
                or node_id not in nodes
                or _NODE.fullmatch(node_id) is None
            ):
                raise RouteRuntimeError("route entrypoint is invalid")
            evidence = request.endpoints.get(node_id)
            if evidence is None or evidence.node_id != node_id:
                raise RouteRuntimeError("route endpoint evidence is unavailable")
            expected_operation = f"{workload_id}:{node_id}:workload.verify"
            if (
                _OPERATION.fullmatch(evidence.operation_id) is None
                or evidence.operation_id != expected_operation
                or _DIGEST.fullmatch(evidence.verify_evidence_digest) is None
                or _DIGEST.fullmatch(evidence.evidence_digest) is None
            ):
                raise RouteRuntimeError(
                    "route endpoint evidence is not exact verify evidence"
                )
            observed = _aware(evidence.observed_at, "endpoint evidence timestamp")
            if observed > now or now - observed > self._maximum_lease:
                raise RouteRuntimeError(
                    "route endpoint evidence is stale or in the future"
                )
            try:
                address = self._policy.validate(evidence.address)
            except PresenceError as error:
                raise RouteRuntimeError(
                    f"management address evidence is invalid: {error}"
                ) from error
            expected_endpoint_digest = endpoint_evidence_digest(
                node_id=node_id,
                address=address,
                observed_at=observed,
                operation_id=evidence.operation_id,
                verify_evidence_digest=evidence.verify_evidence_digest,
            )
            if evidence.evidence_digest != expected_endpoint_digest:
                raise RouteRuntimeError("endpoint evidence binding is invalid")
            if expires > observed + self._maximum_lease:
                raise RouteRuntimeError("route lease exceeds endpoint freshness")
            if (
                scheme not in {"http", "https"}
                or isinstance(port, bool)
                or not isinstance(port, int)
                or not 1 <= port <= 65535
                or not isinstance(path, str)
                or not path.startswith("/")
                or "?" in path
                or "#" in path
                or "//" in path
                or "/../" in f"{path}/"
            ):
                raise RouteRuntimeError("route structured endpoint is invalid")
            if not isinstance(quota, Mapping) or set(quota) != _QUOTA_FIELDS:
                raise RouteRuntimeError("route quota is invalid")
            rpm = quota.get("requests_per_minute")
            tpm = quota.get("tokens_per_minute")
            if (
                isinstance(rpm, bool)
                or not isinstance(rpm, int)
                or isinstance(tpm, bool)
                or not isinstance(tpm, int)
                or not 1 <= rpm <= 100_000
                or not 1 <= tpm <= 100_000_000
                or _DIGEST.fullmatch(quota_digest) is None
                or _canonical_sha256(dict(quota)) != quota_digest
            ):
                raise RouteRuntimeError("route quota or quota digest is invalid")
            published_route = build_published_route(alias, raw, address)
            base = published_route.api_base
            exact_endpoints.add(node_id)
            rendered_routes[alias] = {
                "address": address,
                "evidence_digest": evidence.evidence_digest,
                "node_id": node_id,
                "observed_at": observed.isoformat(),
                "operation_id": evidence.operation_id,
                "path": path,
                "port": port,
                "scheme": scheme,
                "verify_evidence_digest": evidence.verify_evidence_digest,
            }
            models.append(
                {
                    "model_name": alias,
                    "litellm_params": {
                        "api_base": base,
                        "api_key": "os.environ/LITELLM_UPSTREAM_KEY",
                        "model": f"openai/{alias}",
                        "rpm": rpm,
                        "tpm": tpm,
                    },
                }
            )
            published_routes.append(published_route)
        if set(request.endpoints) != exact_endpoints:
            raise RouteRuntimeError(
                "endpoint evidence must exactly cover route entrypoints"
            )
        route_content = _encoded(
            {
                "generation": generation,
                "routes": rendered_routes,
                "schema_version": 1,
                "state": "published",
            }
        )
        litellm_document = json.loads(self.empty_litellm())
        if self._litellm_deployments is not None:
            if _COMMIT.fullmatch(request.base_commit) is None:
                raise RouteRuntimeError("Hermes repository commit is invalid")
            try:
                deployments = tuple(
                    self._litellm_deployments(
                        request.base_commit,
                        tuple(published_routes),
                    )
                )
                if (
                    any(
                        not isinstance(deployment, LiteLlmDeployment)
                        for deployment in deployments
                    )
                    or len({item.priority for item in deployments}) != len(deployments)
                    or len({item.workload for item in deployments}) != len(deployments)
                ):
                    raise ValueError("Hermes deployments are invalid")
                for deployment in sorted(
                    deployments,
                    key=lambda item: item.priority,
                ):
                    LiteLlmPublisher._validate_hermes_deployment(deployment)
                    models.append(
                        {
                            "model_name": deployment.model_name,
                            "litellm_params": {
                                "api_base": deployment.api_base,
                                "api_key": "os.environ/LITELLM_UPSTREAM_KEY",
                                "model": f"openai/{deployment.workload}",
                                "order": deployment.priority,
                                "rpm": deployment.requests_per_minute,
                                "tpm": deployment.tokens_per_minute,
                            },
                        }
                    )
            except RouteRuntimeError:
                raise
            except Exception as error:
                raise RouteRuntimeError(
                    "Hermes repository policy is invalid"
                ) from error
            if deployments:
                litellm_document["router_settings"].update(
                    {
                        "allowed_fails": 0,
                        "num_retries": 1,
                        "retry_policy": {
                            "AuthenticationErrorRetries": 0,
                            "BadRequestErrorRetries": 0,
                            "ContentPolicyViolationErrorRetries": 0,
                            "RateLimitErrorRetries": 1,
                            "TimeoutErrorRetries": 1,
                        },
                    }
                )
        litellm_document["model_list"] = models
        return route_content, _encoded(litellm_document)

    def publish(
        self,
        request: RouteBundleRequest,
        *,
        update_boundary_key: str | None = None,
        renew_update_boundary: bool = False,
        expected_current_digest: str | None = None,
    ) -> ActivationMarker:
        if not isinstance(renew_update_boundary, bool):
            raise RouteRuntimeError("route update renewal flag is invalid")
        if renew_update_boundary and update_boundary_key is None:
            raise RouteRuntimeError("route update renewal requires its exact fence")
        if (
            expected_current_digest is not None
            and _DIGEST.fullmatch(expected_current_digest) is None
        ):
            raise RouteRuntimeError(
                "route publication compare-and-swap digest is invalid"
            )
        self._identity(
            request.reconciliation_id,
            request.plan_digest,
            request.evidence_set_digest,
        )
        with self._locked():
            self._require_update_boundary(update_boundary_key)
            issued, expires = self._lease(request.expires_at)
            current = self._read_marker(
                optional=True,
                verify_files=True,
                verify_lease=False,
            )
            generation = current.generation if current is not None else 1
            routes, litellm = self._render_routes(generation, request, issued, expires)
            if (
                not renew_update_boundary
                and current is not None
                and current.state == "published"
                and current.reconciliation_id == request.reconciliation_id
                and current.plan_digest == request.plan_digest
                and current.evidence_set_digest == request.evidence_set_digest
                and current.routes_sha256 == _sha256(routes)
                and current.litellm_sha256 == _sha256(litellm)
                and _parse_time(current.expires_at, "expiry timestamp") > issued
            ):
                self._require_supervisor_ack(current)
                return current
            if expected_current_digest is not None and (
                current is None or current.digest != expected_current_digest
            ):
                raise RouteRuntimeError("route publication compare-and-swap failed")
            generation = (current.generation if current is not None else 0) + 1
            if current is not None:
                routes, litellm = self._render_routes(
                    generation, request, issued, expires
                )
            marker = self._activate(
                generation=generation,
                state="published",
                reconciliation_id=request.reconciliation_id,
                plan_digest=request.plan_digest,
                evidence_set_digest=request.evidence_set_digest,
                routes=routes,
                litellm=litellm,
                issued=issued,
                expires=expires,
            )
            self._require_supervisor_ack(marker)
            return marker

    def publish_compiled(
        self,
        *,
        authority_id: str,
        plan_digest: str,
        evidence_set_digest: str,
        routes: bytes,
        litellm: bytes,
        expires_at: datetime,
        state: str = "published",
    ) -> ActivationMarker:
        """Activate a complete controller-validated database recipe bundle.

        This is the Git-independent counterpart of ``publish``. Callers must
        compile typed recipe state first; the same lock, validators, immutable
        generation directory, atomic marker, and supervisor acknowledgement
        remain mandatory.
        """
        self._identity(authority_id, plan_digest, evidence_set_digest)
        if state not in {"published", "maintenance"}:
            raise RouteRuntimeError("compiled route state is invalid")
        with self._locked():
            self._require_update_boundary(None)
            issued, expires = self._lease(expires_at)
            current = self._read_marker(
                optional=True, verify_files=True, verify_lease=False
            )
            generation = (current.generation if current is not None else 0) + 1
            marker = self._activate(
                generation=generation,
                state=state,
                reconciliation_id=authority_id,
                plan_digest=plan_digest,
                evidence_set_digest=evidence_set_digest,
                routes=routes,
                litellm=litellm,
                issued=issued,
                expires=expires,
            )
            self._require_supervisor_ack(marker)
            return marker

    def withdraw(
        self,
        *,
        reconciliation_id: str,
        plan_digest: str,
        targets: tuple[str, ...],
        reason: str,
        update_boundary_key: str | None = None,
        renew_update_boundary: bool = False,
        expected_current_digest: str | None = None,
    ) -> ActivationMarker:
        if not isinstance(renew_update_boundary, bool):
            raise RouteRuntimeError("route update renewal flag is invalid")
        if renew_update_boundary and update_boundary_key is None:
            raise RouteRuntimeError("route update renewal requires its exact fence")
        if (
            expected_current_digest is not None
            and _DIGEST.fullmatch(expected_current_digest) is None
        ):
            raise RouteRuntimeError(
                "route publication compare-and-swap digest is invalid"
            )
        self._identity(reconciliation_id, plan_digest, "0" * 64)
        if (
            not targets
            or len(targets) != len(set(targets))
            or any(_NODE.fullmatch(target) is None for target in targets)
        ):
            raise RouteRuntimeError("maintenance targets are invalid")
        safe_reason = re.sub(
            r"(?i)(bearer|token|secret|password)[^\s]*",
            "<redacted>",
            reason,
        )[:256]
        with self._locked():
            self._require_update_boundary(update_boundary_key)
            issued = _aware(self._clock(), "route clock")
            expires = issued + self._maximum_lease
            current = self._read_marker(
                optional=True,
                verify_files=True,
                verify_lease=False,
            )
            generation = current.generation if current is not None else 1

            def maintenance_routes(number: int) -> bytes:
                return _encoded(
                    {
                        "generation": number,
                        "reason": safe_reason or "maintenance",
                        "routes": {},
                        "schema_version": 1,
                        "state": "maintenance",
                        "targets": sorted(targets),
                    }
                )

            routes = maintenance_routes(generation)
            empty = self.empty_litellm()
            if (
                not renew_update_boundary
                and current is not None
                and current.state == "maintenance"
                and current.reconciliation_id == reconciliation_id
                and current.plan_digest == plan_digest
                and current.routes_sha256 == _sha256(routes)
                and current.litellm_sha256 == _sha256(empty)
                and _parse_time(current.expires_at, "expiry timestamp") > issued
            ):
                self._require_supervisor_ack(current)
                return current
            if expected_current_digest is not None and (
                current is None or current.digest != expected_current_digest
            ):
                raise RouteRuntimeError("route publication compare-and-swap failed")
            generation = (current.generation if current is not None else 0) + 1
            routes = maintenance_routes(generation)
            marker = self._activate(
                generation=generation,
                state="maintenance",
                reconciliation_id=reconciliation_id,
                plan_digest=plan_digest,
                evidence_set_digest="0" * 64,
                routes=routes,
                litellm=empty,
                issued=issued,
                expires=expires,
            )
            self._require_supervisor_ack(marker)
            return marker

    @contextmanager
    def _locked(self):
        try:
            import fcntl

            path = self._root / ".publication.lock"
            descriptor = os.open(
                path,
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RouteRuntimeError("route publication lock is unsafe")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except RouteRuntimeError:
            raise
        except Exception as error:
            raise RouteRuntimeError("route publication lock is unavailable") from error
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _activate(
        self,
        *,
        generation: int,
        state: str,
        reconciliation_id: str,
        plan_digest: str,
        evidence_set_digest: str,
        routes: bytes,
        litellm: bytes,
        issued: datetime,
        expires: datetime,
    ) -> ActivationMarker:
        if self._validate_routes(routes) is not True:
            raise RouteRuntimeError("route validation rejected the staged bundle")
        if self._validate_litellm(litellm) is not True:
            raise RouteRuntimeError("LiteLLM validation rejected the staged bundle")
        manifest_document: dict[str, object] = {
            "schema_version": 1,
            "generation": generation,
            "state": state,
            "reconciliation_id": reconciliation_id,
            "plan_digest": plan_digest,
            "evidence_set_digest": evidence_set_digest,
            "routes_sha256": _sha256(routes),
            "litellm_sha256": _sha256(litellm),
            "issued_at": issued.isoformat(),
            "expires_at": expires.isoformat(),
        }
        manifest = _encoded(manifest_document)
        manifest_digest = _sha256(manifest)
        directory_name = f"{generation:08d}-{manifest_digest}"
        directory = self._generations / directory_name
        try:
            self._stage(directory, "routes.json", routes)
            self._stage(directory, "litellm.json", litellm)
            self._stage(directory, "manifest.json", manifest)
        except RouteRuntimeError:
            raise
        except Exception as error:
            raise RouteRuntimeError(
                "route bundle apply failed; previous activation retained"
            ) from error
        activation_document = {
            **manifest_document,
            "directory": directory_name,
            "manifest_sha256": manifest_digest,
        }
        marker = ActivationMarker(**activation_document)  # type: ignore[arg-type]
        try:
            self._atomic_write(
                self._root / "activation.json",
                _encoded(activation_document),
                mode=0o640,
            )
        except Exception as error:
            raise RouteRuntimeError(
                "route bundle activation failed; previous activation retained"
            ) from error
        return marker

    @staticmethod
    def _atomic_write(target: Path, content: bytes, *, mode: int = 0o600) -> None:
        if target.is_symlink() or target.parent.is_symlink():
            raise RouteRuntimeError("route runtime target must not be a symlink")
        descriptor, temporary_raw = tempfile.mkstemp(
            prefix=f".{target.name}-", dir=target.parent
        )
        temporary = Path(temporary_raw)
        try:
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def _stage(self, directory: Path, name: str, content: bytes) -> None:
        if directory.exists():
            if directory.is_symlink() or not directory.is_dir():
                raise RouteRuntimeError("staged route generation is unsafe")
        else:
            directory.mkdir(mode=0o750)
        directory.chmod(0o750)
        target = directory / name
        if target.exists():
            if (
                target.is_symlink()
                or not target.is_file()
                or target.read_bytes() != content
            ):
                raise RouteRuntimeError(
                    "staged route generation conflicts with existing bytes"
                )
            return
        self._atomic_write(target, content, mode=0o640)

    def inspect(
        self,
        *,
        expected: ActivationMarker | None = None,
        verify_lease: bool = True,
    ) -> ActivationMarker:
        if not isinstance(verify_lease, bool):
            raise RouteRuntimeError("route inspection lease flag is invalid")
        marker = self._read_marker(
            optional=False,
            verify_files=True,
            verify_lease=verify_lease,
        )
        assert marker is not None
        if expected is not None and marker != expected:
            raise RouteRuntimeError(
                "active route marker does not match expected publication"
            )
        return marker

    def _read_marker(
        self,
        *,
        optional: bool,
        verify_files: bool,
        verify_lease: bool,
    ) -> ActivationMarker | None:
        bundle = _read_active_route_bundle(
            self._root,
            generations=self._generations,
            clock=self._clock,
            maximum_lease=self._maximum_lease,
            optional=optional,
            verify_files=verify_files,
            verify_lease=verify_lease,
            validate_documents=False,
            validate_routes=self._validate_routes,
            validate_litellm=self._validate_litellm,
        )
        return None if bundle is None else bundle.marker

    @staticmethod
    def _validate_marker(marker: ActivationMarker) -> None:
        if (
            marker.schema_version != 1
            or isinstance(marker.generation, bool)
            or not isinstance(marker.generation, int)
            or marker.generation <= 0
            or marker.state not in {"maintenance", "published"}
            or _DIRECTORY.fullmatch(marker.directory) is None
            or marker.directory != f"{marker.generation:08d}-{marker.manifest_sha256}"
            or any(
                _DIGEST.fullmatch(value) is None
                for value in (
                    marker.plan_digest,
                    marker.evidence_set_digest,
                    marker.routes_sha256,
                    marker.litellm_sha256,
                    marker.manifest_sha256,
                )
            )
        ):
            raise RouteRuntimeError("route activation marker identity is invalid")
        AtomicRouteBundlePublisher._identity(
            marker.reconciliation_id,
            marker.plan_digest,
            marker.evidence_set_digest,
        )


def verify_active_route_bundle(
    root: Path,
    *,
    clock: Callable[[], datetime],
    maximum_lease_seconds: int = 300,
) -> VerifiedRouteBundle:
    """Read and authenticate the complete active bundle without mutating it."""

    if root.is_symlink() or not root.is_dir():
        raise RouteRuntimeError("route runtime root is unavailable")
    if not 1 <= maximum_lease_seconds <= 3600:
        raise RouteRuntimeError("route lease bound is invalid")
    generations = root / "generations"
    if generations.is_symlink() or not generations.is_dir():
        raise RouteRuntimeError("route generation root is unavailable")
    bundle = _read_active_route_bundle(
        root,
        generations=generations,
        clock=clock,
        maximum_lease=timedelta(seconds=maximum_lease_seconds),
        optional=False,
        verify_files=True,
        verify_lease=True,
        validate_documents=True,
        validate_routes=AtomicRouteBundlePublisher._valid_json_mapping,
        validate_litellm=AtomicRouteBundlePublisher._valid_litellm,
    )
    assert bundle is not None
    return bundle


def _read_active_route_bundle(
    root: Path,
    *,
    generations: Path,
    clock: Callable[[], datetime],
    maximum_lease: timedelta,
    optional: bool,
    verify_files: bool,
    verify_lease: bool,
    validate_documents: bool,
    validate_routes: Callable[[bytes], bool],
    validate_litellm: Callable[[bytes], bool],
) -> VerifiedRouteBundle | None:
    active = root / "activation.json"
    if not active.exists():
        if optional:
            return None
        raise RouteRuntimeError("no route bundle is active")
    if active.is_symlink() or not active.is_file():
        raise RouteRuntimeError("route activation marker is unsafe")
    try:
        marker_content = active.read_bytes()
        raw: Any = json.loads(marker_content)
    except (OSError, json.JSONDecodeError) as error:
        raise RouteRuntimeError("route activation marker is unreadable") from error
    if not isinstance(raw, dict) or set(raw) != _MARKER_FIELDS:
        raise RouteRuntimeError("route activation marker fields are invalid")
    try:
        marker = ActivationMarker(**raw)
    except TypeError as error:
        raise RouteRuntimeError("route activation marker fields are invalid") from error
    AtomicRouteBundlePublisher._validate_marker(marker)
    if marker_content != marker.canonical_bytes():
        raise RouteRuntimeError("route activation marker is not canonical")

    documents: dict[str, Mapping[str, object]] = {}
    if verify_files:
        directory = generations / marker.directory
        if directory.is_symlink() or not directory.is_dir():
            raise RouteRuntimeError("active route generation is unavailable")
        manifest_document = {
            field: getattr(marker, field)
            for field in (
                "schema_version",
                "generation",
                "state",
                "reconciliation_id",
                "plan_digest",
                "evidence_set_digest",
                "routes_sha256",
                "litellm_sha256",
                "issued_at",
                "expires_at",
            )
        }
        expected_files = {
            "manifest.json": (
                marker.manifest_sha256,
                _encoded(manifest_document),
            ),
            "routes.json": (marker.routes_sha256, None),
            "litellm.json": (marker.litellm_sha256, None),
        }
        for name, (digest, exact) in expected_files.items():
            target = directory / name
            if target.is_symlink() or not target.is_file():
                raise RouteRuntimeError("active route generation file is unsafe")
            try:
                content = target.read_bytes()
            except OSError as error:
                raise RouteRuntimeError(
                    "active route generation file is unreadable"
                ) from error
            if _sha256(content) != digest or (exact is not None and content != exact):
                raise RouteRuntimeError("active route generation checksum mismatch")
            if name in {"routes.json", "litellm.json"}:
                validator = (
                    validate_routes if name == "routes.json" else validate_litellm
                )
                try:
                    document = json.loads(content)
                except json.JSONDecodeError as error:
                    raise RouteRuntimeError(
                        "active route generation document is invalid"
                    ) from error
                if (
                    not isinstance(document, Mapping)
                    or validate_documents
                    and not validator(content)
                ):
                    raise RouteRuntimeError(
                        "active route generation document is invalid"
                    )
                documents[name] = document

    if verify_lease:
        now = _aware(clock(), "route clock")
        issued = _parse_time(marker.issued_at, "issued timestamp")
        expires = _parse_time(marker.expires_at, "expiry timestamp")
        if (
            issued > now
            or now >= expires
            or expires <= issued
            or expires - issued > maximum_lease
        ):
            raise RouteRuntimeError("active route lease is invalid or expired")
    return VerifiedRouteBundle(
        marker=marker,
        routes=documents.get("routes.json", {}),
        litellm=documents.get("litellm.json", {}),
    )
