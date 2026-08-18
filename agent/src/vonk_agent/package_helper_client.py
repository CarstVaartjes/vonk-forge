"""Unprivileged, authenticated client for the root-owned package helper."""

from __future__ import annotations

import hashlib
import inspect
import re
import socket
import struct
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from vonk_agent_protocol.workload_packages import (
    PackageHelperOperation,
    SignedPackageObjectReceipt,
    package_helper_grant_signing_bytes,
    package_object_receipt_signing_bytes,
)

from .deadlines import DeadlineBindingError, MonotonicDeadline
from .package_helper_protocol import (
    MAX_HELPER_MESSAGE_BYTES,
    HelperProtocolError,
    HelperRequest,
    HelperResponse,
    frame_helper_message,
)
from .packages.adapter import (
    AdapterEvidence,
    AdapterInvocation,
    AdapterOperation,
)

PACKAGE_HELPER_SOCKET = "/run/vonk-forge-package-helper/package-helper.sock"
PACKAGE_HELPER_GRANT_PUBLIC_KEY = Path("/etc/vonk-forge-agent/package-fence-public.pem")
PACKAGE_HELPER_RECEIPT_PUBLIC_KEY = Path("/etc/vonk-forge-agent/package-receipt-public.pem")
_KEY_ID = re.compile(r"[0-9a-f]{64}\Z")


class PackageHelperClient(Protocol):
    def submit(
        self,
        request: HelperRequest,
        *,
        deadline: datetime | MonotonicDeadline | None = None,
    ) -> HelperResponse: ...


class PackageHelperAuthorityVerifier:
    """Verify independent control grants and materialization receipts."""

    def __init__(
        self,
        grant_public_key: Ed25519PublicKey,
        receipt_public_key: Ed25519PublicKey,
        *,
        grant_key_id: str,
        receipt_key_id: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(grant_public_key, Ed25519PublicKey) or not isinstance(
            receipt_public_key, Ed25519PublicKey
        ):
            raise HelperProtocolError("package helper public key is invalid")
        actual_grant_id = hashlib.sha256(
            grant_public_key.public_bytes_raw()
        ).hexdigest()
        actual_receipt_id = hashlib.sha256(
            receipt_public_key.public_bytes_raw()
        ).hexdigest()
        if (
            not isinstance(grant_key_id, str)
            or not _KEY_ID.fullmatch(grant_key_id)
            or grant_key_id != actual_grant_id
        ):
            raise HelperProtocolError("package helper grant key is invalid")
        if (
            not isinstance(receipt_key_id, str)
            or not _KEY_ID.fullmatch(receipt_key_id)
            or receipt_key_id != actual_receipt_id
        ):
            raise HelperProtocolError("package helper receipt key is invalid")
        if grant_key_id == receipt_key_id:
            raise HelperProtocolError("package helper authority keys are not distinct")
        if clock is not None and not callable(clock):
            raise HelperProtocolError("package helper authority clock is invalid")
        self._grant_public_key = grant_public_key
        self._receipt_public_key = receipt_public_key
        self._grant_key_id = grant_key_id
        self._receipt_key_id = receipt_key_id
        self._clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    def from_files(
        cls,
        grant_path: Path = PACKAGE_HELPER_GRANT_PUBLIC_KEY,
        receipt_path: Path = PACKAGE_HELPER_RECEIPT_PUBLIC_KEY,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> PackageHelperAuthorityVerifier:
        """Load the root-installed public keys without introducing a signer."""

        def load(path: Path) -> Ed25519PublicKey:
            try:
                metadata = Path(path).stat()
                if (
                    not Path(path).is_file()
                    or metadata.st_nlink != 1
                    or metadata.st_uid != 0
                    or metadata.st_mode & 0o022
                    or metadata.st_size > 4096
                ):
                    raise ValueError
                value = serialization.load_pem_public_key(Path(path).read_bytes())
            except (OSError, TypeError, ValueError) as error:
                raise HelperProtocolError("package helper public key is unavailable") from error
            if not isinstance(value, Ed25519PublicKey):
                raise HelperProtocolError("package helper public key is invalid")
            return value

        grant = load(Path(grant_path))
        receipt = load(Path(receipt_path))
        return cls(
            grant,
            receipt,
            grant_key_id=hashlib.sha256(grant.public_bytes_raw()).hexdigest(),
            receipt_key_id=hashlib.sha256(receipt.public_bytes_raw()).hexdigest(),
            clock=clock,
        )

    def verify_request(self, request: HelperRequest) -> None:
        if type(request) is not HelperRequest:
            raise HelperProtocolError("package helper request is invalid")
        self._verify_binding(request)
        now = self._now()
        claims = request.grant.claims
        if now < claims.issued_at:
            raise HelperProtocolError("package helper grant is not active")
        if now >= claims.expires_at:
            raise HelperProtocolError("package helper grant has expired")
        signature = request.grant.signature
        if signature.key_id != self._grant_key_id:
            raise HelperProtocolError("package helper grant key is invalid")
        try:
            self._grant_public_key.verify(
                bytes.fromhex(signature.value),
                package_helper_grant_signing_bytes(claims),
            )
        except (InvalidSignature, ValueError) as error:
            raise HelperProtocolError(
                "package helper grant signature is invalid"
            ) from error
        for receipt in request.receipts:
            self._verify_receipt(receipt)

    @staticmethod
    def _verify_binding(request: HelperRequest) -> None:
        body = request.body
        claims = request.grant.claims
        expected = (
            body.request_id,
            body.node_id,
            body.job_id,
            body.operation_id,
            body.attempt,
            body.fence,
            body.invocation.release_digest,
            body.invocation.generation,
            body.operation,
            body.digest,
        )
        actual = (
            claims.request_id,
            claims.node_id,
            claims.job_id,
            claims.operation_id,
            claims.attempt,
            claims.fence,
            claims.release_digest,
            claims.generation,
            claims.operation,
            claims.request_digest,
        )
        if actual != expected:
            raise HelperProtocolError(
                "package helper grant does not bind execution body"
            )

    def _verify_receipt(self, receipt: SignedPackageObjectReceipt) -> None:
        if type(receipt) is not SignedPackageObjectReceipt:
            raise HelperProtocolError("package helper object receipt is invalid")
        signature = receipt.signature
        if signature.key_id != self._receipt_key_id:
            raise HelperProtocolError("package helper receipt key is invalid")
        try:
            self._receipt_public_key.verify(
                bytes.fromhex(signature.value),
                package_object_receipt_signing_bytes(receipt.claims),
            )
        except (InvalidSignature, ValueError) as error:
            raise HelperProtocolError(
                "package helper receipt signature is invalid"
            ) from error

    def _now(self) -> int:
        try:
            now = self._clock()
        except Exception as error:
            raise HelperProtocolError(
                "package helper authority clock is unavailable"
            ) from error
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise HelperProtocolError("package helper authority clock is invalid")
        return int(now.astimezone(UTC).timestamp())


class UnixPackageHelperClient:
    """Submit one bounded request over the fixed root-owned Unix socket."""

    def __init__(
        self,
        authority: PackageHelperAuthorityVerifier,
        *,
        connector: Callable[[str, float], socket.socket] | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        if not callable(getattr(authority, "verify_request", None)):
            raise HelperProtocolError("package helper authority verifier is invalid")
        if connector is not None and not callable(connector):
            raise HelperProtocolError("package helper connector is invalid")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not 0 < timeout_seconds <= 30
        ):
            raise HelperProtocolError("package helper connection deadline is invalid")
        self._authority = authority
        self._connector = connector or _connect_unix
        self._timeout_seconds = float(timeout_seconds)

    def submit(
        self,
        request: HelperRequest,
        *,
        deadline: datetime | MonotonicDeadline | None = None,
    ) -> HelperResponse:
        self._authority.verify_request(request)
        budget = _DeadlineBudget(self._timeout_seconds, deadline)
        raw = request.to_bytes()
        try:
            connection = self._connector(PACKAGE_HELPER_SOCKET, budget.remaining())
        except (OSError, TimeoutError) as error:
            raise HelperProtocolError("package helper connection failed") from error
        if not isinstance(connection, socket.socket):
            raise HelperProtocolError("package helper connection is invalid")
        try:
            if (
                connection.family != socket.AF_UNIX
                or connection.type & socket.SOCK_STREAM == 0
            ):
                raise HelperProtocolError(
                    "package helper connection is not Unix stream"
                )
            if _unix_peer_uid(connection) != 0:
                raise HelperProtocolError("package helper server is not root")
            try:
                connection.settimeout(budget.remaining())
                connection.sendall(frame_helper_message(raw))
                response_raw = _receive_response(connection, budget)
            except (OSError, TimeoutError, HelperProtocolError) as error:
                raise HelperProtocolError(
                    "package helper response deadline elapsed"
                ) from error
            try:
                response = HelperResponse.parse(response_raw)
            except (TypeError, ValueError) as error:
                raise HelperProtocolError(
                    "package helper response is invalid"
                ) from error
            if (
                response.request_id,
                response.fence,
                response.request_digest,
            ) != (request.request_id, request.fence, request.body.digest):
                raise HelperProtocolError("package helper response binding is invalid")
            return response
        finally:
            connection.close()


class PackageHelperAdapterExecutor:
    """Adapter-shaped executor that delegates only to the package helper."""

    def __init__(
        self,
        client: PackageHelperClient,
        request_factory: Callable[
            [AdapterOperation, AdapterInvocation, datetime | MonotonicDeadline],
            HelperRequest,
        ],
        *,
        release_digest: str,
        generation: str,
    ) -> None:
        if not callable(getattr(client, "submit", None)) or not callable(
            request_factory
        ):
            raise HelperProtocolError("package helper adapter boundary is invalid")
        self._client = client
        self._request_factory = request_factory
        self._release_digest = release_digest
        self._generation = generation

    def execute(
        self,
        operation: AdapterOperation,
        invocation: AdapterInvocation,
        deadline: datetime | MonotonicDeadline,
    ) -> AdapterEvidence:
        if (
            type(operation) is not AdapterOperation
            or type(invocation) is not AdapterInvocation
        ):
            raise HelperProtocolError("package helper adapter invocation is invalid")
        if (
            invocation.release_digest != self._release_digest
            or invocation.generation != self._generation
        ):
            raise HelperProtocolError("package helper adapter invocation is not bound")
        try:
            request = self._request_factory(operation, invocation, deadline)
        except Exception as error:
            raise HelperProtocolError(
                "package helper adapter request failed"
            ) from error
        if type(request) is not HelperRequest:
            raise HelperProtocolError("package helper adapter request is invalid")
        helper_operation = PackageHelperOperation(operation.value)
        body = request.body
        if (
            body.operation,
            body.node_id,
            body.job_id,
            body.operation_id,
            body.attempt,
            body.fence,
            body.invocation.release_digest,
            body.invocation.generation,
        ) != (
            helper_operation,
            invocation.node_id,
            invocation.job_id,
            invocation.operation_id,
            invocation.attempt,
            invocation.fence,
            invocation.release_digest,
            invocation.generation,
        ):
            raise HelperProtocolError(
                "package helper adapter request binding is invalid"
            )
        response = self._client.submit(request, deadline=deadline)
        if type(response) is not HelperResponse or (
            response.request_id,
            response.fence,
            response.request_digest,
        ) != (request.request_id, request.fence, request.body.digest):
            raise HelperProtocolError(
                "package helper adapter response binding is invalid"
            )
        return AdapterEvidence(
            operation=operation,
            status=response.status,
            release_digest=invocation.release_digest,
            generation=invocation.generation,
            fence=invocation.fence,
            evidence_digest=response.evidence_digest,
        )


class PackageHelperAdapterFactory:
    """Callable seam for constructing package-helper adapter executors."""

    def __init__(
        self, client: PackageHelperClient, request_factory: Callable[..., object]
    ):
        if not callable(getattr(client, "submit", None)) or not callable(
            request_factory
        ):
            raise HelperProtocolError("package helper adapter factory is invalid")
        self._client = client
        self._request_factory = request_factory

    def __call__(
        self,
        lock: object,
        generation_id: str,
        generation_path: Path,
        objects: Mapping[str, object],
        *,
        request: object | None = None,
    ) -> PackageHelperAdapterExecutor:
        release_digest = getattr(lock, "digest", None)

        def build(
            operation: AdapterOperation,
            invocation: AdapterInvocation,
            deadline: datetime | MonotonicDeadline,
        ) -> HelperRequest:
            arguments = (
                lock,
                generation_id,
                generation_path,
                objects,
                operation,
                invocation,
                deadline,
            )
            try:
                parameters = inspect.signature(self._request_factory).parameters
                accepts_request = (
                    "package_request" in parameters
                    or any(
                        parameter.kind is inspect.Parameter.VAR_KEYWORD
                        for parameter in parameters.values()
                    )
                )
            except (TypeError, ValueError):
                accepts_request = False
            if accepts_request:
                return self._request_factory(*arguments, package_request=request)
            return self._request_factory(*arguments)

        return PackageHelperAdapterExecutor(
            self._client,
            build,
            release_digest=release_digest,
            generation=generation_id,
        )


class _DeadlineBudget:
    def __init__(
        self,
        timeout_seconds: float,
        deadline: datetime | MonotonicDeadline | None,
    ) -> None:
        absolute = time.monotonic() + timeout_seconds
        if deadline is not None:
            try:
                fixed = MonotonicDeadline.bind(deadline)
                fixed.check()
            except DeadlineBindingError as error:
                raise HelperProtocolError(
                    "package helper deadline has elapsed"
                ) from error
            absolute = min(absolute, fixed.absolute())
        self._absolute = absolute

    def remaining(self) -> float:
        remaining = self._absolute - time.monotonic()
        if remaining <= 0:
            raise HelperProtocolError("package helper deadline has elapsed")
        return min(30.0, remaining)


def _connect_unix(path: str, timeout_seconds: float) -> socket.socket:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(timeout_seconds)
        connection.connect(path)
    except (OSError, TimeoutError):
        connection.close()
        raise
    return connection


def _receive_response(connection: socket.socket, budget: _DeadlineBudget) -> bytes:
    header = _receive_exact(connection, 4, budget)
    try:
        (size,) = struct.unpack(">I", header)
    except struct.error as error:
        raise HelperProtocolError("package helper response frame is invalid") from error
    if not 1 <= size <= MAX_HELPER_MESSAGE_BYTES:
        raise HelperProtocolError("package helper response frame is invalid")
    return _receive_exact(connection, size, budget)


def _receive_exact(
    connection: socket.socket, size: int, budget: _DeadlineBudget
) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        connection.settimeout(budget.remaining())
        chunk = connection.recv(remaining)
        if not chunk:
            raise HelperProtocolError("package helper response is truncated")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _unix_peer_uid(connection: socket.socket) -> int:
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        _pid, uid, _gid = struct.unpack("3i", raw)
    except (OSError, struct.error) as error:
        raise HelperProtocolError(
            "package helper server credentials are unavailable"
        ) from error
    return uid


__all__ = [
    "PACKAGE_HELPER_GRANT_PUBLIC_KEY",
    "PACKAGE_HELPER_RECEIPT_PUBLIC_KEY",
    "PACKAGE_HELPER_SOCKET",
    "PackageHelperAdapterExecutor",
    "PackageHelperAdapterFactory",
    "PackageHelperAuthorityVerifier",
    "PackageHelperClient",
    "UnixPackageHelperClient",
]
