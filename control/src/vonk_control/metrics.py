"""Stable-cardinality, content-free OpenMetrics exporter."""

from __future__ import annotations

import math
import re
import threading
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Job,
)

_NODE = re.compile(r"spk_[0-9a-f]{32}")
_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_JOB_KINDS = frozenset({"install", "probe", "reconcile", "deploy", "backup", "restore"})
_JOB_STATES = frozenset({"queued", "running", "waiting-for-operator", "succeeded", "failed", "expired"})
_ROUTE_STATES = frozenset({"published", "maintenance", "unavailable"})
_AGENT_STATES = frozenset({"active", "retired"})
_AGENT_OPERATIONS = frozenset({
    "node.probe",
    "release.install",
    "workload.prepare",
    "workload.start",
    "workload.stop",
    "workload.health",
    "workload.verify",
})
_VERSION_BUCKETS = frozenset({"supported", "old", "new", "incompatible"})
_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def protocol_version_bucket(
    version: int | None,
    *,
    minimum: int = 1,
    maximum: int = 1,
) -> str:
    if minimum < 1 or maximum < minimum:
        raise ValueError("supported protocol range is invalid")
    if version is None or isinstance(version, bool) or not isinstance(version, int):
        return "incompatible"
    if version < 0:
        return "incompatible"
    if version < minimum:
        return "old"
    if version > maximum:
        return "new"
    return "supported"


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._nodes: dict[str, tuple[bool, int, int, float | None]] = {}
        self._jobs: dict[tuple[str, str], int] = {}
        self._route_state = "unavailable"
        self._backup_age: float | None = None
        self._api_counts: dict[tuple[str, str], int] = defaultdict(int)
        self._api_durations: dict[tuple[str, str], list[float]] = defaultdict(list)
        self._agent_nodes: dict[str, tuple[str, str, float | None, float | None]] = {}
        self._agent_operations: dict[tuple[str, str], int] = {}
        self._agent_leases: dict[tuple[str, str], float] = {}
        self._agent_rollouts: dict[str, int] = {}

    @staticmethod
    def _number(value: float, field: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or not math.isfinite(value):
            raise ValueError(f"{field} must be a nonnegative finite number")
        return float(value)

    def update_node(self, node_id: str, *, ready: bool, memory_available_bytes: int, disk_available_bytes: int, probe_age_seconds: float | None) -> None:
        if _NODE.fullmatch(node_id) is None:
            raise ValueError("metrics node ID must be a stable generated ID")
        if not isinstance(ready, bool):
            raise TypeError("node readiness must be boolean")
        memory = int(self._number(memory_available_bytes, "memory"))
        disk = int(self._number(disk_available_bytes, "disk"))
        age = (
            None
            if probe_age_seconds is None
            else self._number(probe_age_seconds, "probe age")
        )
        with self._lock:
            self._nodes[node_id] = (ready, memory, disk, age)

    def set_job_count(self, kind: str, state: str, count: int) -> None:
        safe_kind = kind if kind in _JOB_KINDS else "other"
        safe_state = state if state in _JOB_STATES else "other"
        bounded = int(self._number(count, "job count"))
        with self._lock:
            self._jobs[(safe_kind, safe_state)] = bounded

    def set_route_state(self, state: str) -> None:
        if state not in _ROUTE_STATES:
            raise ValueError("route metric state is invalid")
        with self._lock:
            self._route_state = state

    def set_backup_age(self, age_seconds: float) -> None:
        age = self._number(age_seconds, "backup age")
        with self._lock:
            self._backup_age = age

    def observe_api(self, method: str, status_code: int, duration_seconds: float) -> None:
        safe_method = method if method in _METHODS else "OTHER"
        status_class = f"{status_code // 100}xx" if 100 <= status_code <= 599 else "other"
        duration = self._number(duration_seconds, "API duration")
        with self._lock:
            key = (safe_method, status_class)
            self._api_counts[key] += 1
            self._api_durations[key].append(duration)

    def _replace_agent_snapshot(
        self,
        *,
        nodes: dict[str, tuple[str, str, float | None, float | None]],
        operations: dict[tuple[str, str], int],
        leases: dict[tuple[str, str], float],
        rollouts: dict[str, int],
    ) -> None:
        safe_nodes = {}
        for node_id, (state, version_bucket, last_seen_age, certificate_expiry) in nodes.items():
            if _NODE.fullmatch(node_id) is None:
                raise ValueError("metrics node ID must be a stable generated ID")
            safe_state = state if state in _AGENT_STATES else "other"
            safe_version = version_bucket if version_bucket in _VERSION_BUCKETS else "incompatible"
            safe_nodes[node_id] = (
                safe_state,
                safe_version,
                None if last_seen_age is None else self._number(last_seen_age, "last-seen age"),
                None if certificate_expiry is None else self._number(certificate_expiry, "certificate expiry"),
            )
        safe_operations: dict[tuple[str, str], int] = defaultdict(int)
        for (operation, state), count in operations.items():
            safe_operations[
                (
                    operation if operation in _AGENT_OPERATIONS else "other",
                    state if state in _JOB_STATES else "other",
                )
            ] += int(self._number(count, "operation count"))
        safe_leases: dict[tuple[str, str], float] = {}
        for (node_id, operation), age in leases.items():
            if _NODE.fullmatch(node_id) is None:
                raise ValueError("metrics node ID must be a stable generated ID")
            safe_key = (
                node_id,
                operation if operation in _AGENT_OPERATIONS else "other",
            )
            safe_age = self._number(age, "operation lease age")
            safe_leases[safe_key] = max(safe_leases.get(safe_key, 0.0), safe_age)
        safe_rollouts: dict[str, int] = defaultdict(int)
        for state, count in rollouts.items():
            safe_rollouts[state if state in _JOB_STATES else "other"] += int(
                self._number(count, "rollout count")
            )
        with self._lock:
            self._agent_nodes = safe_nodes
            self._agent_operations = dict(safe_operations)
            self._agent_leases = safe_leases
            self._agent_rollouts = dict(safe_rollouts)

    def render(self) -> str:
        with self._lock:
            nodes, jobs = dict(self._nodes), dict(self._jobs)
            route_state = self._route_state
            backup_age = self._backup_age
            api_counts = dict(self._api_counts)
            api_durations = {key: tuple(values) for key, values in self._api_durations.items()}
            agent_nodes = dict(self._agent_nodes)
            agent_operations = dict(self._agent_operations)
            agent_leases = dict(self._agent_leases)
            agent_rollouts = dict(self._agent_rollouts)
        lines = [
            "# HELP vonk_route_state Current inference route state.",
            "# TYPE vonk_route_state gauge",
        ]
        for state in sorted(_ROUTE_STATES):
            lines.append(f'vonk_route_state{{state="{state}"}} {1 if state == route_state else 0}')
        if backup_age is not None:
            lines.extend((
                "# HELP vonk_control_backup_age_seconds Age of the last successful encrypted control backup.",
                "# TYPE vonk_control_backup_age_seconds gauge",
                f"vonk_control_backup_age_seconds {backup_age:g}",
            ))
        lines.extend(("# HELP vonk_node_ready Whether the stable fleet node is ready.", "# TYPE vonk_node_ready gauge"))
        for node_id, (ready, memory, disk, age) in sorted(nodes.items()):
            label = f'node_id="{node_id}"'
            lines.extend((
                f"vonk_node_ready{{{label}}} {1 if ready else 0}",
                f"vonk_node_memory_available_bytes{{{label}}} {memory}",
                f"vonk_node_disk_available_bytes{{{label}}} {disk}",
            ))
            if age is not None:
                lines.append(f"vonk_node_probe_age_seconds{{{label}}} {age:g}")
        lines.extend(("# HELP vonk_jobs Number of control jobs by bounded kind and state.", "# TYPE vonk_jobs gauge"))
        for (kind, state), count in sorted(jobs.items()):
            lines.append(f'vonk_jobs{{kind="{kind}",state="{state}"}} {count}')
        lines.extend((
            "# HELP vonk_agent_state Current durable outbound-agent lifecycle state.",
            "# TYPE vonk_agent_state gauge",
            "# HELP vonk_agent_version_compatibility Agent protocol compatibility bucket.",
            "# TYPE vonk_agent_version_compatibility gauge",
            "# HELP vonk_agent_last_seen_age_seconds Age of the latest authenticated agent contact.",
            "# TYPE vonk_agent_last_seen_age_seconds gauge",
            "# HELP vonk_agent_certificate_expiry_seconds Seconds until the active agent certificate expires.",
            "# TYPE vonk_agent_certificate_expiry_seconds gauge",
        ))
        for node_id, (state, version_bucket, last_seen_age, certificate_expiry) in sorted(agent_nodes.items()):
            label = f'node_id="{node_id}"'
            lines.append(f'vonk_agent_state{{{label},state="{state}"}} 1')
            lines.append(
                f'vonk_agent_version_compatibility{{{label},version_bucket="{version_bucket}"}} 1'
            )
            if last_seen_age is not None:
                lines.append(f"vonk_agent_last_seen_age_seconds{{{label}}} {last_seen_age:g}")
            if certificate_expiry is not None:
                lines.append(
                    f"vonk_agent_certificate_expiry_seconds{{{label}}} {certificate_expiry:g}"
                )
        lines.extend((
            "# HELP vonk_agent_operations Durable agent operations by bounded kind and state.",
            "# TYPE vonk_agent_operations gauge",
        ))
        for (operation, state), count in sorted(agent_operations.items()):
            lines.append(
                f'vonk_agent_operations{{operation="{operation}",state="{state}"}} {count}'
            )
        lines.extend((
            "# HELP vonk_agent_operation_lease_age_seconds Age since the active operation lease was last updated.",
            "# TYPE vonk_agent_operation_lease_age_seconds gauge",
        ))
        for (node_id, operation), age in sorted(agent_leases.items()):
            lines.append(
                f'vonk_agent_operation_lease_age_seconds{{node_id="{node_id}",operation="{operation}"}} {age:g}'
            )
        lines.extend((
            "# HELP vonk_agent_rollouts Durable reconciliation jobs by bounded state.",
            "# TYPE vonk_agent_rollouts gauge",
        ))
        for state, count in sorted(agent_rollouts.items()):
            lines.append(f'vonk_agent_rollouts{{state="{state}"}} {count}')
        lines.extend(("# HELP vonk_api_requests_total API responses by method and status class.", "# TYPE vonk_api_requests_total counter"))
        for (method, status_class), count in sorted(api_counts.items()):
            labels = f'method="{method}",status_class="{status_class}"'
            lines.append(f"vonk_api_requests_total{{{labels}}} {count}")
            values = api_durations[(method, status_class)]
            cumulative = 0
            for bucket in _BUCKETS:
                cumulative = sum(value <= bucket for value in values)
                lines.append(f'vonk_api_request_duration_seconds_bucket{{{labels},le="{bucket:g}"}} {cumulative}')
            lines.append(f'vonk_api_request_duration_seconds_bucket{{{labels},le="+Inf"}} {len(values)}')
            lines.append(f"vonk_api_request_duration_seconds_sum{{{labels}}} {sum(values):g}")
            lines.append(f"vonk_api_request_duration_seconds_count{{{labels}}} {len(values)}")
        lines.append("# EOF")
        return "\n".join(lines) + "\n"


class OperationalMetricsCollector:
    """Project bounded metrics from existing durable control-plane state."""

    def __init__(
        self,
        registry: MetricsRegistry,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        protocol_minimum: int = 1,
        protocol_maximum: int = 1,
    ) -> None:
        if protocol_minimum < 1 or protocol_maximum < protocol_minimum:
            raise ValueError("supported protocol range is invalid")
        self._registry = registry
        self._sessions = sessions
        self._clock = clock
        self._protocol_minimum = protocol_minimum
        self._protocol_maximum = protocol_maximum

    def refresh(self) -> None:
        now = _aware(self._clock())
        with self._sessions() as session:
            agent_nodes = list(session.scalars(select(AgentNode).order_by(AgentNode.node_id)))
            certificates = list(
                session.scalars(
                    select(AgentCertificate)
                    .where(
                        AgentCertificate.state == "active",
                        AgentCertificate.revoked_at.is_(None),
                    )
                    .order_by(
                        AgentCertificate.node_id,
                        AgentCertificate.not_after.desc(),
                        AgentCertificate.generation.desc(),
                    )
                )
            )
            operation_rows = list(
                session.execute(
                    select(AgentOperation.kind, AgentOperation.state, func.count())
                    .group_by(AgentOperation.kind, AgentOperation.state)
                    .order_by(AgentOperation.kind, AgentOperation.state)
                )
            )
            lease_rows = list(
                session.execute(
                    select(AgentOperation.node_id, AgentOperation.kind, AgentOperation.updated_at)
                    .join(
                        AgentOperationAttempt,
                        (AgentOperationAttempt.operation_id == AgentOperation.id)
                        & (AgentOperationAttempt.attempt == AgentOperation.current_attempt),
                    )
                    .where(
                        AgentOperation.state == "running",
                        AgentOperationAttempt.state == "running",
                    )
                    .order_by(AgentOperation.node_id, AgentOperation.kind, AgentOperation.id)
                )
            )
            rollout_rows = list(
                session.execute(
                    select(Job.state, func.count())
                    .where(Job.kind == "reconcile")
                    .group_by(Job.state)
                    .order_by(Job.state)
                )
            )
        active_certificates: dict[str, AgentCertificate] = {}
        for certificate in certificates:
            active_certificates.setdefault(certificate.node_id, certificate)
        nodes = {}
        for node in agent_nodes:
            certificate = active_certificates.get(node.node_id)
            nodes[node.node_id] = (
                node.state,
                protocol_version_bucket(
                    node.protocol_version,
                    minimum=self._protocol_minimum,
                    maximum=self._protocol_maximum,
                ),
                None
                if node.last_seen_at is None
                else max(0.0, (now - _aware(node.last_seen_at)).total_seconds()),
                None
                if certificate is None
                else max(0.0, (_aware(certificate.not_after) - now).total_seconds()),
            )
        operations = {(kind, state): int(count) for kind, state, count in operation_rows}
        leases: dict[tuple[str, str], float] = {}
        for node_id, operation, updated_at in lease_rows:
            key = (node_id, operation)
            age = max(0.0, (now - _aware(updated_at)).total_seconds())
            leases[key] = max(leases.get(key, 0.0), age)
        rollouts = {state: int(count) for state, count in rollout_rows}
        self._registry._replace_agent_snapshot(
            nodes=nodes,
            operations=operations,
            leases=leases,
            rollouts=rollouts,
        )
