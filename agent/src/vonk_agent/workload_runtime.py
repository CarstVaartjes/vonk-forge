"""Installed-agent composition for the generic workload package plane.

This module deliberately keeps workload trust and payload acquisition separate
from the platform update trust.  The only model/runtime knowledge here is the
stable descriptor and adapter ABI; family and release identities come from a
signed workload lock at operation time.
"""

from __future__ import annotations

import hashlib
import ipaddress
import math
import os
import re
import socket
import ssl
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import urllib3
from tuf.api.exceptions import DownloadError, RepositoryError
from tuf.ngclient import FetcherInterface, Updater
from tuf.ngclient.config import UpdaterConfig
from vonk_agent_protocol import AgentProtocolError
from vonk_agent_protocol.workload_packages import (
    ComponentDescriptor as ProtocolComponent,
)

from .deadlines import DeadlineBindingError, MonotonicDeadline
from .package_trust import TrustedWorkloadTarget, WorkloadTrustError
from .packages.providers import (
    ComponentDescriptor,
    FetchResponse,
    NetworkHop,
    ProviderRequest,
    ProviderTransport,
    SourceLocation,
    SourcePolicy,
    Validators,
)
from .update_trust import _BOOTSTRAP_MARKER, _LOCK_NAME

# Keep these values independent of platform TUF limits.  They mirror the
# bounded workload delivery contract and are intentionally not configurable by
# a workload lock.
WORKLOAD_METADATA_LIMIT = 2 * 1024 * 1024
WORKLOAD_TARGET_LIMIT = 1024 * 1024
WORKLOAD_TUF_METADATA_ROUTE = "/agent/v1/workload-tuf/metadata/"
WORKLOAD_TUF_TARGET_ROUTE = "/agent/v1/workload-tuf/targets/"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40,64}\Z")
_OCI = re.compile(r"(?P<name>[a-z0-9][a-z0-9._/-]*)@(?P<digest>sha256:[0-9a-f]{64})\Z")
_TUF_TARGET = re.compile(r"releases/[0-9a-f]{64}\.json\Z")


def _host(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        raise AgentProtocolError("workload source URL is invalid")
    return host.lower()


def _immutable_source_url(url: str) -> str:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as error:
        raise AgentProtocolError("workload source URL is invalid") from error
    host = _host(url)
    result = f"https://{host}"
    if port is not None:
        result += f":{port}"
    result += parsed.path
    if not parsed.path.startswith("/") or parsed.query or parsed.fragment:
        raise AgentProtocolError("workload source URL is invalid")
    return result


def _oci_blob_url(reference: str) -> tuple[str, str, tuple[str, ...]]:
    match = _OCI.fullmatch(reference)
    if match is None:
        raise AgentProtocolError("OCI source reference is invalid")
    name = match.group("name")
    parts = name.split("/", 1)
    if len(parts) != 2:
        raise AgentProtocolError("OCI source registry is missing")
    registry, repository = parts
    digest = match.group("digest")
    return (
        f"https://{registry}/v2/{repository}/blobs/{digest}",
        digest,
        (registry,),
    )


def _protocol_source(
    source: Mapping[str, object],
    digest: str,
) -> SourceLocation:
    provider = source.get("provider")
    if provider == "https":
        url = _immutable_source_url(str(source["url"]))
        return SourceLocation("https", url, digest, (_host(url),))
    if provider == "oci":
        url, immutable, domains = _oci_blob_url(str(source["reference"]))
        return SourceLocation("oci", url, immutable, domains)
    if provider == "git":
        repository = _immutable_source_url(str(source["repository"]))
        commit = source["commit"]
        if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
            raise AgentProtocolError("Git source commit is invalid")
        url = f"{repository}/archive/{commit}.tar.gz"
        return SourceLocation("git", url, commit, (_host(repository),))
    if provider == "huggingface":
        repository = source.get("repository")
        revision = source.get("revision")
        if not isinstance(repository, str) or not isinstance(revision, str):
            raise AgentProtocolError("Hugging Face source is invalid")
        if _COMMIT.fullmatch(revision) is None:
            raise AgentProtocolError("Hugging Face revision is invalid")
        url = f"https://huggingface.co/{repository}/resolve/{revision}/payload"
        return SourceLocation("huggingface", url, revision, ("huggingface.co",))
    if provider in {"python-index", "signed-http-index"}:
        url = _immutable_source_url(str(source["url"]))
        source_digest = source.get("digest")
        if not isinstance(source_digest, str) or _DIGEST.fullmatch(source_digest) is None:
            raise AgentProtocolError("index source digest is invalid")
        return SourceLocation(
            str(provider),
            url,
            source_digest,
            (_host(url),),
            signature_digest=source_digest if provider == "signed-http-index" else None,
        )
    raise AgentProtocolError("workload source provider is unsupported")


def protocol_component(
    descriptor: ProtocolComponent,
    *,
    platform: str | None = None,
) -> ComponentDescriptor:
    """Convert the signed wire descriptor into the acquisition descriptor.

    The two descriptor types intentionally remain distinct: the protocol type
    carries evidence/materialization policy, while the agent type carries only
    the source policy needed by the fetch engine.
    """

    if type(descriptor) is not ProtocolComponent:
        raise TypeError("protocol component descriptor is invalid")
    selected = platform or (descriptor.platforms[0] if descriptor.platforms else None)
    if selected is None or selected not in descriptor.platforms:
        raise AgentProtocolError("component platform is not supported")
    digest = descriptor.digest
    sources = tuple(
        _protocol_source(source, digest) for source in descriptor.sources
    )
    return ComponentDescriptor(
        name=descriptor.name,
        kind=descriptor.kind,
        digest=digest,
        size=descriptor.size,
        sources=sources,
        unpacked_size=descriptor.unpacked_size,
    )


class ProtocolAcquisition:
    """Adapt signed wire descriptors to the internal provider ABI.

    PackageOperationRequest carries protocol ``ComponentDescriptor`` values.
    The acquisition engine intentionally accepts a narrower internal type so
    that untrusted wire objects cannot bypass source-policy validation.  Keep
    this conversion at the production composition boundary and preserve all
    operation callbacks/deadlines unchanged.
    """

    def __init__(self, engine: object, *, platform: str) -> None:
        if not callable(getattr(engine, "fetch", None)):
            raise TypeError("workload acquisition engine is invalid")
        if not isinstance(platform, str) or not platform:
            raise ValueError("workload platform is invalid")
        self._engine = engine
        self._platform = platform

    def fetch(
        self,
        descriptor: ProtocolComponent,
        binding: object,
        progress: Callable[..., object],
        cancelled: Callable[[], bool],
        *,
        deadline: object | None = None,
    ) -> object:
        internal = protocol_component(descriptor, platform=self._platform)
        return self._engine.fetch(
            internal,
            binding,
            progress,
            cancelled,
            deadline=deadline,
        )


class HTTPSProviderTransport(ProviderTransport):
    """Direct immutable HTTPS acquisition with no control-plane relay."""

    def __init__(
        self,
        *,
        policy: SourcePolicy | None = None,
        credentials: Callable[[str], object] | None = None,
        connect_timeout: float = 5.0,
        read_timeout: float = 15.0,
    ) -> None:
        self._policy = policy or SourcePolicy()
        self._credentials = credentials
        if not 0 < connect_timeout <= 60 or not 0 < read_timeout <= 60:
            raise ValueError("workload source timeout is invalid")
        self._connect_timeout = float(connect_timeout)
        self._read_timeout = float(read_timeout)

    def open(self, request: ProviderRequest, deadline: object | None) -> FetchResponse:
        parsed = urlsplit(request.url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise RuntimeError("workload source URL is invalid")
        try:
            addresses = tuple(
                sorted(
                    {
                        str(item[4][0])
                        for item in socket.getaddrinfo(
                            parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM
                        )
                    }
                )
            )
        except OSError as error:
            raise RuntimeError("workload source DNS resolution failed") from error
        if not addresses:
            raise RuntimeError("workload source address is unavailable")
        for raw in addresses:
            try:
                address = ipaddress.ip_address(raw)
            except ValueError as error:
                raise RuntimeError("workload source address is invalid") from error
            if not self._policy.allow_private_addresses and (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_multicast
                or address.is_reserved
                or address.is_unspecified
            ):
                raise RuntimeError("workload source address is not allowed")
        headers = {str(k): str(v) for k, v in request.headers.items()}
        if request.authorization is not None:
            headers["Authorization"] = request.authorization
        remaining = _remaining(deadline)
        timeout = urllib3.Timeout(
            connect=min(self._connect_timeout, remaining),
            read=min(self._read_timeout, remaining),
            total=remaining,
        )
        context = ssl.create_default_context()
        pool = urllib3.PoolManager(num_pools=1, maxsize=1, block=True, ssl_context=context, retries=False)
        try:
            response = pool.request(
                "GET",
                request.url,
                headers=headers,
                redirect=False,
                retries=False,
                preload_content=False,
                timeout=timeout,
            )
        except Exception:
            pool.clear()
            raise
        if response.status not in {200, 206}:
            status = response.status
            response.release_conn()
            pool.clear()
            raise RuntimeError(f"workload source returned HTTP {status}")
        start = 0
        total: int | None = None
        content_range = response.headers.get("Content-Range")
        if response.status == 206:
            if not isinstance(content_range, str):
                response.release_conn()
                pool.clear()
                raise RuntimeError("workload source range response is invalid")
            match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", content_range)
            if match is None:
                response.release_conn()
                pool.clear()
                raise RuntimeError("workload source range response is invalid")
            start, end, total = map(int, match.groups())
            if end < start or total <= end:
                response.release_conn()
                pool.clear()
                raise RuntimeError("workload source range response is invalid")
        elif request.headers.get("Range"):
            response.release_conn()
            pool.clear()
            raise RuntimeError("workload source ignored requested range")
        if total is None:
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    total = int(content_length) + start
                except ValueError:
                    response.release_conn()
                    pool.clear()
                    raise RuntimeError("workload source length is invalid") from None
        validators = Validators(
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
        )

        def chunks() -> Iterable[bytes]:
            try:
                while True:
                    _check_deadline(deadline)
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        return
                    yield bytes(chunk)
            finally:
                response.release_conn()
                pool.clear()

        return FetchResponse(
            status_code=response.status,
            start_offset=start,
            total_size=total,
            validators=validators,
            chunks=chunks(),
            hops=(NetworkHop(request.url, addresses),),
        )


def _remaining(deadline: object | None) -> float:
    if deadline is not None and hasattr(deadline, "remaining"):
        value = float(deadline.remaining())
    else:
        value = 60.0
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError("workload source deadline elapsed")
    return value


def _check_deadline(deadline: object | None) -> None:
    if deadline is not None and hasattr(deadline, "check"):
        deadline.check()


class WorkloadTUFSource(FetcherInterface):
    """Persistent workload-only TUF verifier and target source.

    ``refresh`` and ``trusted_target`` are intentionally paired.  The cache
    lock remains held between those calls, so a target cannot be authorized
    against metadata that another writer replaced between verification and
    download.
    """

    def __init__(
        self,
        metadata_root: Path,
        target_root: Path,
        metadata_base_url: str,
        target_base_url: str,
        bootstrap_root: bytes,
        fetcher: FetcherInterface,
        *,
        deadline_seconds: int = 120,
    ) -> None:
        self._metadata_root = Path(metadata_root)
        self._target_root = Path(target_root)
        self._metadata_base_url = metadata_base_url
        self._target_base_url = target_base_url
        self._bootstrap_root = bytes(bootstrap_root)
        self._fetcher = fetcher
        self._deadline_seconds = deadline_seconds
        self._updater: Updater | None = None
        self._lock_fd = -1
        if not self._metadata_root.is_absolute() or not self._target_root.is_absolute():
            raise WorkloadTrustError("workload TUF cache path is invalid")
        if self._metadata_root == self._target_root or self._metadata_root in self._target_root.parents or self._target_root in self._metadata_root.parents:
            raise WorkloadTrustError("workload TUF cache roots overlap")
        _validate_route(metadata_base_url, WORKLOAD_TUF_METADATA_ROUTE)
        _validate_route(target_base_url, WORKLOAD_TUF_TARGET_ROUTE)
        if not callable(getattr(fetcher, "set_deadline", None)):
            raise WorkloadTrustError("workload TUF fetcher lacks deadline boundary")
        if type(deadline_seconds) is not int or not 1 <= deadline_seconds <= 600:
            raise WorkloadTrustError("workload TUF deadline is invalid")

    def set_deadline(self, deadline: object) -> None:
        self._fetcher.set_deadline(deadline)

    def _fetch(self, url: str):
        yield from self._fetcher.fetch(url)

    def refresh(self) -> None:
        if self._lock_fd >= 0:
            raise WorkloadTrustError("workload TUF refresh is already active")
        deadline = MonotonicDeadline.bind(datetime.now(UTC) + timedelta(seconds=self._deadline_seconds))
        from .update_trust import (
            _cached_root_bytes,
            _established_root_is_openable,
            _fsync_cache,
            _harden_cache,
            _interruptible_tuf_call,
            _secure_directory,
            _validate_cache,
            _validate_empty_target_cache,
            _write_marker,
        )

        lock = -1
        try:
            _secure_directory(self._metadata_root, deadline)
            _secure_directory(self._target_root, deadline)
            lock = os.open(self._metadata_root / _LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _validate_cache(self._metadata_root, deadline)
            _validate_empty_target_cache(self._target_root, deadline)
            marker = self._metadata_root / _BOOTSTRAP_MARKER
            bootstrap = None if _established_root_is_openable(self._metadata_root, marker, deadline) else self._bootstrap_root
            self.set_deadline(deadline)
            updater = _interruptible_tuf_call(
                deadline,
                lambda: Updater(
                    str(self._metadata_root),
                    self._metadata_base_url,
                    str(self._target_root),
                    self._target_base_url,
                    self,
                    UpdaterConfig(
                        max_root_rotations=32,
                        max_delegations=16,
                        root_max_length=256 * 1024,
                        timestamp_max_length=64 * 1024,
                        snapshot_max_length=WORKLOAD_METADATA_LIMIT,
                        targets_max_length=WORKLOAD_METADATA_LIMIT,
                        prefix_targets_with_hash=False,
                        app_user_agent="vonk-forge-agent/0.1.0",
                    ),
                    bootstrap=bootstrap,
                ),
            )
            if bootstrap is not None:
                _harden_cache(self._metadata_root, deadline)
                _fsync_cache(self._metadata_root, deadline)
            _interruptible_tuf_call(deadline, updater.refresh)
            _harden_cache(self._metadata_root, deadline)
            _fsync_cache(self._metadata_root, deadline)
            _write_marker(marker, hashlib.sha256(_cached_root_bytes(self._metadata_root, deadline)).hexdigest(), deadline)
            self._updater = updater
            self._lock_fd = lock
            lock = -1
        except (DownloadError, RepositoryError, OSError, ValueError, RuntimeError, DeadlineBindingError) as error:
            raise WorkloadTrustError("workload TUF refresh failed") from error
        finally:
            if lock >= 0:
                os.close(lock)

    def trusted_target(self, name: str) -> TrustedWorkloadTarget:
        updater = self._updater
        if self._lock_fd < 0 or updater is None or _TUF_TARGET.fullmatch(name) is None:
            raise WorkloadTrustError("workload target request is invalid")
        import fcntl

        from .update_trust import (
            _interruptible_tuf_call,
            _read_regular_fd,
            _seal_target_fd,
        )

        digest = name.removeprefix("releases/").removesuffix(".json")
        fixed = MonotonicDeadline.bind(datetime.now(UTC) + timedelta(seconds=self._deadline_seconds))
        descriptor = -1
        try:
            self.set_deadline(fixed)
            info = _interruptible_tuf_call(fixed, lambda: updater.get_targetinfo(name))
            if info is None or info.length > WORKLOAD_TARGET_LIMIT or set(info.hashes) != {"sha256"} or info.hashes["sha256"] != digest:
                raise WorkloadTrustError("workload target is not authorized")
            descriptor = os.memfd_create("vonk-workload-tuf-target", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
            _interruptible_tuf_call(fixed, lambda: updater.download_target(info, f"/proc/self/fd/{descriptor}"))
            _seal_target_fd(descriptor, fixed)
            raw = _read_regular_fd(descriptor, WORKLOAD_TARGET_LIMIT, fixed)
            if len(raw) != info.length or hashlib.sha256(raw).hexdigest() != digest:
                raise WorkloadTrustError("workload target digest mismatch")
            return TrustedWorkloadTarget(name=name, length=len(raw), sha256=digest, data=raw)
        except WorkloadTrustError:
            raise
        except (DownloadError, RepositoryError, OSError, ValueError, RuntimeError, DeadlineBindingError) as error:
            raise WorkloadTrustError("workload target authorization failed") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(self._lock_fd)
            self._lock_fd = -1
            self._updater = None


def _validate_route(url: str, suffix: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path != suffix:
        raise WorkloadTrustError("workload TUF route is invalid")
