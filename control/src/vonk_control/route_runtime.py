"""Fail-closed, atomic route and LiteLLM bundle publication."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from vonk_agent_protocol.route_activation import (
    ROUTE_ACK_TIMEOUT_SECONDS,
    ROUTE_MAXIMUM_LEASE_SECONDS,
    ActivationMarker,
    SupervisorAcknowledgement,
)

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
RECIPE_ROUTE_AUTHORITY_ID = str(
    uuid.uuid5(uuid.NAMESPACE_URL, "https://vonkforge.ai/local-recipes")
)
_UPDATE_BOUNDARY_FIELDS = {"key", "schema_version"}


class RouteRuntimeError(RuntimeError):
    """A route bundle could not be safely staged, activated, or inspected."""


def _encoded(value: Mapping[str, object]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


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
        timeout_seconds: float = ROUTE_ACK_TIMEOUT_SECONDS,
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
        if len(content) > 4096:
            return False
        try:
            acknowledgement = SupervisorAcknowledgement.model_validate(raw)
        except ValidationError:
            return False
        if (
            content != acknowledgement.canonical_bytes()
            or acknowledgement.state != marker.state
            or acknowledgement.generation != marker.generation
            or acknowledgement.activation_sha256 != marker.digest
            or acknowledgement.litellm_sha256 != marker.litellm_sha256
            or acknowledgement.expires_at != marker.expires_at
        ):
            return False
        try:
            acknowledged = _parse_time(
                acknowledgement.acknowledged_at, "acknowledgement timestamp"
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
        clock: Callable[[], datetime],
        maximum_lease_seconds: int = ROUTE_MAXIMUM_LEASE_SECONDS,
        validate_routes: Callable[[bytes], bool] | None = None,
        validate_litellm: Callable[[bytes], bool] | None = None,
        await_supervisor_ack: Callable[[ActivationMarker], None] | None = None,
    ) -> None:
        if root.is_symlink():
            raise RouteRuntimeError("route runtime root must not be a symlink")
        if not 1 <= maximum_lease_seconds <= ROUTE_MAXIMUM_LEASE_SECONDS:
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
        self._clock = clock
        self._maximum_lease = timedelta(seconds=maximum_lease_seconds)
        self._validate_routes = validate_routes or self._valid_json_mapping
        self._validate_litellm = validate_litellm or self._valid_litellm
        self._await_supervisor_ack = await_supervisor_ack

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
    def _identity(authority_id: str, plan_digest: str, evidence_digest: str) -> None:
        try:
            parsed = uuid.UUID(authority_id)
        except (TypeError, ValueError, AttributeError) as error:
            raise RouteRuntimeError("reconciliation ID is invalid") from error
        if str(parsed) != authority_id:
            raise RouteRuntimeError("reconciliation ID is not canonical")
        if (
            _DIGEST.fullmatch(plan_digest) is None
            or _DIGEST.fullmatch(evidence_digest) is None
        ):
            raise RouteRuntimeError("publication digest identity is invalid")

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

        Callers compile typed recipe state first; the lock, validators, immutable
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
                authority_id=authority_id,
                plan_digest=plan_digest,
                evidence_set_digest=evidence_set_digest,
                routes=routes,
                litellm=litellm,
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
        authority_id: str,
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
            "schema_version": 2,
            "generation": generation,
            "state": state,
            "authority_id": authority_id,
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
        if marker.directory != f"{marker.generation:08d}-{marker.manifest_sha256}":
            raise RouteRuntimeError(
                "route activation marker directory binding is invalid"
            )


def verify_active_route_bundle(
    root: Path,
    *,
    clock: Callable[[], datetime],
    maximum_lease_seconds: int = ROUTE_MAXIMUM_LEASE_SECONDS,
) -> VerifiedRouteBundle:
    """Read and authenticate the complete active bundle without mutating it."""

    if root.is_symlink() or not root.is_dir():
        raise RouteRuntimeError("route runtime root is unavailable")
    if not 1 <= maximum_lease_seconds <= ROUTE_MAXIMUM_LEASE_SECONDS:
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
    try:
        marker = ActivationMarker.model_validate(raw)
    except ValidationError as error:
        raise RouteRuntimeError("route activation marker fields are invalid") from error
    AtomicRouteBundlePublisher._validate_marker(marker)
    if marker_content != marker.canonical_bytes():
        raise RouteRuntimeError("route activation marker is not canonical")

    documents: dict[str, Mapping[str, object]] = {}
    if verify_files:
        directory = generations / marker.directory
        if directory.is_symlink() or not directory.is_dir():
            raise RouteRuntimeError("active route generation is unavailable")
        manifest_document = marker.manifest_document()
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
