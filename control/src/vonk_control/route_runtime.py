"""Fail-closed, atomic route and LiteLLM bundle publication."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from vonk_agent_protocol import (
    ErrorCategory,
    SecurityRefusalError,
    UnknownError,
    WaitReason,
)
from vonk_agent_protocol.route_activation import (
    ROUTE_ACK_TIMEOUT_SECONDS,
    ActivationManifest,
    ActivationMarker,
    SupervisorAcknowledgement,
)

from .litellm import LiteLlmConfig
from .route_bundle_contract import RouteBundleDocument
from .strict_json import read_stored_model

# A publication claim is a short critical section, so a bounded nonblocking
# claim is enough: the reconciler retries the whole publication, which is safer
# than parking a worker thread on a contended file lock.
_PUBLICATION_LOCK_BUDGET_SECONDS = 30.0
_PUBLICATION_LOCK_RETRY_SECONDS = 0.05

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
RECIPE_ROUTE_AUTHORITY_ID = str(
    uuid.uuid5(uuid.NAMESPACE_URL, "https://vonkforge.ai/local-recipes")
)


class RouteRuntimeError(RuntimeError):
    """A route bundle could not be safely staged, activated, or inspected."""


def _unknown(reason: WaitReason) -> UnknownError:
    return UnknownError(category=ErrorCategory.UNKNOWN, reason=reason)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RouteRuntimeError(f"{label} must include a timezone")
    return value.astimezone(UTC)


def _parse_time(value: object, label: str) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class VerifiedRouteBundle:
    """One canonical, checksum-bound active route bundle."""

    marker: ActivationMarker
    #: The bundle's own documents; ``None`` where the read verified only the
    #: marker and the checksums of the files it names.
    routes: RouteBundleDocument | None
    litellm: LiteLlmConfig | None


def recipe_route_run_id(operation_id: object) -> str | None:
    """Identify a recipe route only from its accepted exact operation identity."""
    if not isinstance(operation_id, str):
        return None
    match = re.fullmatch(
        r"recipe:([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
        r"[89ab][0-9a-f]{3}-[0-9a-f]{12}):rank:(?:0|[1-9][0-9]*)",
        operation_id,
    )
    return None if match is None else match.group(1)


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

    def __call__(self, marker: ActivationMarker) -> UnknownError | None:
        deadline = self._monotonic() + self._timeout_seconds
        while True:
            now = _aware(self._clock(), "supervisor acknowledgement clock")
            if self._matches(marker, now=now):
                return
            if self._monotonic() >= deadline:
                return _unknown(WaitReason.RUNTIME_EFFECT_UNCONFIRMED)
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
        ):
            return False
        try:
            acknowledged = _parse_time(
                acknowledgement.acknowledged_at, "acknowledgement timestamp"
            )
        except RouteRuntimeError:
            return False
        return (
            acknowledged is not None
            and acknowledged <= now
            and now - acknowledged <= self._maximum_age
        )


class AtomicRouteBundlePublisher:
    """Stage a complete bundle and replace its sole activation marker last."""

    def __init__(
        self,
        root: Path,
        *,
        validate_routes: Callable[[bytes], bool] | None = None,
        validate_litellm: Callable[[bytes], bool] | None = None,
        await_supervisor_ack: Callable[[ActivationMarker], UnknownError | None]
        | None = None,
    ) -> None:
        if root.is_symlink():
            raise RouteRuntimeError("route runtime root must not be a symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o750)
        root.chmod(0o750)
        generations = root / "generations"
        if generations.is_symlink():
            raise RouteRuntimeError("route generation root must not be a symlink")
        generations.mkdir(mode=0o750, exist_ok=True)
        generations.chmod(0o750)
        self._root = root
        self._generations = generations
        self._validate_routes = validate_routes or self._valid_routes
        self._validate_litellm = validate_litellm or self._valid_litellm
        self._await_supervisor_ack = await_supervisor_ack

    def _require_supervisor_ack(self, marker: ActivationMarker) -> UnknownError | None:
        if self._await_supervisor_ack is None:
            return
        try:
            return self._await_supervisor_ack(marker)
        except (RouteRuntimeError, SecurityRefusalError):
            raise
        except Exception:  # noqa: BLE001 - unavailable acknowledgement has no authority
            return _unknown(WaitReason.RUNTIME_EFFECT_UNCONFIRMED)

    @staticmethod
    def _valid_routes(content: bytes) -> bool:
        try:
            RouteBundleDocument.model_validate_json(content)
        except (TypeError, ValueError):
            return False
        return True

    @staticmethod
    def _valid_litellm(content: bytes) -> bool:
        try:
            LiteLlmConfig.model_validate_json(content)
        except (TypeError, ValueError):
            return False
        return True

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

    @contextmanager
    def _locked(self):
        """Hold the publication lock for one bounded claim.

        The lock is a kernel file lock shared with any other publication
        process, so it is claimed nonblockingly and retried with a short sleep
        until the bounded budget expires. A publication that cannot make
        progress returns to its caller instead of pinning a worker thread
        indefinitely, and the caller's normal reconciliation retries the whole
        publication.
        """

        if (self._root / ".publication.lock").is_symlink():
            raise RouteRuntimeError("route publication lock must not be a symlink")
        try:
            descriptor = os.open(
                self._root / ".publication.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
        except OSError:
            yield _unknown(WaitReason.OBSERVATION_UNAVAILABLE)
            return
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RouteRuntimeError("route publication lock is unsafe")
            uncertainty = self._claim_publication_lock(descriptor)
        except RouteRuntimeError:
            os.close(descriptor)
            raise
        except OSError:
            os.close(descriptor)
            yield _unknown(WaitReason.OBSERVATION_UNAVAILABLE)
            return
        try:
            yield uncertainty
        finally:
            try:
                if uncertainty is None:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _claim_publication_lock(self, descriptor: int) -> UnknownError | None:
        """Acquire the publication lock nonblockingly inside a bounded budget."""

        deadline = time.monotonic() + _PUBLICATION_LOCK_BUDGET_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return _unknown(WaitReason.LEASE_LAPSED)
                time.sleep(_PUBLICATION_LOCK_RETRY_SECONDS)

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
    ) -> ActivationMarker | UnknownError:
        if self._validate_routes(routes) is not True:
            raise RouteRuntimeError("route validation rejected the staged bundle")
        if self._validate_litellm(litellm) is not True:
            raise RouteRuntimeError("LiteLLM validation rejected the staged bundle")
        manifest_document = ActivationManifest.model_validate(
            {
                "schema_version": 2,
                "generation": generation,
                "state": state,
                "authority_id": authority_id,
                "plan_digest": plan_digest,
                "evidence_set_digest": evidence_set_digest,
                "routes_sha256": _sha256(routes),
                "litellm_sha256": _sha256(litellm),
            }
        )
        manifest = manifest_document.canonical_bytes()
        manifest_digest = _sha256(manifest)
        directory_name = f"{generation:08d}-{manifest_digest}"
        directory = self._generations / directory_name
        try:
            self._stage(directory, "routes.json", routes)
            self._stage(directory, "litellm.json", litellm)
            self._stage(directory, "manifest.json", manifest)
        except RouteRuntimeError:
            raise
        except OSError:
            return _unknown(WaitReason.RUNTIME_EFFECT_UNCONFIRMED)
        marker = ActivationMarker(
            **manifest_document.model_dump(),
            directory=directory_name,
            manifest_sha256=manifest_digest,
        )
        try:
            self._atomic_write(
                self._root / "activation.json",
                marker.canonical_bytes(),
                mode=0o640,
            )
        except RouteRuntimeError:
            raise
        except OSError:
            # Replacement may already have completed before directory fsync failed.
            # The next publication observes the exact marker before replacing it.
            return _unknown(WaitReason.RUNTIME_EFFECT_UNCONFIRMED)
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
            if target.is_symlink() or not target.is_file():
                raise RouteRuntimeError(
                    "staged route generation conflicts with existing bytes"
                )
            if target.read_bytes() == content:
                return
        self._atomic_write(target, content, mode=0o640)

    def inspect(
        self, *, expected: ActivationMarker | None = None
    ) -> ActivationMarker | UnknownError:
        marker = self._read_marker(optional=False, verify_files=True)
        assert marker is not None
        # A stale expectation is observation evidence, never a reason to refuse
        # the current checksum-verified route or withdraw its working activation.
        return marker

    def _read_marker(
        self,
        *,
        optional: bool,
        verify_files: bool,
    ) -> ActivationMarker | UnknownError | None:
        bundle = _read_active_route_bundle(
            self._root,
            generations=self._generations,
            optional=optional,
            verify_files=verify_files,
            validate_documents=False,
        )
        return (
            bundle
            if isinstance(bundle, UnknownError)
            else (None if bundle is None else bundle.marker)
        )


def verify_active_route_bundle(root: Path) -> VerifiedRouteBundle | UnknownError:
    """Read and authenticate the complete active bundle without mutating it."""

    if root.is_symlink() or not root.is_dir():
        return _unknown(WaitReason.OBSERVATION_UNAVAILABLE)
    generations = root / "generations"
    if generations.is_symlink() or not generations.is_dir():
        return _unknown(WaitReason.OBSERVATION_UNAVAILABLE)
    bundle = _read_active_route_bundle(
        root,
        generations=generations,
        optional=False,
        verify_files=True,
        validate_documents=True,
    )
    assert bundle is not None
    return bundle


def _read_active_route_bundle(
    root: Path,
    *,
    generations: Path,
    optional: bool,
    verify_files: bool,
    validate_documents: bool,
) -> VerifiedRouteBundle | UnknownError | None:
    active = root / "activation.json"
    if not active.exists():
        if optional:
            return None
        return _unknown(WaitReason.RECEIPT_MISSING)
    if active.is_symlink() or not active.is_file():
        return _unknown(WaitReason.OBSERVATION_UNAVAILABLE)
    try:
        marker_content = active.read_bytes()
        raw: Any = json.loads(marker_content)
    except OSError:
        return _unknown(WaitReason.OBSERVATION_UNAVAILABLE)
    except (json.JSONDecodeError, UnicodeError):
        return _unknown(WaitReason.OBSERVATION_UNAVAILABLE)
    try:
        marker = read_stored_model(ActivationMarker, raw)
    except ValidationError:
        return _unknown(WaitReason.OBSERVATION_UNAVAILABLE)
    if (
        marker.directory != f"{marker.generation:08d}-{marker.manifest_sha256}"
        or marker_content != marker.canonical_bytes()
    ):
        return _unknown(WaitReason.OBSERVATION_UNAVAILABLE)

    routes_document: RouteBundleDocument | None = None
    litellm_document: LiteLlmConfig | None = None
    if verify_files:
        directory = generations / marker.directory
        if directory.is_symlink() or not directory.is_dir():
            return _unknown(WaitReason.OBSERVATION_UNAVAILABLE)
        manifest_document = marker.manifest_document()
        expected_files = {
            "manifest.json": (
                marker.manifest_sha256,
                manifest_document.canonical_bytes(),
            ),
            "routes.json": (marker.routes_sha256, None),
            "litellm.json": (marker.litellm_sha256, None),
        }
        for name, (digest, exact) in expected_files.items():
            target = directory / name
            if target.is_symlink() or not target.is_file():
                return _unknown(WaitReason.OBSERVATION_UNAVAILABLE)
            try:
                content = target.read_bytes()
            except OSError:
                return _unknown(WaitReason.OBSERVATION_UNAVAILABLE)
            if _sha256(content) != digest or (exact is not None and content != exact):
                return _unknown(WaitReason.OBSERVATION_UNAVAILABLE)
            if validate_documents and name in {"routes.json", "litellm.json"}:
                try:
                    if name == "routes.json":
                        routes_document = RouteBundleDocument.model_validate_json(
                            content
                        )
                    else:
                        litellm_document = LiteLlmConfig.model_validate_json(content)
                except ValueError:
                    return _unknown(WaitReason.OBSERVATION_UNAVAILABLE)

    return VerifiedRouteBundle(
        marker=marker,
        routes=routes_document,
        litellm=litellm_document,
    )
