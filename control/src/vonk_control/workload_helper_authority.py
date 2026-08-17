"""Distinct workload helper grant and object-receipt issuer."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import AgentProtocolError
from vonk_agent_protocol.workload_packages import (
    MAX_PACKAGE_HELPER_GRANT_SECONDS,
    PACKAGE_HELPER_AUTHORITY,
    PackageHelperGrantClaims,
    PackageHelperOperation,
    PackageHelperSignature,
    PackageObjectReceiptClaims,
    SignedPackageHelperGrant,
    SignedPackageObjectReceipt,
    package_helper_grant_signing_bytes,
    package_object_receipt_signing_bytes,
)

from .models import AgentOperation as StoredAgentOperation
from .models import AgentOperationAttempt


class WorkloadHelperAuthorityError(RuntimeError):
    """A helper authority input or signing boundary is invalid."""


def _load_private_key(path: Path) -> ed25519.Ed25519PrivateKey:
    descriptor = -1
    try:
        descriptor = os.open(
            Path(path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) & 0o077
            or not 1 <= before.st_size <= 16 * 1024
        ):
            raise WorkloadHelperAuthorityError(
                "workload helper private key is unsafe"
            )
        raw = os.read(descriptor, 16 * 1024 + 1)
        after = os.fstat(descriptor)
        identity = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
        if len(raw) > 16 * 1024 or identity(before) != identity(after):
            raise WorkloadHelperAuthorityError(
                "workload helper private key changed while read"
            )
    except WorkloadHelperAuthorityError:
        raise
    except OSError as error:
        raise WorkloadHelperAuthorityError(
            "workload helper private key is unavailable"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        key = serialization.load_pem_private_key(raw, password=None)
    except (TypeError, ValueError) as error:
        raise WorkloadHelperAuthorityError(
            "workload helper private key is invalid"
        ) from error
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise WorkloadHelperAuthorityError(
            "workload helper private key must be Ed25519"
        )
    return key


class WorkloadHelperGrantIssuer:
    """Issue only workload helper grants from the dedicated fence key."""

    def __init__(
        self,
        private_key: ed25519.Ed25519PrivateKey,
        *,
        clock: Callable[[], datetime] | None = None,
        request_id_factory: Callable[[], object] | None = None,
    ) -> None:
        if not isinstance(private_key, ed25519.Ed25519PrivateKey):
            raise TypeError("workload helper authority key must be Ed25519")
        if clock is not None and not callable(clock):
            raise TypeError("workload helper authority clock is invalid")
        if request_id_factory is not None and not callable(request_id_factory):
            raise TypeError("workload helper request ID factory is invalid")
        self._private_key = private_key
        self._clock = clock or (lambda: datetime.now(UTC))
        self._request_id_factory = request_id_factory or uuid4
        self.public_key = private_key.public_key()
        self.public_key_bytes = self.public_key.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        self.key_id = hashlib.sha256(self.public_key_bytes).hexdigest()

    @classmethod
    def from_private_key_file(
        cls,
        path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        request_id_factory: Callable[[], object] | None = None,
    ) -> WorkloadHelperGrantIssuer:
        return cls(
            _load_private_key(Path(path)),
            clock=clock,
            request_id_factory=request_id_factory,
        )

    def public_key_document(self) -> dict[str, object]:
        return {
            "algorithm": "ed25519",
            "authority": PACKAGE_HELPER_AUTHORITY,
            "key_id": self.key_id,
            "public_key": self.public_key_bytes.hex(),
            "schema_version": 1,
            "usage": "grant",
        }

    def issue_grant(
        self,
        *,
        request_id: object | None = None,
        node_id: object,
        job_id: object,
        operation_id: object,
        attempt: object,
        fence: object,
        release_digest: object,
        generation: object,
        operation: object,
        request_digest: object,
        expires_in_seconds: object,
    ) -> SignedPackageHelperGrant:
        if type(operation) is not PackageHelperOperation:
            raise WorkloadHelperAuthorityError("workload helper operation is invalid")
        if (
            not isinstance(expires_in_seconds, int)
            or isinstance(expires_in_seconds, bool)
            or not 1
            <= expires_in_seconds
            <= MAX_PACKAGE_HELPER_GRANT_SECONDS
        ):
            raise WorkloadHelperAuthorityError("workload helper grant expiry is invalid")
        now = self._now()
        try:
            if request_id is None:
                request_id = str(self._request_id_factory())
            claims = PackageHelperGrantClaims(
                schema_version=1,
                authority=PACKAGE_HELPER_AUTHORITY,
                request_id=request_id,
                node_id=node_id,
                job_id=job_id,
                operation_id=operation_id,
                attempt=attempt,
                fence=fence,
                release_digest=release_digest,
                generation=generation,
                operation=operation,
                request_digest=request_digest,
                issued_at=now,
                expires_at=now + expires_in_seconds,
            )
        except (AgentProtocolError, TypeError, ValueError) as error:
            raise WorkloadHelperAuthorityError(
                "workload helper grant binding is invalid"
            ) from error
        return SignedPackageHelperGrant(
            claims=claims,
            signature=PackageHelperSignature(
                algorithm="ed25519",
                key_id=self.key_id,
                value=self._private_key.sign(
                    package_helper_grant_signing_bytes(claims)
                ).hex(),
            ),
        )

    def _now(self) -> int:
        try:
            now = self._clock()
        except Exception as error:
            raise WorkloadHelperAuthorityError(
                "workload helper authority clock is unavailable"
            ) from error
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise WorkloadHelperAuthorityError(
                "workload helper authority clock must be timezone-aware"
            )
        return int(now.astimezone(UTC).timestamp())


class WorkloadObjectReceiptIssuer:
    """Issue only workload object receipts from an independent receipt key."""

    def __init__(self, private_key: ed25519.Ed25519PrivateKey) -> None:
        if not isinstance(private_key, ed25519.Ed25519PrivateKey):
            raise TypeError("workload object receipt key must be Ed25519")
        self._private_key = private_key
        self.public_key = private_key.public_key()
        self.public_key_bytes = self.public_key.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        self.key_id = hashlib.sha256(self.public_key_bytes).hexdigest()

    @classmethod
    def from_private_key_file(cls, path: Path) -> WorkloadObjectReceiptIssuer:
        return cls(_load_private_key(Path(path)))

    def public_key_document(self) -> dict[str, object]:
        return {
            "algorithm": "ed25519",
            "authority": PACKAGE_HELPER_AUTHORITY,
            "key_id": self.key_id,
            "public_key": self.public_key_bytes.hex(),
            "schema_version": 1,
            "usage": "object-receipt",
        }

    def issue_object_receipt(
        self, *, object_digest: object, size: object
    ) -> SignedPackageObjectReceipt:
        try:
            claims = PackageObjectReceiptClaims(
                schema_version=1,
                authority=PACKAGE_HELPER_AUTHORITY,
                object_digest=object_digest,
                size=size,
                relative_name=f"objects/sha256/{object_digest}",
            )
        except (AgentProtocolError, TypeError, ValueError) as error:
            raise WorkloadHelperAuthorityError(
                "workload object receipt binding is invalid"
            ) from error
        return SignedPackageObjectReceipt(
            claims=claims,
            signature=PackageHelperSignature(
                algorithm="ed25519",
                key_id=self.key_id,
                value=self._private_key.sign(
                    package_object_receipt_signing_bytes(claims)
                ).hex(),
            ),
        )


class WorkloadHelperAuthorityService:
    """Database-bound issuer used by the authenticated GPU node grant routes.

    The private keys remain control-plane secrets.  A GPU node can ask for a
    receipt only for an object present in the control plane's signed workload
    release target, and can ask for a grant only while it owns the exact
    running operation attempt.  This keeps the agent's fixed executor ABI
    generic without giving it a local signing capability.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        grant_issuer: WorkloadHelperGrantIssuer,
        receipt_issuer: WorkloadObjectReceiptIssuer,
        *,
        workload_target_root: Path,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(sessions):
            raise TypeError("workload helper sessions are invalid")
        if not isinstance(grant_issuer, WorkloadHelperGrantIssuer):
            raise TypeError("workload helper grant issuer is invalid")
        if not isinstance(receipt_issuer, WorkloadObjectReceiptIssuer):
            raise TypeError("workload helper receipt issuer is invalid")
        root = Path(workload_target_root)
        if not root.is_absolute():
            raise ValueError("workload target root must be absolute")
        if clock is not None and not callable(clock):
            raise TypeError("workload helper authority clock is invalid")
        self._sessions = sessions
        self._grant_issuer = grant_issuer
        self._receipt_issuer = receipt_issuer
        self._workload_target_root = root
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def grant_public_key_document(self) -> dict[str, object]:
        return self._grant_issuer.public_key_document()

    @property
    def receipt_public_key_document(self) -> dict[str, object]:
        return self._receipt_issuer.public_key_document()

    def issue_receipts(
        self,
        *,
        node_id: str,
        job_id: str,
        operation_id: str,
        attempt: int,
        fence: str,
        release_digest: str,
        objects: object,
        certificate_serial: str,
    ) -> tuple[SignedPackageObjectReceipt, ...]:
        self._check_attempt(
            node_id=node_id,
            job_id=job_id,
            operation_id=operation_id,
            attempt=attempt,
            fence=fence,
            release_digest=release_digest,
            certificate_serial=certificate_serial,
        )
        allowed = self._release_objects(release_digest)
        if not isinstance(objects, (list, tuple)) or not 1 <= len(objects) <= 256:
            raise WorkloadHelperAuthorityError("workload helper objects are invalid")
        result: list[SignedPackageObjectReceipt] = []
        seen: set[str] = set()
        for item in objects:
            if not isinstance(item, dict):
                raise WorkloadHelperAuthorityError("workload helper object is invalid")
            digest = item.get("object_digest")
            size = item.get("size")
            if digest in seen or digest not in allowed or allowed[digest] != size:
                raise WorkloadHelperAuthorityError("workload helper object is not authorized")
            seen.add(digest)
            result.append(
                self._receipt_issuer.issue_object_receipt(
                    object_digest=digest, size=size
                )
            )
        return tuple(result)

    def issue_grant(
        self,
        *,
        request_id: str,
        node_id: str,
        job_id: str,
        operation_id: str,
        attempt: int,
        fence: str,
        release_digest: str,
        generation: str,
        operation: object,
        request_digest: str,
        certificate_serial: str,
        expires_in_seconds: int = 30,
    ) -> SignedPackageHelperGrant:
        self._check_attempt(
            node_id=node_id,
            job_id=job_id,
            operation_id=operation_id,
            attempt=attempt,
            fence=fence,
            release_digest=release_digest,
            certificate_serial=certificate_serial,
        )
        return self._grant_issuer.issue_grant(
            request_id=request_id,
            node_id=node_id,
            job_id=job_id,
            operation_id=operation_id,
            attempt=attempt,
            fence=fence,
            release_digest=release_digest,
            generation=generation,
            operation=operation,
            request_digest=request_digest,
            expires_in_seconds=expires_in_seconds,
        )

    def _check_attempt(
        self,
        *,
        node_id: str,
        job_id: str,
        operation_id: str,
        attempt: int,
        fence: str,
        release_digest: str,
        certificate_serial: str,
    ) -> None:
        now = self._clock()
        with self._sessions() as session:
            operation = session.get(StoredAgentOperation, operation_id)
            current = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == operation_id,
                    AgentOperationAttempt.attempt == attempt,
                )
            )
            payload = operation.payload if operation is not None else None
            lease_deadline = (
                None
                if current is None
                else current.lease_deadline
                if current.lease_deadline.tzinfo is not None
                else current.lease_deadline.replace(tzinfo=UTC)
            )
            if (
                operation is None
                or current is None
                or operation.node_id != node_id
                or operation.parent_job_id != job_id
                or operation.state != "running"
                or operation.current_attempt != attempt
                or current.state != "running"
                or current.fence != fence
                or current.agent_certificate_serial != certificate_serial
                or lease_deadline is None
                or lease_deadline <= now
                or not isinstance(payload, dict)
                or payload.get("release_digest") != release_digest
            ):
                raise WorkloadHelperAuthorityError("workload helper operation authority is stale")

    def _release_objects(self, release_digest: str) -> dict[str, int]:
        if (
            not isinstance(release_digest, str)
            or len(release_digest) != 64
            or any(value not in "0123456789abcdef" for value in release_digest)
        ):
            raise WorkloadHelperAuthorityError("workload release digest is invalid")
        # Workload TUF exposes ``releases/<digest>.json`` but stores the
        # content-addressed target as the bare digest on disk.
        path = self._workload_target_root / release_digest
        try:
            from vonk_agent_protocol.workload_packages import PackageReleaseLock
            metadata = path.stat(follow_symlinks=False)
            if (
                not path.is_file()
                or metadata.st_nlink != 1
                or metadata.st_uid not in {0, os.geteuid()}
                or metadata.st_mode & 0o022
                or metadata.st_size > 1024 * 1024
            ):
                raise OSError("unsafe workload release target")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != release_digest:
                raise ValueError("workload release target digest mismatch")
            lock = PackageReleaseLock.parse(raw)
            if lock.canonical_bytes != raw:
                raise ValueError("workload release target is not canonical")
        except Exception as error:
            raise WorkloadHelperAuthorityError("workload release target is unavailable") from error
        if lock.digest != release_digest:
            raise WorkloadHelperAuthorityError("workload release target identity is invalid")
        allowed: dict[str, int] = {}
        for descriptor in (*lock.components, lock.adapter):
            digest = descriptor.digest.removeprefix("sha256:")
            allowed[digest] = descriptor.size
        return allowed

__all__ = [
    "WorkloadHelperAuthorityError",
    "WorkloadHelperAuthorityService",
    "WorkloadHelperGrantIssuer",
    "WorkloadObjectReceiptIssuer",
]
