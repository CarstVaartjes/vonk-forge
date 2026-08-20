"""Bounded networkless Unix-socket boundary for GPU node update authorization."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import struct
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519

from cluster_profiles.platform_release import PlatformRelease, PlatformReleaseError

from .host_state import HostGenerationStore, HostStateConflict, SelectedGeneration
from .update_authority import UpdateAuthorizationError
from .upgrade import ActiveControlRelease, UpgradeConflict

MAX_SIGNER_MESSAGE_BYTES = 64 * 1024
_VERSIONED_PLATFORM_TARGET = re.compile(
    r"platform/releases/"
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)/"
    r"(?P<sha256>[0-9a-f]{64})\.json\Z"
)


class SignerProtocolError(RuntimeError):
    """A signer request or response is outside the closed IPC contract."""


class UpdateSignerPolicy(Protocol):
    def authorize(self, request: Mapping[str, object]) -> Mapping[str, object]: ...


def _canonical(value: object) -> bytes:
    try:
        return (
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as error:
        raise SignerProtocolError("signer message is not canonical JSON") from error


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SignerProtocolError("signer message contains duplicate fields")
        result[key] = value
    return result


def _document(raw: bytes) -> dict[str, object]:
    if not raw or len(raw) > MAX_SIGNER_MESSAGE_BYTES or not raw.endswith(b"\n"):
        raise SignerProtocolError("signer message bounds are invalid")
    try:
        value = json.loads(raw.decode("ascii"), object_pairs_hook=_unique)
    except SignerProtocolError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise SignerProtocolError("signer message is invalid JSON") from error
    if not isinstance(value, dict) or _canonical(value) != raw:
        raise SignerProtocolError("signer message is not canonical")
    return value


def _read_message(connection: socket.socket, *, deadline: float | None = None) -> bytes:
    content = bytearray()
    while len(content) <= MAX_SIGNER_MESSAGE_BYTES:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SignerProtocolError("signer message timeout")
            connection.settimeout(remaining)
        try:
            chunk = connection.recv(
                min(16 * 1024, MAX_SIGNER_MESSAGE_BYTES + 1 - len(content))
            )
        except TimeoutError as error:
            raise SignerProtocolError("signer message timeout") from error
        except OSError as error:
            raise SignerProtocolError("signer peer disconnected") from error
        if not chunk:
            break
        content.extend(chunk)
    if len(content) > MAX_SIGNER_MESSAGE_BYTES:
        raise SignerProtocolError("signer message is too large")
    return bytes(content)


def _snapshot_regular(
    path: Path,
    maximum: int,
    name: str,
    *,
    allowed_owner_uids: frozenset[int] | None = None,
    reject_group_other_write: bool = False,
) -> bytes:
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
        )
    except OSError as error:
        raise SignerProtocolError(f"{name} is unavailable") from error
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > maximum
            or (
                allowed_owner_uids is not None
                and before.st_uid not in allowed_owner_uids
            )
            or (
                reject_group_other_write
                and stat.S_IMODE(before.st_mode) & (stat.S_IWGRP | stat.S_IWOTH)
            )
        ):
            raise SignerProtocolError(f"{name} is unsafe")
        content = bytearray()
        while len(content) <= maximum:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        identity = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
        if len(content) > maximum or identity(before) != identity(after):
            raise SignerProtocolError(f"{name} changed while being read")
        return bytes(content)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class SignerRunningIdentity:
    """Immutable identity injected into the running signer container."""

    release_digest: str
    build_digest: str
    platform_version: str
    process_image: str

    def __post_init__(self) -> None:
        if (
            re.fullmatch(r"sha256:[0-9a-f]{64}", self.release_digest) is None
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.build_digest) is None
            or re.fullmatch(
                r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)",
                self.platform_version,
            )
            is None
            or re.fullmatch(r"[^\s]{1,1900}@sha256:[0-9a-f]{64}", self.process_image)
            is None
        ):
            raise ValueError("signer running identity is invalid")


class SignerActiveControlReleaseLoader:
    """Bind each signer decision to the fresh host-selected active projection."""

    def __init__(
        self,
        identity_root: Path,
        release_source,
        running_identity,
        *,
        owner_uid: int = 0,
    ) -> None:
        identity_root = Path(identity_root)
        if not identity_root.is_absolute():
            raise UpgradeConflict("active control identity root must be absolute")
        if not callable(getattr(release_source, "refresh", None)):
            raise TypeError("verified platform release source is invalid")
        if not callable(running_identity):
            raise TypeError("signer running identity source is invalid")
        self._identity_store = HostGenerationStore(
            identity_root / ".signer-host-state-unavailable",
            identity_root,
            owner_uid=owner_uid,
        )
        self._release_source = release_source
        self._running_identity = running_identity

    def load(self) -> ActiveControlRelease:
        try:
            # The signer deliberately has no control-host mount. Read only the
            # projection side of HostGenerationStore; host pointer reconciliation
            # remains the root updater's responsibility.
            selected = self._identity_store.load_active_projection()
        except HostStateConflict as error:
            raise UpgradeConflict(
                "active control identity projection is unavailable or unsafe"
            ) from error
        if (
            not isinstance(selected, SelectedGeneration)
            or selected.projection_kind != "active"
        ):
            raise UpgradeConflict("active control identity projection is unavailable")
        try:
            raw_target, targets_version = self._release_source.refresh(
                selected.platform_target_name
            )
        except Exception as error:
            raise UpgradeConflict(
                "active control verified platform release is unavailable"
            ) from error
        if not isinstance(raw_target, bytes):
            raise UpgradeConflict("active control verified platform release is invalid")
        if type(targets_version) is not int or targets_version < 1:
            raise UpgradeConflict("active control TUF targets version is invalid")
        target_sha256 = hashlib.sha256(raw_target).hexdigest()
        try:
            release = PlatformRelease.from_bytes(raw_target)
            release.validate_target_identity(
                selected.platform_target_name,
                target_sha256,
            )
        except PlatformReleaseError as error:
            raise UpgradeConflict(
                "active control verified platform release is invalid"
            ) from error
        expected = {
            "platform_target_sha256": target_sha256,
            "tuf_targets_version": targets_version,
            "release_digest": release.digest,
            "build_digest": release.build_digest,
            "platform_version": release.platform_version,
            "deployment_bundle_digest": release.deployment_bundle.layer_digest,
            "api_image": release.control.api_image.reference,
            "worker_image": release.control.worker_image.reference,
            "database_revision": release.database.expand_revision,
        }
        if any(getattr(selected, name) != value for name, value in expected.items()):
            raise UpgradeConflict(
                "active control identity disagrees with verified platform release"
            )
        try:
            running = self._running_identity()
        except Exception as error:
            raise UpgradeConflict("signer running identity is unavailable") from error
        if not isinstance(running, SignerRunningIdentity):
            raise UpgradeConflict("signer running identity is invalid")
        if (
            running.release_digest != selected.release_digest
            or running.build_digest != selected.build_digest
            or running.platform_version != selected.platform_version
            or running.process_image != selected.worker_image
        ):
            raise UpgradeConflict(
                "signer running identity disagrees with active control identity"
            )
        return ActiveControlRelease(
            generation_id=selected.generation_id,
            release_digest=selected.release_digest,
            build_digest=selected.build_digest,
            platform_version=selected.platform_version,
            api_image=selected.api_image,
            worker_image=selected.worker_image,
            migration_revision=selected.database_revision,
        )


class UpdateSignerConnectionHandler:
    def __init__(
        self,
        policy: UpdateSignerPolicy,
        *,
        allowed_peer_uid: int,
        healthcheck_peer_uid: int | None = None,
        request_timeout_seconds: float = 5.0,
    ) -> None:
        if not callable(getattr(policy, "authorize", None)):
            raise TypeError("signer policy is invalid")
        if (
            isinstance(allowed_peer_uid, bool)
            or not isinstance(allowed_peer_uid, int)
            or allowed_peer_uid < 0
        ):
            raise ValueError("signer peer UID is invalid")
        if healthcheck_peer_uid is not None and (
            isinstance(healthcheck_peer_uid, bool)
            or not isinstance(healthcheck_peer_uid, int)
            or healthcheck_peer_uid < 0
        ):
            raise ValueError("signer healthcheck peer UID is invalid")
        if (
            isinstance(request_timeout_seconds, bool)
            or not isinstance(request_timeout_seconds, (int, float))
            or not 0 < request_timeout_seconds <= 30
        ):
            raise ValueError("signer request timeout is invalid")
        self._policy = policy
        self._allowed_peer_uid = allowed_peer_uid
        self._healthcheck_peer_uid = healthcheck_peer_uid
        self._request_timeout_seconds = float(request_timeout_seconds)

    def handle(self, connection: socket.socket) -> None:
        try:
            _pid, uid, _gid = struct.unpack(
                "3i",
                connection.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
                ),
            )
        except (AttributeError, OSError, struct.error) as error:
            raise SignerProtocolError("signer peer identity is unavailable") from error
        if uid not in {self._allowed_peer_uid, self._healthcheck_peer_uid}:
            raise SignerProtocolError("signer peer UID is not authorized")
        raw = _read_message(
            connection,
            deadline=time.monotonic() + self._request_timeout_seconds,
        )
        request = _document(raw)
        if request == {"kind": "health", "schema_version": 1}:
            if uid != self._healthcheck_peer_uid:
                raise SignerProtocolError("signer healthcheck peer UID is not authorized")
            self._send(connection, {"schema_version": 1, "status": "ready"})
            return
        if uid != self._allowed_peer_uid:
            raise SignerProtocolError("signer peer UID is not authorized")
        intent_id = request.get("intent_id")
        if not isinstance(intent_id, str):
            raise SignerProtocolError("signer intent ID is invalid")
        try:
            signed_payload = self._policy.authorize(request)
        except (
            TypeError,
            ValueError,
            UpdateAuthorizationError,
            UpgradeConflict,
        ) as error:
            raise SignerProtocolError("signer policy rejected request") from error
        if not isinstance(signed_payload, Mapping):
            raise SignerProtocolError("signer policy response is invalid")
        response = {
            "intent_id": intent_id,
            "request_digest": hashlib.sha256(raw).hexdigest(),
            "schema_version": 1,
            "signed_payload": dict(signed_payload),
        }
        self._send(connection, response)

    def _send(self, connection: socket.socket, response: Mapping[str, object]) -> None:
        encoded = _canonical(response)
        if len(encoded) > MAX_SIGNER_MESSAGE_BYTES:
            raise SignerProtocolError("signer response is too large")
        try:
            connection.settimeout(self._request_timeout_seconds)
            connection.sendall(encoded)
        except TimeoutError as error:
            raise SignerProtocolError("signer response timeout") from error
        except OSError as error:
            raise SignerProtocolError("signer peer disconnected") from error


class UnixUpdateSignerClient:
    def __init__(self, socket_path: Path, *, timeout_seconds: float = 5.0) -> None:
        self._socket_path = Path(socket_path)
        if not self._socket_path.is_absolute():
            raise ValueError("signer socket path must be absolute")
        if not 0 < timeout_seconds <= 30:
            raise ValueError("signer timeout is invalid")
        self._timeout_seconds = timeout_seconds

    def authorize(self, request: Mapping[str, object]) -> dict[str, object]:
        if not isinstance(request, Mapping):
            raise TypeError("signer request must be a mapping")
        encoded = _canonical(dict(request))
        if len(encoded) > MAX_SIGNER_MESSAGE_BYTES:
            raise SignerProtocolError("signer request is too large")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self._timeout_seconds)
        try:
            connection.connect(str(self._socket_path))
            connection.sendall(encoded)
            connection.shutdown(socket.SHUT_WR)
            response = _document(_read_message(connection))
        except OSError as error:
            raise SignerProtocolError("signer service is unavailable") from error
        finally:
            connection.close()
        if set(response) != {
            "intent_id",
            "request_digest",
            "schema_version",
            "signed_payload",
        }:
            raise SignerProtocolError("signer response fields are invalid")
        if (
            response["schema_version"] != 1
            or response["intent_id"] != request.get("intent_id")
            or response["request_digest"] != hashlib.sha256(encoded).hexdigest()
            or not isinstance(response["signed_payload"], dict)
        ):
            raise SignerProtocolError("signer response binding is invalid")
        return response


def check_signer_ready(
    socket_path: Path,
    *,
    timeout_seconds: float = 3.0,
) -> None:
    """Complete a bounded signer protocol exchange without invoking key authority."""
    request = {"kind": "health", "schema_version": 1}
    encoded = _canonical(request)
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout_seconds)
    try:
        connection.connect(str(socket_path))
        connection.sendall(encoded)
        connection.shutdown(socket.SHUT_WR)
        response = _document(_read_message(connection))
    except OSError as error:
        raise SignerProtocolError("signer service is unavailable") from error
    finally:
        connection.close()
    if response != {"schema_version": 1, "status": "ready"}:
        raise SignerProtocolError("signer health response is invalid")


class RootUpdateSignerPolicy:
    """Closed signing vocabulary backed by signer-local TUF and control identity."""

    def __init__(
        self,
        authority,
        active_release_loader,
        admin_grant_verifier,
        *,
        clock=None,
    ) -> None:
        if not callable(getattr(authority, "authorize", None)) or not callable(
            getattr(authority, "authorize_rollback", None)
        ):
            raise TypeError("update authority is invalid")
        if not callable(getattr(active_release_loader, "load", None)):
            raise TypeError("active control release loader is invalid")
        if not callable(getattr(admin_grant_verifier, "verify", None)):
            raise TypeError("admin action grant verifier is invalid")
        self._authority = authority
        self._active_release_loader = active_release_loader
        self._admin_grant_verifier = admin_grant_verifier
        self._clock = clock or (lambda: datetime.now(UTC))

    def authorize(self, request: Mapping[str, object]) -> dict[str, object]:
        document = self._request(request)
        active = self._active_release_loader.load()
        if not isinstance(active, ActiveControlRelease):
            raise UpdateAuthorizationError("active control release is invalid")
        self._admin_grant_verifier.verify(document)
        source = document["source"]
        assert isinstance(source, dict)
        common = {
            "operation_id": document["operation_id"],
            "fence": document["fence"],
            "expires_at": document["expires_at"],
            "node_id": document["node_id"],
            "attempt": document["attempt"],
            "claim_deadline": document["claim_deadline"],
            "now": self._clock(),
        }
        if document["action"] == "agent.rollback":
            return dict(
                self._authority.authorize_rollback(
                    current_slot=source["slot"],
                    current_sha256=source["sha256"],
                    current_generation=source["generation"],
                    **common,
                )
            )
        payload = document["payload"]
        assert isinstance(payload, dict)
        prepared = self._authority.refresh_and_validate(
            payload, target_name=document["platform_target_name"]
        )
        release = getattr(prepared, "release", None)
        if (
            getattr(release, "digest", None) != document["target_release_digest"]
            or active.release_digest != document["target_release_digest"]
            or getattr(release, "platform_version", None)
            != active.platform_version
            or getattr(release, "build_digest", None) != active.build_digest
        ):
            raise UpdateAuthorizationError(
                "signer target release disagrees with active control release"
            )
        if (
            getattr(prepared, "target_name", None)
            != document["platform_target_name"]
            or getattr(prepared, "target_sha256", None)
            != document["expected_tuf_target_sha256"]
            or getattr(prepared, "targets_version", None)
            != document["expected_tuf_targets_version"]
        ):
            raise UpdateAuthorizationError(
                "signer TUF target identity disagrees with request"
            )
        return dict(
            self._authority.authorize(
                payload,
                previous_slot=source["slot"],
                previous_sha256=source["sha256"],
                previous_generation=source["generation"],
                prepared=prepared,
                **common,
            )
        )

    @staticmethod
    def _request(value: Mapping[str, object]) -> dict[str, object]:
        if not isinstance(value, Mapping):
            raise UpdateAuthorizationError("signer request is invalid")
        document = dict(value)
        common = {
            "action",
            "attempt",
            "claim_deadline",
            "expires_at",
            "fence",
            "admin_grant",
            "intent_id",
            "node_id",
            "operation_id",
            "parent_job_id",
            "rollout_id",
            "schema_version",
            "source",
        }
        expected = (
            common
            if document.get("action") == "agent.rollback"
            else common
            | {
                "expected_tuf_target_sha256",
                "expected_tuf_targets_version",
                "payload",
                "platform_target_name",
                "target_release_digest",
            }
        )
        source = document.get("source")
        if (
            set(document) != expected
            or document.get("schema_version") != 1
            or document.get("action") not in {"agent.update", "agent.rollback"}
            or isinstance(document.get("attempt"), bool)
            or document.get("attempt") != 1
            or isinstance(document.get("claim_deadline"), bool)
            or not isinstance(document.get("claim_deadline"), int)
            or document.get("claim_deadline") != document.get("expires_at")
            or not isinstance(document.get("node_id"), str)
            or re.fullmatch(r"spk_[0-9a-f]{32}", document["node_id"]) is None
            or not isinstance(document.get("admin_grant"), dict)
            or not isinstance(source, dict)
            or set(source) != {"generation", "sha256", "slot"}
            or source.get("slot") not in {"A", "B"}
            or not isinstance(source.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) is None
            or isinstance(source.get("generation"), bool)
            or not isinstance(source.get("generation"), int)
            or not 1 <= source["generation"] <= 999_999_999
        ):
            raise UpdateAuthorizationError("signer request fields are invalid")
        for name in (
            "intent_id",
            "operation_id",
            "fence",
            "parent_job_id",
            "rollout_id",
        ):
            try:
                parsed = uuid.UUID(str(document.get(name)))
            except (TypeError, ValueError) as error:
                raise UpdateAuthorizationError(
                    f"signer {name.replace('_', ' ')} is invalid"
                ) from error
            if parsed.version != 4 or str(parsed) != document.get(name):
                raise UpdateAuthorizationError(
                    f"signer {name.replace('_', ' ')} is invalid"
                )
        if document["action"] == "agent.update":
            digest = document.get("target_release_digest")
            target_name = document.get("platform_target_name")
            target_match = (
                _VERSIONED_PLATFORM_TARGET.fullmatch(target_name)
                if isinstance(target_name, str)
                else None
            )
            if (
                not isinstance(document.get("payload"), dict)
                or not isinstance(digest, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
                or target_match is None
                or not isinstance(document.get("expected_tuf_target_sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", document["expected_tuf_target_sha256"])
                is None
                or target_match.group("sha256")
                != document["expected_tuf_target_sha256"]
                or isinstance(document.get("expected_tuf_targets_version"), bool)
                or not isinstance(document.get("expected_tuf_targets_version"), int)
                or document["expected_tuf_targets_version"] < 1
            ):
                raise UpdateAuthorizationError("signer update target is invalid")
        return document


class AdminActionGrantVerifier:
    """Verify API-issued rollout authority with a signer-only public key."""

    def __init__(
        self,
        public_key: ed25519.Ed25519PublicKey,
        *,
        key_id: str,
        clock=None,
    ) -> None:
        if not isinstance(public_key, ed25519.Ed25519PublicKey):
            raise TypeError("admin grant public key is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", key_id) is None:
            raise ValueError("admin grant key ID is invalid")
        self._public_key = public_key
        self._key_id = key_id
        self._clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    def from_public_document(
        cls, path: Path, *, clock=None
    ) -> AdminActionGrantVerifier:
        path = Path(path)
        try:
            raw = _snapshot_regular(
                path,
                16 * 1024,
                "admin grant public key file",
                allowed_owner_uids=frozenset({0, os.geteuid()}),
                reject_group_other_write=True,
            )
        except SignerProtocolError as error:
            raise UpdateAuthorizationError(
                "admin grant public key file is unsafe"
            ) from error
        try:
            document = _document(raw)
        except SignerProtocolError as error:
            raise UpdateAuthorizationError(
                "admin grant public key is invalid"
            ) from error
        if (
            set(document) != {"algorithm", "key_id", "public_key", "schema_version"}
            or document.get("algorithm") != "ed25519"
            or document.get("schema_version") != 1
            or not isinstance(document.get("key_id"), str)
            or not isinstance(document.get("public_key"), str)
        ):
            raise UpdateAuthorizationError("admin grant public key is invalid")
        try:
            raw = bytes.fromhex(document["public_key"])
            key = ed25519.Ed25519PublicKey.from_public_bytes(raw)
        except ValueError as error:
            raise UpdateAuthorizationError(
                "admin grant public key is invalid"
            ) from error
        if hashlib.sha256(raw).hexdigest() != document["key_id"]:
            raise UpdateAuthorizationError("admin grant public key ID is invalid")
        return cls(key, key_id=document["key_id"], clock=clock)

    def verify(self, request: Mapping[str, object]) -> None:
        envelope = request.get("admin_grant")
        if not isinstance(envelope, Mapping) or set(envelope) != {
            "claims",
            "signature",
        }:
            raise UpdateAuthorizationError("admin action grant is invalid")
        claims = envelope.get("claims")
        signature = envelope.get("signature")
        if not isinstance(claims, Mapping) or not isinstance(signature, Mapping):
            raise UpdateAuthorizationError("admin action grant is invalid")
        claims = dict(claims)
        expected_claims = {
            "action",
            "expires_at",
            "nonce",
            "node_ids",
            "parent_job_id",
            "rollout_id",
            "schema_version",
            "target_release_digest",
        }
        nodes = claims.get("node_ids")
        if (
            set(claims) != expected_claims
            or claims.get("schema_version") != 1
            or claims.get("action") not in {"agent.update", "agent.rollback"}
            or not isinstance(nodes, list)
            or not nodes
            or any(
                not isinstance(node, str)
                or re.fullmatch(r"spk_[0-9a-f]{32}", node) is None
                for node in nodes
            )
            or len(nodes) != len(set(nodes))
            or nodes != sorted(nodes)
        ):
            raise UpdateAuthorizationError("admin action grant claims are invalid")
        for name in ("nonce", "parent_job_id", "rollout_id"):
            try:
                parsed = uuid.UUID(str(claims.get(name)))
            except (TypeError, ValueError) as error:
                raise UpdateAuthorizationError(
                    "admin action grant identity is invalid"
                ) from error
            if parsed.version != 4 or str(parsed) != claims.get(name):
                raise UpdateAuthorizationError("admin action grant identity is invalid")
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise UpdateAuthorizationError("admin grant verifier clock is naive")
        expiry = claims.get("expires_at")
        target = claims.get("target_release_digest")
        if (
            isinstance(expiry, bool)
            or not isinstance(expiry, int)
            or expiry <= int(now.astimezone(UTC).timestamp())
            or expiry > int(now.astimezone(UTC).timestamp()) + 3600
            or (
                claims["action"] == "agent.update"
                and (
                    not isinstance(target, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", target) is None
                )
            )
            or (claims["action"] == "agent.rollback" and target is not None)
        ):
            raise UpdateAuthorizationError(
                "admin action grant expiry or target is invalid"
            )
        if (
            set(signature) != {"algorithm", "key_id", "value"}
            or signature.get("algorithm") != "ed25519"
            or signature.get("key_id") != self._key_id
            or not isinstance(signature.get("value"), str)
            or re.fullmatch(r"[0-9a-f]{128}", signature["value"]) is None
        ):
            raise UpdateAuthorizationError("admin action grant signature is invalid")
        try:
            raw_signature = bytes.fromhex(signature["value"])
            self._public_key.verify(raw_signature, _canonical(claims))
        except (ValueError, InvalidSignature) as error:
            raise UpdateAuthorizationError(
                "admin action grant signature is invalid"
            ) from error
        if (
            request.get("action") != claims["action"]
            or request.get("rollout_id") != claims["rollout_id"]
            or request.get("parent_job_id") != claims["parent_job_id"]
            or request.get("node_id") not in nodes
            or request.get("target_release_digest") != target
            or not isinstance(request.get("expires_at"), int)
            or request["expires_at"] > expiry
        ):
            raise UpdateAuthorizationError(
                "admin action grant does not authorize signer request"
            )


def serve_forever(
    socket_path: Path,
    handler: UpdateSignerConnectionHandler,
) -> None:
    """Serve one bounded request per local peer connection."""
    path = Path(socket_path)
    if not path.is_absolute():
        raise ValueError("signer socket path must be absolute")
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    try:
        info = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISSOCK(info.st_mode):
            raise SignerProtocolError("signer socket path is occupied")
        path.unlink()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        os.chmod(path, 0o660)
        listener.listen(16)
        while True:
            connection, _ = listener.accept()
            with connection:
                try:
                    handler.handle(connection)
                except SignerProtocolError:
                    continue
    finally:
        listener.close()


def main() -> None:
    from .settings import SignerSettings
    from .update_authority import (
        PublishedTUFReleaseSource,
        UpdateAuthorizationAuthority,
        snapshot_public_trust_root,
    )

    settings = SignerSettings.from_env_and_secrets()
    bootstrap = snapshot_public_trust_root(settings.tuf_bootstrap_root_path)
    release_source = PublishedTUFReleaseSource(
        publication_metadata_root=settings.tuf_metadata_root,
        publication_target_root=settings.tuf_target_root,
        verified_metadata_root=settings.tuf_verified_metadata_root,
        verified_target_root=settings.tuf_verified_target_root,
        bootstrap_root=bootstrap,
    )
    authority = UpdateAuthorizationAuthority.from_private_key_file(
        settings.update_authority_key_path,
        release_source=release_source,
    )
    running = SignerRunningIdentity(
        release_digest=settings.platform_release_digest,
        build_digest=settings.platform_build_digest,
        platform_version=settings.platform_version,
        process_image=settings.process_image,
    )
    loader = SignerActiveControlReleaseLoader(
        settings.control_identity_root,
        release_source,
        lambda: running,
    )
    verifier = AdminActionGrantVerifier.from_public_document(
        settings.admin_grant_public_key_path
    )
    policy = RootUpdateSignerPolicy(authority, loader, verifier)
    serve_forever(
        settings.socket_path,
        UpdateSignerConnectionHandler(
            policy,
            allowed_peer_uid=10001,
            healthcheck_peer_uid=10003,
        ),
    )


if __name__ == "__main__":
    main()
