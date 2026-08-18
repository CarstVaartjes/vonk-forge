"""Crash-recovering lifecycle for the outbound Vonk Forge agent."""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import platform
import random
import re
import shutil
import signal
import stat
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from vonk_agent_protocol import AgentClaim, AgentDirective, AgentProgress, AgentResult
from vonk_agent_protocol.workload_packages import OciBundleMetadata

from cluster_profiles.update_trust import UpdateTrust

from .client import (
    AgentClient,
    AgentRuntimeIdentity,
    AgentTransportError,
    CredentialProvider,
    CredentialStore,
    EnrollmentPending,
    IssuedCredential,
)
from .config import DEFAULT_CONFIG_PATH, AgentConfig
from .deadlines import MonotonicDeadline
from .nvidia_tools import InstalledPolicy
from .oci import ORASClient, ORASPolicy
from .operations import OperationContext, OperationExecution, OperationRegistry
from .packages.backends import (
    Backend,
    BackendInvocation,
    MountPolicy,
    NetworkPolicy,
    PythonRuntimePolicy,
    ResourcePolicy,
)
from .probe import PinnedNodeProbe
from .readiness import ReadinessReporter
from .releases import ReleaseInstaller
from .runtime_policy import RuntimePolicy
from .state import AgentStateStore
from .update import (
    AgentUpdater,
    LocalSupervisor,
    ORASAgentTransport,
    PlatformAgentTrust,
    PlatformTUFRouteFetcher,
)
from .update_trust import BoundedHTTPSFetcher, TUFReleaseTrust
from .workloads import WorkloadOperations


class AgentControl(Protocol):
    def claim(self) -> AgentClaim | None: ...

    def heartbeat(
        self, progress: AgentProgress
    ) -> AgentDirective | AgentProgress: ...

    def result(self, result: AgentResult) -> None: ...

    def renew(self, csr: bytes) -> IssuedCredential: ...

    def activate(self, generation: int, credentials: CredentialProvider) -> None: ...


def _local_workload_os_identities() -> tuple[str, ...]:
    """Return bounded Linux distribution identities for workload compatibility."""

    identities: list[str] = ["linux"]
    try:
        release = platform.freedesktop_os_release()
    except (AttributeError, OSError):
        return tuple(identities)
    raw_ids = [release.get("ID", ""), *release.get("ID_LIKE", "").split()]
    version = release.get("VERSION_ID", "").strip().strip('"')
    for raw_id in raw_ids:
        identity = raw_id.strip().strip('"').lower()
        if re.fullmatch(r"[a-z0-9][a-z0-9._+-]{0,63}", identity) is None:
            continue
        identities.append(identity)
        if re.fullmatch(r"[a-z0-9][a-z0-9._+-]{0,63}", version):
            identities.append(f"{identity}-{version}")
    return tuple(dict.fromkeys(identities))


def _validate_workload_compatibility(
    lock: object,
    protocol_architecture: str,
    *,
    available_storage_bytes: int,
    operating_systems: tuple[str, ...] | None = None,
    driver_version: str | None = None,
    cuda_version: str | None = None,
    request: object | None = None,
) -> None:
    """Fail closed when a signed workload lock cannot run on this GPU node."""

    compatibility = getattr(lock, "compatibility", {})
    if not isinstance(compatibility, Mapping):
        raise TypeError("workload compatibility policy is invalid")
    architectures = compatibility.get("architectures", ())
    supported_os = compatibility.get("operating_systems", ())
    if (
        not isinstance(architectures, (tuple, list))
        or not isinstance(supported_os, (tuple, list))
        or not all(isinstance(value, str) for value in architectures)
        or not all(isinstance(value, str) for value in supported_os)
    ):
        raise ValueError("workload compatibility policy is invalid")
    try:
        local_architecture = {
            "linux-arm64": "arm64",
            "linux-x86_64": "x86_64",
        }[protocol_architecture]
    except KeyError as error:
        raise ValueError("workload architecture policy is invalid") from error
    if local_architecture not in architectures:
        raise ValueError("workload release is incompatible with this node")
    local_os = operating_systems or _local_workload_os_identities()
    if not set(supported_os).intersection(local_os):
        raise ValueError("workload operating system is incompatible with this node")
    required_storage = compatibility.get("minimum_storage_bytes", 0)
    if (
        type(required_storage) is not int
        or required_storage < 0
        or type(available_storage_bytes) is not int
        or available_storage_bytes < required_storage
    ):
        raise ValueError("workload storage capacity is insufficient")
    required_capabilities = compatibility.get("required_capabilities", ())
    if not isinstance(required_capabilities, (tuple, list)) or not all(
        isinstance(value, str) for value in required_capabilities
    ):
        raise ValueError("workload capability policy is invalid")
    backends = compatibility.get("backends", ())
    if not isinstance(backends, (tuple, list)) or len(backends) != 1 or backends[0] not in {
        "native",
        "oci",
        "python-venv",
    }:
        raise ValueError("workload backend policy is invalid")
    if backends[0] == "python-venv":
        _validate_python_runtime_components(lock)
    elif backends[0] == "oci":
        _oci_bundle_metadata(lock)
    elif backends[0] != "native":
        raise ValueError("workload backend runtime capability is unavailable")
    if getattr(lock, "adapter_abi", None) != 1:
        raise ValueError("workload adapter ABI is unsupported")
    for field, actual in (
        ("minimum_driver", driver_version),
        ("minimum_cuda", cuda_version),
    ):
        minimum = compatibility.get(field)
        if minimum is None:
            continue
        if not isinstance(actual, str) or not _version_at_least(actual, minimum):
            raise ValueError(f"workload {field} requirement is not met")
    if request is not None:
        deployment = getattr(request, "deployment", None)
        if deployment is not None:
            resources = deployment.get("resources") if isinstance(deployment, Mapping) else None
            if not isinstance(resources, Mapping):
                raise ValueError("workload deployment resources are invalid")
            memory = resources.get("memory_bytes")
            gpu_count = resources.get("gpu_count")
            if (
                not isinstance(memory, int)
                or isinstance(memory, bool)
                or memory < 1
                or not isinstance(gpu_count, int)
                or isinstance(gpu_count, bool)
                or not 0 <= gpu_count <= 1024
            ):
                raise ValueError("workload deployment resources are invalid")
            minimum_memory = compatibility.get("minimum_memory_bytes")
            if minimum_memory is not None and (
                type(minimum_memory) is not int
                or minimum_memory < 1
                or memory < minimum_memory
            ):
                raise ValueError("workload memory requirement is not met")


def _version_at_least(actual: str, minimum: str) -> bool:
    def parts(value: str) -> tuple[int, ...] | None:
        pieces = re.findall(r"\d+", value)
        return tuple(int(piece) for piece in pieces[:8]) if pieces else None

    parsed_actual, parsed_minimum = parts(actual), parts(minimum)
    if parsed_actual is None or parsed_minimum is None:
        return False
    width = max(len(parsed_actual), len(parsed_minimum))
    return parsed_actual + (0,) * (width - len(parsed_actual)) >= parsed_minimum + (0,) * (width - len(parsed_minimum))


def _backend_invocation_for_workload(
    lock: object,
    package_request: object | None,
    *,
    release_digest: str,
    generation: str,
) -> BackendInvocation:
    """Project signed family/deployment policy into the helper ABI."""
    compatibility = getattr(lock, "compatibility", {})
    if not isinstance(compatibility, Mapping):
        raise TypeError("workload compatibility policy is invalid")
    backend_names = compatibility.get("backends")
    if not isinstance(backend_names, (tuple, list)) or len(backend_names) != 1:
        raise RuntimeError("workload backend policy is invalid")
    try:
        backend = Backend(backend_names[0])
    except (TypeError, ValueError) as error:
        raise RuntimeError("workload backend policy is invalid") from error
    if getattr(lock, "adapter_abi", None) != 1:
        raise RuntimeError("workload adapter ABI is unsupported")
    deployment = getattr(package_request, "deployment", None)
    if deployment is None:
        raise RuntimeError("workload deployment projection is missing")
    if not isinstance(deployment, Mapping):
        raise TypeError("workload deployment policy is invalid")
    raw_resources = deployment.get("resources")
    if not isinstance(raw_resources, Mapping):
        raise TypeError("workload deployment resources are invalid")
    memory_bytes = raw_resources.get("memory_bytes")
    if not isinstance(memory_bytes, int) or isinstance(memory_bytes, bool):
        raise TypeError("workload deployment memory policy is invalid")
    arguments = tuple(deployment.get("arguments", ()))
    mounts = tuple(MountPolicy.parse(item) for item in deployment.get("mounts", ()))
    devices = tuple(deployment.get("devices", ()))
    network = NetworkPolicy.parse(
        deployment.get("network", {"mode": "none", "egress": []})
    )
    resources = ResourcePolicy(1000, memory_bytes, 256, 900, 64 * 1024)
    adapter = getattr(lock, "adapter", None)
    adapter_name = getattr(adapter, "name", None)
    if not isinstance(adapter_name, str):
        raise TypeError("workload adapter identity is invalid")
    python_runtime = None
    oci_bundle = None
    oci_bundle_digest = None
    if backend is Backend.PYTHON_VENV:
        python_runtime = _python_runtime_policy(lock)
    elif backend is Backend.OCI:
        oci_bundle = _oci_bundle_metadata(lock)
        components = getattr(lock, "components", ())
        bundle_components = [
            item
            for item in components
            if (
                isinstance(getattr(item, "materialization", None), Mapping)
                and getattr(item, "materialization", {}).get("method")
                == "oci-bundle"
            )
        ]
        if len(bundle_components) != 1:
            raise RuntimeError("signed OCI bundle component is missing or ambiguous")
        oci_bundle_digest = str(bundle_components[0].digest).removeprefix("sha256:")
    entrypoint = (
        oci_bundle.entrypoint
        if oci_bundle is not None
        else f"components/{adapter_name}/{adapter_name}"
    )
    return BackendInvocation(
        schema_version=1,
        backend=backend,
        release_digest=release_digest,
        generation=generation,
        entrypoint=entrypoint,
        arguments=arguments,
        resources=resources,
        mounts=mounts,
        devices=devices,
        network=network,
        python_runtime=python_runtime,
        oci_bundle=oci_bundle,
        oci_bundle_digest=oci_bundle_digest,
    )


def _python_runtime_policy(lock: object) -> PythonRuntimePolicy:
    """Project lock-signed Python runtime metadata after descriptor binding."""
    compatibility = getattr(lock, "compatibility", {})
    runtime = (
        compatibility.get("python_runtime")
        if isinstance(compatibility, Mapping)
        else None
    )
    runtime = _python_runtime_document(runtime)
    try:
        policy = PythonRuntimePolicy.parse(runtime)
    except (TypeError, ValueError) as error:
        raise RuntimeError("signed Python runtime metadata is invalid") from error
    _validate_python_runtime_components(lock, policy)
    return policy


def _oci_bundle_metadata(lock: object) -> OciBundleMetadata:
    components = getattr(lock, "components", ())
    if not isinstance(components, (tuple, list)):
        raise TypeError("OCI bundle components are unavailable")
    matches = []
    for component in components:
        materialization = getattr(component, "materialization", None)
        if (
            isinstance(materialization, Mapping)
            and materialization.get("method") == "oci-bundle"
        ):
            matches.append(materialization)
    if len(matches) != 1:
        raise ValueError("signed OCI bundle component is missing or ambiguous")
    try:
        return OciBundleMetadata.parse(
            {key: value for key, value in matches[0].items() if key != "method"}
        )
    except (TypeError, ValueError) as error:
        raise ValueError("signed OCI bundle metadata is invalid") from error


def _validate_python_runtime_components(
    lock: object, policy: PythonRuntimePolicy | None = None
) -> None:
    if policy is None:
        compatibility = getattr(lock, "compatibility", {})
        runtime = (
            compatibility.get("python_runtime")
            if isinstance(compatibility, Mapping)
            else None
        )
        try:
            policy = PythonRuntimePolicy.parse(_python_runtime_document(runtime))
        except (TypeError, ValueError) as error:
            raise ValueError("signed Python runtime metadata is invalid") from error
    components = getattr(lock, "components", ())
    if not isinstance(components, (tuple, list)):
        raise TypeError("Python runtime components are unavailable")
    by_name = {
        getattr(item, "name", None): item
        for item in components
        if isinstance(getattr(item, "name", None), str)
    }
    environment = by_name.get(policy.environment_component)
    interpreter = by_name.get(policy.interpreter_component)
    if environment is None or interpreter is None:
        raise ValueError("signed Python runtime component is missing")
    environment_method = getattr(environment, "materialization", None)
    interpreter_method = getattr(interpreter, "materialization", None)
    if environment_method != {"method": "pylock-environment"}:
        raise ValueError("Python environment component materialization is invalid")
    if not isinstance(interpreter_method, Mapping):
        raise TypeError("Python interpreter component materialization is invalid")
    method = interpreter_method.get("method")
    if method not in {"archive", "native-archive", "executable"}:
        raise ValueError("Python interpreter component materialization is invalid")
    if method == "executable" and policy.interpreter_entrypoint != interpreter.name:
        raise ValueError("Python executable interpreter entrypoint is invalid")
    interpreter_digest = str(getattr(interpreter, "digest", "")).removeprefix("sha256:")
    if interpreter_digest != policy.interpreter_component_digest:
        raise ValueError("Python interpreter component digest is not lock-bound")
    environment_digest = str(getattr(environment, "digest", "")).removeprefix("sha256:")
    if environment_digest != policy.environment_digest:
        raise ValueError("Python environment digest is not lock-bound")


def _python_runtime_document(value: object) -> object:
    if not isinstance(value, Mapping):
        return value
    return {
        **value,
        "environment_digest": str(
            value.get("environment_digest", "")
        ).removeprefix("sha256:"),
        "environment_tree_digest": str(
            value.get("environment_tree_digest", "")
        ).removeprefix("sha256:"),
        "interpreter_digest": str(value.get("interpreter_digest", "")).removeprefix(
            "sha256:"
        ),
        "interpreter_component_digest": str(
            value.get("interpreter_component_digest", "")
        ).removeprefix("sha256:"),
    }


class Interrupt(Protocol):
    def is_set(self) -> bool: ...

    def wait(self, timeout: float) -> bool: ...


_CONTROL_HEARTBEAT_LEASE_SECONDS = 30.0
_HEARTBEAT_TRANSPORT_BOUND_SECONDS = 15.0
_HEARTBEAT_SCHEDULING_MARGIN_SECONDS = 5.0
_DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 10.0
_MAX_HEARTBEAT_INTERVAL_SECONDS = (
    _CONTROL_HEARTBEAT_LEASE_SECONDS
    - _HEARTBEAT_TRANSPORT_BOUND_SECONDS
    - _HEARTBEAT_SCHEDULING_MARGIN_SECONDS
)
_DEFAULT_HEARTBEAT_JOIN_SECONDS = 20.0
_AGENT_UPDATE_STAGING_ROOT = Path("/var/lib/vonk-forge-agent/update-staging")
_PROTOCOL_ARCHITECTURE = {
    "aarch64": "linux-arm64",
    "x86_64": "linux-x86_64",
}


class AgentHeartbeatShutdownError(RuntimeError):
    """The agent cannot safely continue while an old heartbeat may be in flight."""


class _ActiveHeartbeat:
    def __init__(
        self,
        client: AgentControl,
        context: OperationContext,
        claim: AgentClaim,
        *,
        interval_seconds: float,
        join_seconds: float,
        wait: Callable[[threading.Event, float], bool],
        execution_deadline: MonotonicDeadline | None,
        on_authenticated_exchange: Callable[[], None],
    ) -> None:
        self._client = client
        self._context = context
        self._claim = claim
        self._interval_seconds = interval_seconds
        self._join_seconds = join_seconds
        self._wait = wait
        self._execution_deadline = execution_deadline
        self._on_authenticated_exchange = on_authenticated_exchange
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("heartbeat worker already started")
        exact = self._context.state.lookup_exact(self._claim)
        if exact is None or exact.result is not None or exact.state != "active":
            raise RuntimeError("heartbeat attempt is not durably active")
        self._thread = threading.Thread(
            target=self._run,
            name=f"vonk-agent-heartbeat-{self._claim.operation_id[:8]}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop.set()
        thread.join(self._join_seconds)
        if thread.is_alive():
            raise AgentHeartbeatShutdownError("heartbeat worker did not stop")
        if self._error is not None:
            raise self._error

    def _run(self) -> None:
        while True:
            try:
                if self._wait(self._stop, self._interval_seconds):
                    return
                current = self._context.state.lookup_exact(self._claim)
                if current is None or current.result is not None:
                    return
                deadline = (
                    current.claim.deadline
                    if current.progress is None
                    else current.progress.deadline
                )
                request = AgentProgress(
                    schema_version=self._claim.schema_version,
                    job_id=self._claim.job_id,
                    operation_id=self._claim.operation_id,
                    attempt=self._claim.attempt,
                    fence=self._claim.fence,
                    node_id=self._claim.node_id,
                    deadline=deadline,
                    progress={"phase": "executing"},
                )
                response = self._client.heartbeat(request)
                if isinstance(response, AgentProgress):
                    response = AgentDirective(
                        schema_version=response.schema_version,
                        job_id=response.job_id,
                        operation_id=response.operation_id,
                        attempt=response.attempt,
                        fence=response.fence,
                        node_id=response.node_id,
                        deadline=response.deadline,
                        cancel_requested=False,
                    )
                persisted = self._context.state.apply_directive(
                    request,
                    response,
                    allow_terminal_race=True,
                )
                if persisted.state != "active":
                    return
                if self._execution_deadline is not None:
                    self._execution_deadline.extend(response.deadline)
                self._on_authenticated_exchange()
            except BaseException as error:  # noqa: BLE001 - transfer to agent thread
                self._error = error
                self._stop.set()
                return


class Agent:
    def __init__(
        self,
        client: AgentControl,
        registry: OperationRegistry,
        context: OperationContext,
        *,
        backoff_min_seconds: float = 1,
        backoff_max_seconds: float = 60,
        jitter: Callable[[float], float] | None = None,
        credentials: CredentialStore | None = None,
        on_authenticated_exchange: Callable[[], object] | None = None,
        heartbeat_interval_seconds: float = _DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        heartbeat_join_seconds: float = _DEFAULT_HEARTBEAT_JOIN_SECONDS,
        heartbeat_wait: Callable[[threading.Event, float], bool] | None = None,
    ) -> None:
        if backoff_min_seconds <= 0 or backoff_max_seconds < backoff_min_seconds:
            raise ValueError("backoff bounds are invalid")
        try:
            heartbeat_interval = float(heartbeat_interval_seconds)
            heartbeat_join = float(heartbeat_join_seconds)
        except (TypeError, ValueError) as error:
            raise ValueError("heartbeat bounds are invalid") from error
        if (
            isinstance(heartbeat_interval_seconds, bool)
            or not 0 < heartbeat_interval <= _MAX_HEARTBEAT_INTERVAL_SECONDS
        ):
            raise ValueError("heartbeat interval must precede control lease")
        if (
            isinstance(heartbeat_join_seconds, bool)
            or not math.isfinite(heartbeat_join)
            or heartbeat_join <= 0
        ):
            raise ValueError("heartbeat join bound must be positive")
        self._client = client
        self._registry = registry
        self._context = context
        self._backoff_min = float(backoff_min_seconds)
        self._backoff_max = float(backoff_max_seconds)
        self._jitter = jitter or (lambda upper: random.uniform(0, upper))
        self._credentials = credentials
        self._on_authenticated_exchange = on_authenticated_exchange or (lambda: None)
        self._authenticated_exchange_reported = False
        self._authenticated_exchange_lock = threading.Lock()
        self._heartbeat_interval = heartbeat_interval
        self._heartbeat_join = heartbeat_join
        self._heartbeat_wait = heartbeat_wait or (
            lambda stop, timeout: stop.wait(timeout)
        )
        self._heartbeat_shutdown_error: AgentHeartbeatShutdownError | None = None

    def _report_authenticated_exchange(self) -> None:
        with self._authenticated_exchange_lock:
            if self._authenticated_exchange_reported:
                return
            self._on_authenticated_exchange()
            self._authenticated_exchange_reported = True

    def run_once(self) -> None:
        if self._heartbeat_shutdown_error is not None:
            raise self._heartbeat_shutdown_error
        pending = self._context.state.recover_pending()
        if pending is not None:
            assert pending.result is not None
            self._submit(pending.result)
            return
        active = self._context.state.recover_active()
        if active is not None:
            execution = self._execute(active.claim)
            self._submit(execution.result)
            return
        if self._credentials is not None:
            self._rotate_credentials()
        claim = self._client.claim()
        if claim is None:
            self._report_authenticated_exchange()
            return
        self._report_authenticated_exchange()
        execution = self._execute(claim)
        self._submit(execution.result)

    def _execute(self, claim: AgentClaim) -> OperationExecution:
        exact = self._context.state.lookup_exact(claim)
        latest_deadline = (
            claim.deadline
            if exact is None or exact.progress is None
            else exact.progress.deadline
        )
        try:
            execution_deadline = MonotonicDeadline.bind(latest_deadline)
        except ValueError:
            execution_deadline = None
        heartbeat = _ActiveHeartbeat(
            self._client,
            self._context,
            claim,
            interval_seconds=self._heartbeat_interval,
            join_seconds=self._heartbeat_join,
            wait=self._heartbeat_wait,
            execution_deadline=execution_deadline,
            on_authenticated_exchange=self._report_authenticated_exchange,
        )
        try:
            return self._registry.execute(
                claim,
                self._context,
                on_active=heartbeat.start,
                execution_deadline=execution_deadline,
            )
        finally:
            try:
                heartbeat.stop()
            except AgentHeartbeatShutdownError as error:
                self._heartbeat_shutdown_error = error
                raise

    def run_forever(self, stop: Interrupt) -> None:
        backoff = self._backoff_min
        while not stop.is_set():
            try:
                self.run_once()
            except AgentTransportError:
                delay = max(0.0, min(backoff, float(self._jitter(backoff))))
                if stop.wait(delay):
                    return
                backoff = min(self._backoff_max, backoff * 2)
            else:
                backoff = self._backoff_min

    def _submit(self, result: AgentResult) -> None:
        self._client.result(result)
        self._report_authenticated_exchange()
        self._context.state.acknowledge(result)

    def _rotate_credentials(self) -> None:
        assert self._credentials is not None
        staged = self._credentials.staged_provider()
        if staged is not None:
            generation = self._credentials.staged_generation
            assert generation is not None
            self._client.activate(generation, staged)
            self._credentials.publish_active(generation)
            self._report_authenticated_exchange()
            return
        pending = self._credentials.pending_rotation()
        if pending is None:
            if not self._credentials.renewal_due(datetime.now(UTC)):
                return
            pending = self._credentials.prepare_rotation(self._context.node_id)
        elif pending.purpose != "rotation":
            raise RuntimeError("enrollment credential request was not recovered")
        issued = self._client.renew(pending.csr_pem)
        self._credentials.stage(issued)
        staged = self._credentials.staged_provider()
        if staged is None:
            raise RuntimeError("staged credential was not published")
        self._client.activate(issued.generation, staged)
        self._credentials.publish_active(issued.generation)
        self._report_authenticated_exchange()


def build_agent(
    config: AgentConfig,
    *,
    credentials: CredentialStore | None = None,
    readiness: ReadinessReporter | None = None,
) -> Agent:
    credentials = credentials or CredentialStore(
        config.state_root,
        config.ca_path,
        config.certificate_path,
        config.private_key_path,
    )
    client = AgentClient(
        config.control_origin,
        config.node_id,
        credentials,
        long_poll_seconds=min(60, config.poll_max_seconds),
        lease_seconds=max(30, min(300, config.poll_max_seconds * 2)),
        runtime_identity=AgentRuntimeIdentity.from_environment(),
    )
    state = AgentStateStore(config.state_root)
    policy = InstalledPolicy.load(config.installed_policy_path)
    runtime = RuntimePolicy.load(config.runtime_policy_path)
    runtime.verify_installed()
    fetcher = BoundedHTTPSFetcher(
        config.control_origin,
        credential_provider=credentials,
    )
    trust = TUFReleaseTrust(
        runtime.tuf.metadata_root,
        runtime.tuf.target_root,
        f"{config.control_origin}/agent/v1/tuf/metadata/",
        f"{config.control_origin}/agent/v1/tuf/targets/",
        runtime.read_bootstrap_root(),
        fetcher,
        runtime.registry_origin,
        runtime.repository,
        runtime.architecture,
    )
    oras = ORASClient(
        ORASPolicy(
            runtime.registry_origin,
            runtime.repository,
            runtime.oras.executable,
            runtime.oras.sha256,
            runtime.oras.version,
            runtime.oras.auth_path,
            config.ca_path,
            config.certificate_path,
            config.private_key_path,
            allow_unprivileged_test_files=runtime.allow_unprivileged_test_files,
            credential_provider=credentials,
        )
    )
    releases = ReleaseInstaller(
        trust,
        oras,
        runtime.release_root,
        runtime.staging_root,
    )
    workloads = WorkloadOperations(runtime.release_root, trust)
    protocol_architecture = _PROTOCOL_ARCHITECTURE[runtime.architecture]
    platform_fetcher = PlatformTUFRouteFetcher(
        fetcher, control_origin=config.control_origin
    )
    platform_trust = PlatformAgentTrust(
        UpdateTrust(
            runtime.tuf.metadata_root / "platform",
            runtime.tuf.target_root / "platform",
            f"{config.control_origin}/platform/metadata/",
            f"{config.control_origin}/platform/targets/",
            runtime.read_bootstrap_root(),
            platform_fetcher,
        ),
        platform_fetcher,
    )
    updates = AgentUpdater(
        architecture=protocol_architecture,
        protocol_version=3,
        staging_root=_AGENT_UPDATE_STAGING_ROOT,
        trust=platform_trust,
        transport=ORASAgentTransport(
            oras,
            registry_origin=runtime.registry_origin,
            repository=runtime.repository,
            architecture=protocol_architecture,
        ),
        supervisor=LocalSupervisor(),
        available_bytes=lambda: shutil.disk_usage(
            _AGENT_UPDATE_STAGING_ROOT.parent
        ).free,
    )

    context = OperationContext(
        node_id=config.node_id,
        state=state,
        probe=PinnedNodeProbe(policy),
        releases=releases,
        workloads=workloads,
        updates=updates,
    )
    return Agent(
        client,
        OperationRegistry(),
        context,
        backoff_min_seconds=config.poll_min_seconds,
        backoff_max_seconds=config.poll_max_seconds,
        credentials=credentials,
        on_authenticated_exchange=(
            readiness or ReadinessReporter.from_environment()
        ).report,
    )


class EnrollmentControl(Protocol):
    def enroll(
        self,
        enrollment_origin: str,
        grant_token: str,
        csr: bytes,
        evidence: Mapping[str, object],
    ) -> EnrollmentPending | IssuedCredential: ...


def ensure_initial_enrollment(
    config: AgentConfig,
    credentials: CredentialStore,
    client: EnrollmentControl,
    evidence: Mapping[str, object],
) -> bool:
    """Attempt one idempotent initial enrollment pickup."""
    if credentials.has_active_credentials:
        return True
    pending = credentials.prepare_enrollment(config.node_id)
    request = x509.load_pem_x509_csr(pending.csr_pem)
    public = request.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    exact_evidence = dict(evidence)
    exact_evidence["node_id"] = config.node_id
    exact_evidence["csr_public_key_fingerprint"] = hashlib.sha256(public).hexdigest()
    if set(exact_evidence) != {
        "agent_digest",
        "boot_id",
        "csr_public_key_fingerprint",
        "hardware_fingerprint",
        "host_key_fingerprint",
        "node_id",
    }:
        raise RuntimeError("initial enrollment evidence fields are invalid")
    directory, token, identity = _open_enrollment_token(config.enrollment_token_path)
    try:
        response = client.enroll(
            config.enrollment_origin,
            token,
            pending.csr_pem,
            exact_evidence,
        )
        if isinstance(response, EnrollmentPending):
            return False
        credentials.install_initial(response)
        current = os.stat(
            config.enrollment_token_path.name,
            dir_fd=directory,
            follow_symlinks=False,
        )
        if (current.st_dev, current.st_ino) != identity:
            raise RuntimeError("enrollment token changed before consumption")
        os.unlink(config.enrollment_token_path.name, dir_fd=directory)
        os.fsync(directory)
        return True
    finally:
        os.close(directory)


def remove_consumed_enrollment_token(
    config: AgentConfig, credentials: CredentialStore
) -> bool:
    """Finish token consumption after a crash beyond active publication."""
    if not credentials.has_published_credentials:
        return False
    try:
        directory, _token, identity = _open_enrollment_token(
            config.enrollment_token_path
        )
    except FileNotFoundError:
        return False
    try:
        current = os.stat(
            config.enrollment_token_path.name,
            dir_fd=directory,
            follow_symlinks=False,
        )
        if (current.st_dev, current.st_ino) != identity:
            raise RuntimeError("enrollment token changed before recovery consumption")
        os.unlink(config.enrollment_token_path.name, dir_fd=directory)
        os.fsync(directory)
        return True
    finally:
        os.close(directory)


def _open_enrollment_token(path: Path) -> tuple[int, str, tuple[int, int]]:
    if not path.is_absolute() or len(path.parts) < 2:
        raise RuntimeError("enrollment token path is invalid")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    descriptor = -1
    try:
        for component in path.parts[1:-1]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory,
            )
            os.close(directory)
            directory = child
            metadata = os.fstat(directory)
            mode = stat.S_IMODE(metadata.st_mode)
            if metadata.st_uid not in {0, os.geteuid()} or (
                mode & 0o022 and not mode & stat.S_ISVTX
            ):
                raise RuntimeError("enrollment token ancestry is unsafe")
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > 128
        ):
            raise RuntimeError("enrollment token is unsafe")
        raw = os.read(descriptor, 129)
        match = re.fullmatch(rb"([A-Za-z0-9_-]{43})\n?", raw)
        if match is None:
            raise RuntimeError("enrollment token is invalid")
        return (
            directory,
            match.group(1).decode("ascii"),
            (metadata.st_dev, metadata.st_ino),
        )
    except Exception:
        os.close(directory)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _bounded_text(path: Path, fallback: str) -> str:
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    descriptor = -1
    try:
        for component in path.parts[1:-1]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory,
            )
            os.close(directory)
            directory = child
            metadata = os.fstat(directory)
            mode = stat.S_IMODE(metadata.st_mode)
            if metadata.st_uid != 0 or (mode & 0o022 and not mode & stat.S_ISVTX):
                return fallback
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or metadata.st_size > 512
        ):
            return fallback
        raw = os.read(descriptor, 513)
    except OSError:
        return fallback
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory)
    if not 0 < len(raw) <= 512:
        return fallback
    value = raw.strip()
    try:
        return value.decode("ascii", "strict") if value else fallback
    except UnicodeDecodeError:
        return fallback


def _enrollment_evidence() -> dict[str, object]:
    agent_digest = os.environ.get("VONK_AGENT_SUPERVISOR_SHA256", "")
    if re.fullmatch(r"[0-9a-f]{64}", agent_digest) is None:
        raise RuntimeError("supervised agent digest is unavailable")
    machine = _bounded_text(Path("/etc/machine-id"), "unavailable")
    host = _bounded_text(Path("/etc/ssh/ssh_host_ed25519_key.pub"), "unavailable")
    return {
        "agent_digest": agent_digest,
        "boot_id": _bounded_text(
            Path("/proc/sys/kernel/random/boot_id"), "unavailable"
        ),
        "csr_public_key_fingerprint": "0" * 64,
        "hardware_fingerprint": hashlib.sha256(machine.encode("ascii")).hexdigest(),
        "host_key_fingerprint": hashlib.sha256(host.encode("ascii")).hexdigest(),
        "node_id": "pending",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vonk Forge outbound agent")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="absolute path to the restrictive agent configuration",
    )
    arguments = parser.parse_args(argv)
    config = AgentConfig.load(arguments.config)
    stop = threading.Event()

    def terminate(_signal: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    readiness = ReadinessReporter.from_environment()
    credentials = CredentialStore(
        config.state_root,
        config.ca_path,
        config.certificate_path,
        config.private_key_path,
    )
    enrollment_client = AgentClient(
        config.control_origin,
        config.node_id,
        credentials,
        long_poll_seconds=min(60, config.poll_max_seconds),
        lease_seconds=max(30, min(300, config.poll_max_seconds * 2)),
    )
    while not credentials.has_active_credentials and not stop.is_set():
        try:
            enrolled = ensure_initial_enrollment(
                config,
                credentials,
                enrollment_client,
                _enrollment_evidence(),
            )
        except AgentTransportError:
            enrolled = False
        if enrolled:
            break
        stop.wait(config.poll_min_seconds)
    if stop.is_set():
        return 0
    credentials.recover_initial_enrollment(config.node_id)
    remove_consumed_enrollment_token(config, credentials)
    build_agent(config, credentials=credentials, readiness=readiness).run_forever(stop)
    return 0
