"""Strict outbound HTTPS transport for the Vonk Forge agent protocol."""

from __future__ import annotations

import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
import platform
import re
import secrets
import ssl
import stat
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID
from vonk_agent_protocol import (
    AgentClaim,
    AgentProtocolError,
    canonical_message,
)

MAX_BODY_BYTES = 64 * 1024
_PROTOCOL_VERSION = 2
_CAPABILITIES = (
    "agent.rollback",
    "agent.update",
    "node.probe",
    "release.install",
    "workload.health",
    "workload.prepare",
    "workload.start",
    "workload.stop",
    "workload.verify",
)
_JSON_CONTENT_TYPE = re.compile(r"application/json(?:;\s*charset=utf-8)?\Z")
_DNS_HOST = re.compile(
    r"(?=.{1,253}\Z)"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)"
    r"(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*\Z"
)
_SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_PREFIXED_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class AgentClientError(RuntimeError):
    """Base class for bounded outbound protocol failures."""


class AgentTransportError(AgentClientError):
    """A retryable connection, timeout, or server-availability failure."""


class AgentProtocolResponseError(AgentClientError):
    """The control plane returned bytes outside the protocol contract."""


class AgentPermanentError(AgentClientError):
    """The control plane permanently rejected a request."""


class AgentAuthenticationError(AgentPermanentError):
    """The configured agent identity is not authorized."""


@dataclass(frozen=True)
class CredentialSnapshot:
    ca_path: Path
    certificate_path: Path | None
    private_key_path: Path | None
    generation: int = 1
    not_before: datetime | None = None
    not_after: datetime | None = None


class CredentialProvider(Protocol):
    def snapshot(self) -> AbstractContextManager[CredentialSnapshot]: ...


@dataclass(frozen=True)
class AgentRuntimeIdentity:
    architecture: str
    platform_version: str
    build_digest: str
    active_slot: str
    agent_sha256: str
    supervisor_generation: int
    supervisor_ready_generation: int | None = None
    self_test_passed: bool = False

    def __post_init__(self) -> None:
        if (
            self.architecture not in {"linux-arm64", "linux-x86_64"}
            or _SEMVER.fullmatch(self.platform_version) is None
            or _PREFIXED_DIGEST.fullmatch(self.build_digest) is None
            or self.active_slot not in {"A", "B"}
            or _DIGEST.fullmatch(self.agent_sha256) is None
            or isinstance(self.supervisor_generation, bool)
            or not isinstance(self.supervisor_generation, int)
            or not 1 <= self.supervisor_generation <= 999_999_999
            or (
                self.supervisor_ready_generation is not None
                and (
                    isinstance(self.supervisor_ready_generation, bool)
                    or not isinstance(self.supervisor_ready_generation, int)
                    or self.supervisor_ready_generation != self.supervisor_generation
                )
            )
            or not isinstance(self.self_test_passed, bool)
            or (self.self_test_passed and self.supervisor_ready_generation is None)
        ):
            raise ValueError("agent runtime identity is invalid")

    @classmethod
    def from_environment(
        cls, *, machine: Callable[[], str] = platform.machine
    ) -> AgentRuntimeIdentity:
        try:
            generation = int(os.environ["VONK_AGENT_SUPERVISOR_GENERATION"])
            architecture = {
                "aarch64": "linux-arm64",
                "arm64": "linux-arm64",
                "x86_64": "linux-x86_64",
                "amd64": "linux-x86_64",
            }[machine().lower()]
            return cls(
                architecture=architecture,
                platform_version=os.environ["VONK_AGENT_PLATFORM_VERSION"],
                build_digest=os.environ["VONK_AGENT_BUILD_DIGEST"],
                active_slot=os.environ["VONK_AGENT_SUPERVISOR_SLOT"],
                agent_sha256=os.environ["VONK_AGENT_SUPERVISOR_SHA256"],
                supervisor_generation=generation,
                supervisor_ready_generation=generation,
                self_test_passed=True,
            )
        except (AttributeError, KeyError, ValueError) as error:
            raise ValueError("verified agent runtime identity is unavailable") from error

    def wire(self) -> dict[str, object]:
        return {
            "active_slot": self.active_slot,
            "architecture": self.architecture,
            "agent_sha256": self.agent_sha256,
            "build_digest": self.build_digest,
            "platform_version": self.platform_version,
            "supervisor_generation": self.supervisor_generation,
            "supervisor_ready_generation": self.supervisor_ready_generation,
            "self_test_passed": self.self_test_passed,
        }


@dataclass(frozen=True)
class StaticCredentialProvider:
    """Seed identity supplied by the restrictive service configuration."""

    ca_path: Path
    certificate_path: Path | None
    private_key_path: Path | None

    @contextmanager
    def snapshot(self) -> Iterator[CredentialSnapshot]:
        certificate = (
            None if self.certificate_path is None else Path(self.certificate_path)
        )
        private_key = (
            None if self.private_key_path is None else Path(self.private_key_path)
        )
        try:
            with _stable_snapshot(
                Path(self.ca_path), certificate, private_key, generation=1
            ) as snapshot:
                yield snapshot
        except CredentialStoreError as error:
            raise AgentTransportError(
                "agent TLS credentials are unavailable"
            ) from error


@dataclass(frozen=True)
class EnrollmentPending:
    id: str
    node_id: str
    state: str


@dataclass(frozen=True)
class IssuedCredential:
    node_id: str
    certificate_pem: bytes
    chain_pem: bytes
    serial: str
    fingerprint: str
    not_before: datetime
    not_after: datetime
    generation: int


class CredentialStoreError(RuntimeError):
    """The service-owned credential generation store is unsafe or corrupt."""


@dataclass(frozen=True)
class PendingRotation:
    node_id: str
    csr_pem: bytes
    purpose: str


class _GenerationProvider:
    def __init__(self, store: CredentialStore, generation: int) -> None:
        self._store = store
        self._generation = generation

    @contextmanager
    def snapshot(self) -> Iterator[CredentialSnapshot]:
        with self._store._generation_snapshot(self._generation) as snapshot:
            yield snapshot


class CredentialStore:
    """Crash-safe credential generations rooted beneath the agent state tree."""

    _DIRECTORY = "credentials"
    _PENDING_META = "pending.json"
    _PENDING_KEY = "pending-key.pem"
    _PENDING_CSR = "pending-csr.pem"
    _ACTIVE = "active.json"
    _STAGED = "staged.json"

    def __init__(
        self,
        state_root: Path,
        ca_path: Path,
        seed_certificate_path: Path,
        seed_private_key_path: Path,
    ) -> None:
        self._state_root = Path(state_root)
        self._ca_path = Path(ca_path)
        self._seed_certificate_path = Path(seed_certificate_path)
        self._seed_private_key_path = Path(seed_private_key_path)
        self._initialize()

    @property
    def active_generation(self) -> int:
        pointer = self._pointer(self._ACTIVE)
        return 1 if pointer is None else pointer

    @property
    def has_active_credentials(self) -> bool:
        if self._pointer(self._ACTIVE) is not None:
            return True
        certificate_exists = os.path.lexists(self._seed_certificate_path)
        key_exists = os.path.lexists(self._seed_private_key_path)
        if certificate_exists != key_exists:
            raise CredentialStoreError("seed certificate and key must be paired")
        return certificate_exists

    @property
    def has_published_credentials(self) -> bool:
        """Return whether a durable active generation pointer exists."""
        return self._pointer(self._ACTIVE) is not None

    @property
    def staged_generation(self) -> int | None:
        return self._pointer(self._STAGED)

    @contextmanager
    def snapshot(self) -> Iterator[CredentialSnapshot]:
        generation = self.active_generation
        if generation == 1 and self._pointer(self._ACTIVE) is None:
            if not self.has_active_credentials:
                with _stable_snapshot(
                    self._ca_path, None, None, generation=1
                ) as snapshot:
                    yield snapshot
                return
            with _stable_snapshot(
                self._ca_path,
                self._seed_certificate_path,
                self._seed_private_key_path,
                generation=1,
            ) as snapshot:
                yield snapshot
            return
        with self._generation_snapshot(generation) as snapshot:
            yield snapshot

    def staged_provider(self) -> CredentialProvider | None:
        generation = self.staged_generation
        return None if generation is None else _GenerationProvider(self, generation)

    def renewal_due(self, now: datetime) -> bool:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("renewal time must be timezone-aware")
        with self.snapshot() as snapshot:
            if snapshot.not_before is None or snapshot.not_after is None:
                raise CredentialStoreError("active credential validity is unavailable")
            lifetime = snapshot.not_after - snapshot.not_before
            return now.astimezone(UTC) >= snapshot.not_after - lifetime / 3

    def prepare_rotation(self, node_id: str) -> PendingRotation:
        return self._prepare_pending(node_id, "rotation")

    def prepare_enrollment(self, node_id: str) -> PendingRotation:
        return self._prepare_pending(node_id, "enrollment")

    def _prepare_pending(self, node_id: str, purpose: str) -> PendingRotation:
        if re.fullmatch(r"spk_[0-9a-f]{32}", node_id) is None:
            raise ValueError("node ID is not canonical")
        if purpose not in {"enrollment", "rotation"}:
            raise ValueError("credential request purpose is invalid")
        existing = self.pending_rotation()
        if existing is not None:
            if existing.node_id != node_id or existing.purpose != purpose:
                raise CredentialStoreError("pending credential request conflicts")
            return existing
        key = ed25519.Ed25519PrivateKey.generate()
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, node_id)]))
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.UniformResourceIdentifier(
                            f"spiffe://vonk-forge.local/node/{node_id}"
                        )
                    ]
                ),
                critical=False,
            )
            .sign(key, algorithm=None)
            .public_bytes(serialization.Encoding.PEM)
        )
        private_key = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        descriptor = self._credentials_descriptor()
        try:
            for name in (self._PENDING_META, self._PENDING_KEY, self._PENDING_CSR):
                _unlink_optional(descriptor, name)
            _atomic_write(descriptor, self._PENDING_KEY, private_key, 0o600)
            _atomic_write(descriptor, self._PENDING_CSR, csr, 0o600)
            _atomic_write(
                descriptor,
                self._PENDING_META,
                canonical_message(
                    {
                        "csr_sha256": hashlib.sha256(csr).hexdigest(),
                        "node_id": node_id,
                        "purpose": purpose,
                    }
                ),
                0o600,
            )
        finally:
            os.close(descriptor)
        return PendingRotation(node_id, csr, purpose)

    def pending_rotation(self) -> PendingRotation | None:
        descriptor = self._credentials_descriptor()
        try:
            raw = _read_optional(descriptor, self._PENDING_META, 4096, mode=0o600)
            if raw is None:
                return None
            document = _strict_document(raw)
            if not isinstance(document, dict) or set(document) != {
                "csr_sha256",
                "node_id",
                "purpose",
            }:
                raise CredentialStoreError("pending rotation metadata is invalid")
            node_id = document["node_id"]
            digest = document["csr_sha256"]
            purpose = document["purpose"]
            if (
                not isinstance(node_id, str)
                or re.fullmatch(r"spk_[0-9a-f]{32}", node_id) is None
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or purpose not in {"enrollment", "rotation"}
            ):
                raise CredentialStoreError("pending rotation metadata is invalid")
            csr = _read_required(descriptor, self._PENDING_CSR, 16 * 1024, mode=0o600)
            key = _read_required(descriptor, self._PENDING_KEY, 16 * 1024, mode=0o600)
            if hashlib.sha256(csr).hexdigest() != digest:
                raise CredentialStoreError("pending rotation metadata is invalid")
            request = x509.load_pem_x509_csr(csr)
            private_key = serialization.load_pem_private_key(key, password=None)
            if not isinstance(private_key, ed25519.Ed25519PrivateKey):
                raise CredentialStoreError("pending rotation private key is invalid")
            if request.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            ) != private_key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            ):
                raise CredentialStoreError("pending rotation key does not match CSR")
            return PendingRotation(node_id, csr, purpose)
        except CredentialStoreError:
            raise
        except (OSError, TypeError, ValueError) as error:
            raise CredentialStoreError("pending rotation is invalid") from error
        finally:
            os.close(descriptor)

    def stage(self, issued: IssuedCredential) -> None:
        if not isinstance(issued, IssuedCredential):
            raise CredentialStoreError("issued credential is invalid")
        pending = self.pending_rotation()
        if (
            pending is None
            or pending.node_id != issued.node_id
            or pending.purpose != "rotation"
        ):
            raise CredentialStoreError("issued credential has no pending rotation")
        if issued.generation <= self.active_generation:
            raise CredentialStoreError("issued credential generation is stale")
        descriptor = self._credentials_descriptor()
        temporary = f".generation-{issued.generation:08d}-{secrets.token_hex(8)}"
        final = _generation_name(issued.generation)
        temp_descriptor = -1
        try:
            key_bytes = _read_required(
                descriptor, self._PENDING_KEY, 16 * 1024, mode=0o600
            )
            _validate_issued_key(issued, key_bytes)
            try:
                os.mkdir(temporary, 0o700, dir_fd=descriptor)
                temp_descriptor = os.open(
                    temporary,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=descriptor,
                )
                _write_new(temp_descriptor, "private-key.pem", key_bytes, 0o600)
                _write_new(
                    temp_descriptor,
                    "certificate.pem",
                    issued.certificate_pem + issued.chain_pem,
                    0o600,
                )
                _write_new(
                    temp_descriptor,
                    "credential.json",
                    canonical_message(
                        {
                            "fingerprint": issued.fingerprint,
                            "generation": issued.generation,
                            "node_id": issued.node_id,
                            "not_after": issued.not_after.isoformat(),
                            "not_before": issued.not_before.isoformat(),
                            "serial": issued.serial,
                        }
                    ),
                    0o600,
                )
                os.fsync(temp_descriptor)
                os.close(temp_descriptor)
                temp_descriptor = -1
                os.rename(
                    temporary, final, src_dir_fd=descriptor, dst_dir_fd=descriptor
                )
                os.fsync(descriptor)
            except FileExistsError:
                if temp_descriptor >= 0:
                    os.close(temp_descriptor)
                    temp_descriptor = -1
                _remove_generation(descriptor, temporary)
                self._verify_generation(issued.generation, expected=issued)
            _atomic_write(
                descriptor,
                self._STAGED,
                canonical_message({"generation": issued.generation}),
                0o600,
            )
        except CredentialStoreError:
            if temp_descriptor >= 0:
                os.close(temp_descriptor)
            _remove_generation(descriptor, temporary)
            raise
        except OSError as error:
            if temp_descriptor >= 0:
                os.close(temp_descriptor)
            _remove_generation(descriptor, temporary)
            raise CredentialStoreError(
                "credential generation could not be staged"
            ) from error
        finally:
            os.close(descriptor)

    def install_initial(self, issued: IssuedCredential) -> None:
        """Durably publish an enrollment-issued generation one identity."""
        if not isinstance(issued, IssuedCredential) or issued.generation != 1:
            raise CredentialStoreError("initial credential generation is invalid")
        active = self._pointer(self._ACTIVE)
        if active is not None:
            if active != 1:
                raise CredentialStoreError("an active credential already exists")
            self._verify_generation(1, expected=issued)
            self.recover_initial_enrollment(issued.node_id)
            return
        pending = self.pending_rotation()
        if (
            pending is None
            or pending.node_id != issued.node_id
            or pending.purpose != "enrollment"
        ):
            raise CredentialStoreError("issued credential has no pending enrollment")
        _validate_issued_authority(issued, self._ca_path)
        descriptor = self._credentials_descriptor()
        temporary = f".generation-{issued.generation:08d}-{secrets.token_hex(8)}"
        final = _generation_name(issued.generation)
        temp_descriptor = -1
        try:
            key_bytes = _read_required(
                descriptor, self._PENDING_KEY, 16 * 1024, mode=0o600
            )
            _validate_issued_key(issued, key_bytes)
            try:
                os.mkdir(temporary, 0o700, dir_fd=descriptor)
                temp_descriptor = os.open(
                    temporary,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=descriptor,
                )
                _write_new(temp_descriptor, "private-key.pem", key_bytes, 0o600)
                _write_new(
                    temp_descriptor,
                    "certificate.pem",
                    issued.certificate_pem + issued.chain_pem,
                    0o600,
                )
                _write_new(
                    temp_descriptor,
                    "credential.json",
                    canonical_message(
                        {
                            "fingerprint": issued.fingerprint,
                            "generation": 1,
                            "node_id": issued.node_id,
                            "not_after": issued.not_after.isoformat(),
                            "not_before": issued.not_before.isoformat(),
                            "serial": issued.serial,
                        }
                    ),
                    0o600,
                )
                os.fsync(temp_descriptor)
                os.close(temp_descriptor)
                temp_descriptor = -1
                os.rename(
                    temporary, final, src_dir_fd=descriptor, dst_dir_fd=descriptor
                )
                os.fsync(descriptor)
            except FileExistsError:
                if temp_descriptor >= 0:
                    os.close(temp_descriptor)
                    temp_descriptor = -1
                _remove_generation(descriptor, temporary)
                self._verify_generation(1, expected=issued)
            _atomic_write(
                descriptor,
                self._ACTIVE,
                canonical_message({"generation": 1}),
                0o600,
            )
            for name in (
                self._STAGED,
                self._PENDING_KEY,
                self._PENDING_CSR,
                self._PENDING_META,
            ):
                _unlink_optional(descriptor, name)
            os.fsync(descriptor)
        except CredentialStoreError:
            if temp_descriptor >= 0:
                os.close(temp_descriptor)
            _remove_generation(descriptor, temporary)
            raise
        except OSError as error:
            if temp_descriptor >= 0:
                os.close(temp_descriptor)
            _remove_generation(descriptor, temporary)
            raise CredentialStoreError(
                "initial credential could not be installed"
            ) from error
        finally:
            os.close(descriptor)

    def recover_initial_enrollment(self, node_id: str) -> bool:
        """Remove only enrollment-pending material after generation one is active."""
        if self._pointer(self._ACTIVE) != 1:
            return False
        self._verify_generation(1)
        cleanup_identity = self._pending_cleanup_identity()
        if cleanup_identity != (node_id, "enrollment"):
            return False
        descriptor = self._credentials_descriptor()
        try:
            for name in (
                self._STAGED,
                self._PENDING_KEY,
                self._PENDING_CSR,
                self._PENDING_META,
            ):
                _unlink_optional(descriptor, name)
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return True

    def _pending_cleanup_identity(self) -> tuple[str, str] | None:
        descriptor = self._credentials_descriptor()
        try:
            raw = _read_optional(descriptor, self._PENDING_META, 4096, mode=0o600)
            if raw is None:
                return None
            document = _strict_document(raw)
            if (
                not isinstance(document, dict)
                or set(document) != {"csr_sha256", "node_id", "purpose"}
                or not isinstance(document["node_id"], str)
                or re.fullmatch(r"spk_[0-9a-f]{32}", document["node_id"]) is None
                or not isinstance(document["csr_sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", document["csr_sha256"]) is None
                or document["purpose"] not in {"enrollment", "rotation"}
            ):
                raise CredentialStoreError("pending credential metadata is invalid")
            return document["node_id"], document["purpose"]
        finally:
            os.close(descriptor)

    def publish_active(self, generation: int) -> None:
        if self.staged_generation != generation:
            raise CredentialStoreError("credential generation is not staged")
        self._verify_generation(generation)
        descriptor = self._credentials_descriptor()
        try:
            _atomic_write(
                descriptor,
                self._ACTIVE,
                canonical_message({"generation": generation}),
                0o600,
            )
            for name in (
                self._STAGED,
                self._PENDING_META,
                self._PENDING_KEY,
                self._PENDING_CSR,
            ):
                _unlink_optional(descriptor, name)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _initialize(self) -> None:
        root = _open_state_root(self._state_root, create=True)
        try:
            try:
                os.mkdir(self._DIRECTORY, 0o700, dir_fd=root)
            except FileExistsError:
                pass
            descriptor = os.open(
                self._DIRECTORY,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=root,
            )
            try:
                metadata = os.fstat(descriptor)
                if (
                    metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o700
                ):
                    raise CredentialStoreError("credential state directory is unsafe")
                removed = False
                for name in os.listdir(descriptor):
                    if re.fullmatch(r"\.generation-[0-9]{8}-[0-9a-f]{16}", name):
                        _remove_generation(descriptor, name)
                        removed = True
                if removed:
                    os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise CredentialStoreError(
                "credential state directory is unavailable"
            ) from error
        finally:
            os.close(root)
        self._pointer(self._ACTIVE)
        self._pointer(self._STAGED)
        if self.active_generation != 1 or self._pointer(self._ACTIVE) is not None:
            self._verify_generation(self.active_generation)
        if self.staged_generation is not None:
            self._verify_generation(self.staged_generation)
        cleanup = self._pending_cleanup_identity()
        if not (
            self._pointer(self._ACTIVE) == 1
            and cleanup is not None
            and cleanup[1] == "enrollment"
        ):
            self.pending_rotation()

    def _credentials_descriptor(self) -> int:
        root = _open_state_root(self._state_root, create=False)
        try:
            descriptor = os.open(
                self._DIRECTORY,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=root,
            )
        except OSError as error:
            os.close(root)
            raise CredentialStoreError(
                "credential state directory is unavailable"
            ) from error
        os.close(root)
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            os.close(descriptor)
            raise CredentialStoreError("credential state directory is unsafe")
        return descriptor

    def _pointer(self, name: str) -> int | None:
        descriptor = self._credentials_descriptor()
        try:
            raw = _read_optional(descriptor, name, 1024, mode=0o600)
            if raw is None:
                return None
            document = _strict_document(raw)
            generation = (
                document.get("generation") if isinstance(document, dict) else None
            )
            if (
                not isinstance(document, dict)
                or set(document) != {"generation"}
                or not isinstance(generation, int)
                or isinstance(generation, bool)
                or generation < 1
            ):
                raise CredentialStoreError("credential generation pointer is invalid")
            return generation
        finally:
            os.close(descriptor)

    def _verify_generation(
        self,
        generation: int,
        *,
        expected: IssuedCredential | None = None,
    ) -> dict[str, object]:
        descriptor = self._credentials_descriptor()
        generation_descriptor = -1
        try:
            generation_descriptor = os.open(
                _generation_name(generation),
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            metadata = os.fstat(generation_descriptor)
            if (
                metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise CredentialStoreError("credential generation directory is unsafe")
            document = _strict_document(
                _read_required(
                    generation_descriptor, "credential.json", 4096, mode=0o600
                )
            )
            required = {
                "fingerprint",
                "generation",
                "node_id",
                "not_after",
                "not_before",
                "serial",
            }
            if not isinstance(document, dict) or set(document) != required:
                raise CredentialStoreError("credential generation metadata is invalid")
            if document["generation"] != generation:
                raise CredentialStoreError("credential generation metadata is invalid")
            _read_required(
                generation_descriptor, "certificate.pem", MAX_BODY_BYTES, mode=0o600
            )
            key_bytes = _read_required(
                generation_descriptor, "private-key.pem", 16 * 1024, mode=0o600
            )
            if expected is not None and document != {
                "fingerprint": expected.fingerprint,
                "generation": expected.generation,
                "node_id": expected.node_id,
                "not_after": expected.not_after.isoformat(),
                "not_before": expected.not_before.isoformat(),
                "serial": expected.serial,
            }:
                raise CredentialStoreError(
                    "staged generation conflicts with server response"
                )
            if expected is not None:
                certificate_bytes = _read_required(
                    generation_descriptor,
                    "certificate.pem",
                    MAX_BODY_BYTES,
                    mode=0o600,
                )
                if certificate_bytes != expected.certificate_pem + expected.chain_pem:
                    raise CredentialStoreError(
                        "staged generation conflicts with server response"
                    )
                _validate_issued_key(expected, key_bytes)
            return document
        except CredentialStoreError:
            raise
        except OSError as error:
            raise CredentialStoreError(
                "credential generation is unavailable"
            ) from error
        finally:
            if generation_descriptor >= 0:
                os.close(generation_descriptor)
            os.close(descriptor)

    @contextmanager
    def _generation_snapshot(self, generation: int) -> Iterator[CredentialSnapshot]:
        document = self._verify_generation(generation)
        root = self._state_root / self._DIRECTORY / _generation_name(generation)
        try:
            not_before = datetime.fromisoformat(str(document["not_before"]))
            not_after = datetime.fromisoformat(str(document["not_after"]))
        except ValueError as error:
            raise CredentialStoreError(
                "credential generation validity is invalid"
            ) from error
        with _stable_snapshot(
            self._ca_path,
            root / "certificate.pem",
            root / "private-key.pem",
            generation=generation,
            validity=(not_before, not_after),
        ) as snapshot:
            yield snapshot


def _generation_name(generation: int) -> str:
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or not 1 <= generation <= 99999999
    ):
        raise CredentialStoreError("credential generation is invalid")
    return f"generation-{generation:08d}"


def _open_state_root(path: Path, *, create: bool) -> int:
    if not path.is_absolute() or not path.parts[1:]:
        raise CredentialStoreError("state root is invalid")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for index, component in enumerate(path.parts[1:]):
            final = index == len(path.parts[1:]) - 1
            try:
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o700 if final else 0o755, dir_fd=descriptor)
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=descriptor,
                )
            os.close(descriptor)
            descriptor = child
            metadata = os.fstat(descriptor)
            mode = stat.S_IMODE(metadata.st_mode)
            if final:
                if metadata.st_uid != os.geteuid() or mode != 0o700:
                    raise CredentialStoreError("state root is unsafe")
            elif metadata.st_uid not in {0, os.geteuid()} or (
                mode & 0o022 and not mode & stat.S_ISVTX
            ):
                raise CredentialStoreError("state root ancestry is unsafe")
        return descriptor
    except CredentialStoreError:
        os.close(descriptor)
        raise
    except OSError as error:
        os.close(descriptor)
        raise CredentialStoreError("state root is unavailable") from error


def _read_optional(
    directory: int,
    name: str,
    maximum: int,
    *,
    mode: int,
) -> bytes | None:
    try:
        return _read_required(directory, name, maximum, mode=mode)
    except FileNotFoundError:
        return None


def _read_required(directory: int, name: str, maximum: int, *, mode: int) -> bytes:
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=directory,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != mode
            or metadata.st_size > maximum
        ):
            raise CredentialStoreError("credential state file is unsafe")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        value = b"".join(chunks)
        if len(value) > maximum:
            raise CredentialStoreError("credential state file is too large")
        return value
    finally:
        os.close(descriptor)


def _strict_document(raw: bytes) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise CredentialStoreError(
                    "credential metadata contains duplicate fields"
                )
            result[key] = value
        return result

    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except CredentialStoreError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise CredentialStoreError("credential metadata is invalid") from error
    if canonical_message(document) != raw:
        raise CredentialStoreError("credential metadata is not canonical")
    return document


def _write_all(descriptor: int, value: bytes) -> None:
    offset = 0
    while offset < len(value):
        offset += os.write(descriptor, value[offset:])


def _write_new(directory: int, name: str, value: bytes, mode: int) -> None:
    if len(value) > MAX_BODY_BYTES:
        raise CredentialStoreError("credential file is too large")
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        mode,
        dir_fd=directory,
    )
    try:
        os.fchmod(descriptor, mode)
        _write_all(descriptor, value)
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        if (
            metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != mode
        ):
            raise CredentialStoreError("credential file publication is unsafe")
    finally:
        os.close(descriptor)


def _atomic_write(directory: int, name: str, value: bytes, mode: int) -> None:
    temporary = f".{name}.{uuid.uuid4()}"
    try:
        _write_new(directory, temporary, value, mode)
        os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    except Exception:
        _unlink_optional(directory, temporary)
        raise


def _unlink_optional(directory: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory)
    except FileNotFoundError:
        pass


def _remove_generation(directory: int, name: str) -> None:
    try:
        child = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory,
        )
    except FileNotFoundError:
        return
    except OSError as error:
        raise CredentialStoreError("credential staging directory is unsafe") from error
    try:
        for leaf in ("private-key.pem", "certificate.pem", "credential.json"):
            _unlink_optional(child, leaf)
    finally:
        os.close(child)
    try:
        os.rmdir(name, dir_fd=directory)
    except FileNotFoundError:
        pass


def _validate_issued_key(issued: IssuedCredential, private_key_pem: bytes) -> None:
    try:
        certificate = x509.load_pem_x509_certificate(issued.certificate_pem)
        private_key = serialization.load_pem_private_key(private_key_pem, password=None)
        if not isinstance(private_key, ed25519.Ed25519PrivateKey):
            raise TypeError
        certificate_public = certificate.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        key_public = private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issued.node_id)])
        sans = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
        expected_sans = x509.SubjectAlternativeName(
            [
                x509.UniformResourceIdentifier(
                    f"spiffe://vonk-forge.local/node/{issued.node_id}"
                )
            ]
        )
        if (
            certificate_public != key_public
            or certificate.subject != subject
            or sans != expected_sans
            or certificate.not_valid_before_utc != issued.not_before
            or certificate.not_valid_after_utc != issued.not_after
            or str(certificate.serial_number) != issued.serial
            or hashlib.sha256(
                certificate.public_bytes(serialization.Encoding.DER)
            ).hexdigest()
            != issued.fingerprint
            or len(issued.certificate_pem) + len(issued.chain_pem) > MAX_BODY_BYTES
        ):
            raise ValueError
    except (TypeError, ValueError, x509.ExtensionNotFound) as error:
        raise CredentialStoreError(
            "issued credential does not match pending key"
        ) from error


def _validate_issued_authority(issued: IssuedCredential, ca_path: Path) -> None:
    try:
        with _stable_snapshot(ca_path, None, None, generation=1) as snapshot:
            ca = x509.load_pem_x509_certificate(snapshot.ca_path.read_bytes())
        certificate = x509.load_pem_x509_certificate(issued.certificate_pem)
        chain_ca = x509.load_pem_x509_certificate(issued.chain_pem)
        if chain_ca.fingerprint(hashes.SHA256()) != ca.fingerprint(hashes.SHA256()):
            raise ValueError
        certificate.verify_directly_issued_by(ca)
        now = datetime.now(UTC)
        if not issued.not_before <= now < issued.not_after:
            raise ValueError
    except (CredentialStoreError, TypeError, ValueError) as error:
        raise CredentialStoreError("issued credential authority is invalid") from error


@contextmanager
def _stable_snapshot(
    ca_path: Path,
    certificate_path: Path | None,
    private_key_path: Path | None,
    *,
    generation: int,
    validity: tuple[datetime, datetime] | None = None,
) -> Iterator[CredentialSnapshot]:
    if (certificate_path is None) != (private_key_path is None):
        raise CredentialStoreError("certificate and private key must be paired")
    descriptors: list[int] = []
    try:
        ca = _snapshot_source(ca_path, private=False)
        descriptors.append(ca)
        certificate = key = None
        not_before = not_after = None
        if certificate_path is not None and private_key_path is not None:
            certificate = _snapshot_source(certificate_path, private=False)
            descriptors.append(certificate)
            key = _snapshot_source(private_key_path, private=True)
            descriptors.append(key)
            if validity is None:
                try:
                    parsed = x509.load_pem_x509_certificate(
                        _read_descriptor(certificate, MAX_BODY_BYTES)
                    )
                except ValueError as error:
                    raise CredentialStoreError(
                        "certificate snapshot is invalid"
                    ) from error
                not_before = parsed.not_valid_before_utc
                not_after = parsed.not_valid_after_utc
            else:
                not_before, not_after = validity
        yield CredentialSnapshot(
            ca_path=Path(f"/proc/self/fd/{ca}"),
            certificate_path=None
            if certificate is None
            else Path(f"/proc/self/fd/{certificate}"),
            private_key_path=None if key is None else Path(f"/proc/self/fd/{key}"),
            generation=generation,
            not_before=not_before,
            not_after=not_after,
        )
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def _read_descriptor(descriptor: int, maximum: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    value = os.read(descriptor, maximum + 1)
    os.lseek(descriptor, 0, os.SEEK_SET)
    if len(value) > maximum:
        raise CredentialStoreError("credential snapshot is too large")
    return value


def _snapshot_source(path: Path, *, private: bool) -> int:
    if not path.is_absolute() or len(path.parts) < 2:
        raise CredentialStoreError("credential path is invalid")
    parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    source = snapshot = -1
    try:
        for component in path.parts[1:-1]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent,
            )
            os.close(parent)
            parent = child
            metadata = os.fstat(parent)
            mode = stat.S_IMODE(metadata.st_mode)
            if metadata.st_uid not in {0, os.geteuid()} or (
                mode & 0o022 and not mode & stat.S_ISVTX
            ):
                raise CredentialStoreError("credential path ancestry is unsafe")
        source = os.open(
            path.name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent,
        )
        before = os.fstat(source)
        mode = stat.S_IMODE(before.st_mode)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid not in {0, os.geteuid()}
            or before.st_nlink != 1
            or (mode & 0o077 if private else mode & 0o022)
            or before.st_size > MAX_BODY_BYTES
        ):
            raise CredentialStoreError("credential source is unsafe")
        snapshot = os.memfd_create(
            "vonk-agent-credential", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
        )
        remaining = before.st_size
        while remaining:
            chunk = os.read(source, min(64 * 1024, remaining))
            if not chunk:
                raise CredentialStoreError("credential source changed during snapshot")
            _write_all(snapshot, chunk)
            remaining -= len(chunk)
        if os.read(source, 1):
            raise CredentialStoreError("credential source changed during snapshot")
        after = os.fstat(source)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise CredentialStoreError("credential source changed during snapshot")
        fcntl.fcntl(
            snapshot,
            fcntl.F_ADD_SEALS,
            fcntl.F_SEAL_SEAL
            | fcntl.F_SEAL_SHRINK
            | fcntl.F_SEAL_GROW
            | fcntl.F_SEAL_WRITE,
        )
        os.lseek(snapshot, 0, os.SEEK_SET)
        result, snapshot = snapshot, -1
        return result
    except CredentialStoreError:
        raise
    except OSError as error:
        raise CredentialStoreError("credential source is unavailable") from error
    finally:
        if snapshot >= 0:
            os.close(snapshot)
        if source >= 0:
            os.close(source)
        os.close(parent)


@dataclass(frozen=True)
class _Response:
    status: int
    body: bytes
    document: Any | None


class AgentClient:
    """One-request-per-connection mTLS client with fixed agent API paths."""

    def __init__(
        self,
        runtime_origin: str,
        node_id: str,
        credentials: CredentialProvider,
        *,
        connect_timeout: float = 5,
        read_timeout: float = 10,
        long_poll_seconds: int = 30,
        lease_seconds: int = 60,
        max_body_bytes: int = MAX_BODY_BYTES,
        runtime_identity: AgentRuntimeIdentity | None = None,
    ) -> None:
        self._runtime_origin = _origin(runtime_origin)
        if (
            not isinstance(node_id, str)
            or re.fullmatch(r"spk_[0-9a-f]{32}", node_id) is None
        ):
            raise ValueError("node ID is not canonical")
        if (
            connect_timeout <= 0
            or read_timeout <= 0
            or not isinstance(long_poll_seconds, int)
            or isinstance(long_poll_seconds, bool)
            or not 0 <= long_poll_seconds <= 60
            or not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or not 1 <= lease_seconds <= 300
            or not 1 <= max_body_bytes <= MAX_BODY_BYTES
        ):
            raise ValueError("client bounds are invalid")
        self._node_id = node_id
        self._credentials = credentials
        self._connect_timeout = float(connect_timeout)
        self._read_timeout = float(read_timeout)
        self._long_poll_seconds = long_poll_seconds
        self._lease_seconds = lease_seconds
        self._max_body_bytes = max_body_bytes
        if runtime_identity is not None and not isinstance(
            runtime_identity, AgentRuntimeIdentity
        ):
            raise ValueError("agent runtime identity is invalid")
        self._runtime_identity = runtime_identity

    def claim(self) -> AgentClaim | None:
        body: dict[str, object] = {
            "agent_implementation": "python",
            "capabilities": list(_CAPABILITIES),
            "lease_seconds": self._lease_seconds,
            "node_id": self._node_id,
            "protocol_version": _PROTOCOL_VERSION,
            "wait_seconds": self._long_poll_seconds,
        }
        if self._runtime_identity is not None:
            body["runtime_identity"] = self._runtime_identity.wire()
        response = self._post(
            self._runtime_origin,
            "/agent/v1/claim",
            body,
            use_client_identity=True,
            response_timeout=self._read_timeout + self._long_poll_seconds,
        )
        if response.status == 204:
            _require_empty(response)
            return None
        _require_status(response, {200})
        try:
            return AgentClaim.parse(response.document)
        except (AgentProtocolError, TypeError, ValueError) as error:
            raise AgentProtocolResponseError(
                "claim response violates the protocol contract"
            ) from error

    def enroll(
        self,
        enrollment_origin: str,
        grant_token: str,
        csr: bytes,
        evidence: Mapping[str, object],
    ) -> EnrollmentPending | IssuedCredential:
        if (
            not isinstance(grant_token, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{43}", grant_token) is None
        ):
            raise ValueError("enrollment grant token is invalid")
        try:
            csr_text = csr.decode("ascii")
        except (AttributeError, UnicodeDecodeError) as error:
            raise ValueError("enrollment CSR must be ASCII PEM") from error
        if not csr_text or len(csr) > 16 * 1024:
            raise ValueError("enrollment CSR is invalid")
        response = self._post(
            _origin(enrollment_origin),
            "/agent/v1/enroll",
            {"grant_token": grant_token, "csr": csr_text, "evidence": evidence},
            use_client_identity=False,
        )
        _require_status(response, {200, 202})
        if response.status == 202:
            return _pending(response.document, self._node_id)
        return _issued(response.document, self._node_id)

    def heartbeat(self, progress: Any) -> Any:
        from vonk_agent_protocol import AgentDirective, AgentProgress

        if not isinstance(progress, AgentProgress):
            raise TypeError("progress message is invalid")
        response = self._post(
            self._runtime_origin,
            "/agent/v1/heartbeat",
            progress,
            use_client_identity=True,
        )
        _require_status(response, {200})
        try:
            return AgentDirective.parse(response.document)
        except (AgentProtocolError, TypeError, ValueError) as error:
            raise AgentProtocolResponseError(
                "heartbeat response violates the protocol contract"
            ) from error

    def result(self, result: Any) -> None:
        from vonk_agent_protocol import AgentResult

        if not isinstance(result, AgentResult):
            raise TypeError("result message is invalid")
        response = self._post(
            self._runtime_origin,
            "/agent/v1/result",
            result,
            use_client_identity=True,
        )
        _require_status(response, {204, 409})
        if response.status == 204:
            _require_empty(response)

    def renew(self, csr: bytes) -> IssuedCredential:
        try:
            csr_text = csr.decode("ascii")
        except (AttributeError, UnicodeDecodeError) as error:
            raise ValueError("renewal CSR must be ASCII PEM") from error
        if not csr_text or len(csr) > 16 * 1024:
            raise ValueError("renewal CSR is invalid")
        response = self._post(
            self._runtime_origin,
            "/agent/v1/renew",
            {"csr": csr_text, "node_id": self._node_id},
            use_client_identity=True,
        )
        _require_status(response, {200})
        return _issued(response.document, self._node_id)

    def activate(self, generation: int, credentials: CredentialProvider) -> None:
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
        ):
            raise ValueError("credential generation is invalid")
        response = self._post(
            self._runtime_origin,
            "/agent/v1/renew/activate",
            {"generation": generation, "node_id": self._node_id},
            use_client_identity=True,
            credentials=credentials,
        )
        _require_status(response, {204})
        _require_empty(response)

    def package_helper_receipts(self, payload: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
        """Fetch control-signed object receipts for one active package attempt."""
        response = self._post(
            self._runtime_origin,
            "/agent/v1/package-helper/receipts",
            payload,
            use_client_identity=True,
        )
        _require_status(response, {200})
        document = response.document
        if not isinstance(document, Mapping) or set(document) != {"receipts"}:
            raise AgentProtocolResponseError("package helper receipts response is invalid")
        receipts = document.get("receipts")
        if not isinstance(receipts, list) or not 1 <= len(receipts) <= 256:
            raise AgentProtocolResponseError("package helper receipts response is invalid")
        if not all(isinstance(item, Mapping) for item in receipts):
            raise AgentProtocolResponseError("package helper receipts response is invalid")
        return tuple(receipts)

    def package_helper_grant(self, payload: Mapping[str, object]) -> Mapping[str, object]:
        """Fetch one short-lived control-signed helper grant."""
        response = self._post(
            self._runtime_origin,
            "/agent/v1/package-helper/grant",
            payload,
            use_client_identity=True,
        )
        _require_status(response, {200})
        document = response.document
        if not isinstance(document, Mapping) or set(document) != {"grant"}:
            raise AgentProtocolResponseError("package helper grant response is invalid")
        grant = document.get("grant")
        if not isinstance(grant, Mapping):
            raise AgentProtocolResponseError("package helper grant response is invalid")
        return grant

    def _post(
        self,
        origin: str,
        path: str,
        payload: Mapping[str, object] | object,
        *,
        use_client_identity: bool,
        response_timeout: float | None = None,
        credentials: CredentialProvider | None = None,
    ) -> _Response:
        body = canonical_message(payload)
        if len(body) > self._max_body_bytes:
            raise AgentProtocolResponseError("request body is too large")
        provider = credentials or self._credentials
        with provider.snapshot() as snapshot:
            context = _ssl_context(snapshot, use_client_identity=use_client_identity)
            host, port = _endpoint(origin)
            connection = http.client.HTTPSConnection(
                host,
                port,
                timeout=self._connect_timeout,
                context=context,
            )
            response: http.client.HTTPResponse | None = None
            try:
                connection.connect()
                assert connection.sock is not None
                connection.sock.settimeout(response_timeout or self._read_timeout)
                connection.request(
                    "POST",
                    path,
                    body=body,
                    headers={
                        "Content-Type": "application/json",
                        "Content-Length": str(len(body)),
                    },
                )
                response = connection.getresponse()
                if _explicit_error_status(response.status):
                    return _Response(response.status, b"", None)
                raw = _read_bounded(response, self._max_body_bytes)
                document = None
                if raw:
                    document = _canonical_json(response, raw)
                elif response.status == 200:
                    raise AgentProtocolResponseError("JSON response body is empty")
                return _Response(response.status, raw, document)
            except AgentClientError:
                raise
            except (
                http.client.HTTPException,
                OSError,
                TimeoutError,
                ssl.SSLError,
            ) as error:
                raise AgentTransportError("control-plane transport failed") from error
            finally:
                if response is not None:
                    response.close()
                connection.close()


def _origin(value: str) -> str:
    if not isinstance(value, str) or any(ord(character) <= 32 for character in value):
        raise ValueError("origin is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("origin is invalid") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        raise ValueError("origin is invalid")
    host = parsed.hostname
    assert host is not None
    try:
        parsed_ip = ipaddress.ip_address(host)
        rendered_host = (
            f"[{parsed_ip.compressed}]" if parsed_ip.version == 6 else str(parsed_ip)
        )
    except ValueError:
        if (
            re.fullmatch(
                r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*",
                host,
            )
            or _DNS_HOST.fullmatch(host) is None
        ):
            raise ValueError("origin is invalid") from None
        rendered_host = host
    rendered = "https://" + rendered_host + ("" if port is None else f":{port}")
    if rendered != value:
        raise ValueError("origin is not canonical")
    return value


def _endpoint(origin: str) -> tuple[str, int | None]:
    parsed = urlsplit(origin)
    assert parsed.hostname is not None
    return parsed.hostname, parsed.port


def _ssl_context(
    credentials: CredentialSnapshot,
    *,
    use_client_identity: bool,
) -> ssl.SSLContext:
    try:
        context = ssl.create_default_context(
            ssl.Purpose.SERVER_AUTH,
            cafile=str(credentials.ca_path),
        )
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        if use_client_identity and (
            credentials.certificate_path is not None
            and credentials.private_key_path is not None
        ):
            context.load_cert_chain(
                str(credentials.certificate_path),
                str(credentials.private_key_path),
            )
        return context
    except (OSError, ssl.SSLError) as error:
        raise AgentTransportError("agent TLS credentials are unavailable") from error


def _read_bounded(response: http.client.HTTPResponse, maximum: int) -> bytes:
    content_length = response.getheader("content-length")
    if content_length is not None:
        try:
            advertised = int(content_length)
        except ValueError as error:
            raise AgentProtocolResponseError(
                "response content length is invalid"
            ) from error
        if advertised < 0 or advertised > maximum:
            raise AgentProtocolResponseError("response body is too large")
    body = response.read(maximum + 1)
    if len(body) > maximum:
        raise AgentProtocolResponseError("response body is too large")
    return body


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AgentProtocolResponseError("JSON response contains duplicate fields")
        result[key] = value
    return result


def _canonical_json(response: http.client.HTTPResponse, raw: bytes) -> Any:
    content_type = response.getheader("content-type")
    if content_type is None or _JSON_CONTENT_TYPE.fullmatch(content_type) is None:
        raise AgentProtocolResponseError("JSON response content type is invalid")
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs)
    except AgentProtocolResponseError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise AgentProtocolResponseError("JSON response is invalid") from error
    if canonical_message(document) != raw:
        raise AgentProtocolResponseError("JSON response is not canonical")
    return document


def _raise_status(response: _Response) -> None:
    if response.status in {408, 429} or response.status >= 500:
        raise AgentTransportError("control plane is temporarily unavailable")
    if response.status in {401, 403}:
        raise AgentAuthenticationError("agent identity is not authorized")
    if 400 <= response.status < 500:
        raise AgentPermanentError("control plane permanently rejected the request")


def _explicit_error_status(status: int) -> bool:
    return 400 <= status < 600


def _require_status(response: _Response, expected: set[int]) -> None:
    if response.status not in expected:
        _raise_status(response)
        raise AgentProtocolResponseError("control plane returned an unexpected status")


def _require_empty(response: _Response) -> None:
    if response.body:
        raise AgentProtocolResponseError("empty response status included a body")


def _pending(value: Any, node_id: str) -> EnrollmentPending:
    if not isinstance(value, dict) or set(value) != {"id", "node_id", "state"}:
        raise AgentProtocolResponseError("pending enrollment response is invalid")
    try:
        identifier = str(uuid.UUID(value["id"]))
    except (TypeError, ValueError, AttributeError) as error:
        raise AgentProtocolResponseError(
            "pending enrollment response is invalid"
        ) from error
    if (
        identifier != value["id"]
        or value["node_id"] != node_id
        or value["state"] not in {"pending-approval", "issuing"}
    ):
        raise AgentProtocolResponseError("pending enrollment response is invalid")
    return EnrollmentPending(identifier, node_id, value["state"])


def _issued(value: Any, node_id: str) -> IssuedCredential:
    required = {
        "node_id",
        "certificate_pem",
        "chain_pem",
        "serial",
        "fingerprint",
        "not_before",
        "not_after",
        "generation",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value.get("node_id") != node_id
    ):
        raise AgentProtocolResponseError("issued credential response is invalid")
    generation = value.get("generation")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
    ):
        raise AgentProtocolResponseError("issued credential response is invalid")
    try:
        certificate = value["certificate_pem"].encode("ascii")
        chain = value["chain_pem"].encode("ascii")
        not_before = datetime.fromisoformat(value["not_before"])
        not_after = datetime.fromisoformat(value["not_after"])
    except (AttributeError, TypeError, UnicodeEncodeError, ValueError) as error:
        raise AgentProtocolResponseError(
            "issued credential response is invalid"
        ) from error
    if (
        not certificate
        or not chain
        or len(certificate) > MAX_BODY_BYTES
        or len(chain) > MAX_BODY_BYTES
        or not isinstance(value["serial"], str)
        or not value["serial"].strip()
        or len(value["serial"]) > 128
        or not isinstance(value["fingerprint"], str)
        or not value["fingerprint"].strip()
        or len(value["fingerprint"]) > 128
        or not_before.tzinfo is None
        or not_before.utcoffset() != UTC.utcoffset(not_before)
        or not_after.tzinfo is None
        or not_after.utcoffset() != UTC.utcoffset(not_after)
        or not_after <= not_before
    ):
        raise AgentProtocolResponseError("issued credential response is invalid")
    return IssuedCredential(
        node_id=node_id,
        certificate_pem=certificate,
        chain_pem=chain,
        serial=value["serial"],
        fingerprint=value["fingerprint"],
        not_before=not_before,
        not_after=not_after,
        generation=generation,
    )
