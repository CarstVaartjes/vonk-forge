"""Root-owned control-host generations, projections, journals, and lock."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

_IDENTIFIER = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_PHASE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\Z")
_SEMVER_PATTERN = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
_SEMVER = re.compile(_SEMVER_PATTERN + r"\Z")
_VERSIONED_TARGET = re.compile(
    rf"platform/releases/(?P<version>{_SEMVER_PATTERN})/"
    r"(?P<sha256>[0-9a-f]{64})\.json\Z"
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IMAGE = re.compile(r"[^\s]{1,1900}@sha256:[0-9a-f]{64}\Z")
_ENTRY_NAME = re.compile(r"([0-9]{4})-([a-z0-9][a-z0-9-]{0,63})\.json\Z")
_MAX_DOCUMENT_BYTES = 64 * 1024
_TERMINAL_PHASES = frozenset({"completed", "failed", "rolled-back"})


class HostStateConflict(RuntimeError):
    """Host generation state is unsafe, inconsistent, or currently locked."""


class _DuplicateField(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise _DuplicateField(name)
        result[name] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(value)


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
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise HostStateConflict("host state document is not canonical JSON") from error


def _parse_mapping(raw: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (
        _DuplicateField,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as error:
        raise HostStateConflict(f"{label} is not valid JSON") from error
    if not isinstance(value, dict) or raw != _canonical(value):
        raise HostStateConflict(f"{label} is not canonical")
    return value


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _validate_absolute(path: Path, label: str) -> Path:
    path = Path(path)
    if not path.is_absolute():
        raise HostStateConflict(f"{label} must be absolute")
    return path


def _owner_is_trusted(metadata: os.stat_result, expected_uid: int) -> bool:
    return metadata.st_uid == expected_uid


def _validate_directory(
    metadata: os.stat_result,
    *,
    mode: int,
    label: str,
    expected_uid: int,
) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise HostStateConflict(f"{label} is unsafe")
    if not _owner_is_trusted(metadata, expected_uid):
        raise HostStateConflict(f"{label} owner is unsafe")
    if stat.S_IMODE(metadata.st_mode) != mode:
        raise HostStateConflict(f"{label} mode is unsafe")


def _validate_file(
    metadata: os.stat_result,
    *,
    mode: int,
    label: str,
    maximum: int = _MAX_DOCUMENT_BYTES,
    allow_empty: bool = False,
    expected_uid: int,
) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise HostStateConflict(f"{label} is unsafe")
    if metadata.st_nlink != 1:
        raise HostStateConflict(f"{label} hard-link count is unsafe")
    if not _owner_is_trusted(metadata, expected_uid):
        raise HostStateConflict(f"{label} owner is unsafe")
    if stat.S_IMODE(metadata.st_mode) != mode:
        raise HostStateConflict(f"{label} mode is unsafe")
    minimum = 0 if allow_empty else 1
    if not minimum <= metadata.st_size <= maximum:
        raise HostStateConflict(f"{label} size is unsafe")


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _open_directory_path(
    path: Path, *, mode: int, label: str, expected_uid: int
) -> int:
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as error:
        raise HostStateConflict(f"{label} is missing or unsafe") from error
    try:
        _validate_directory(
            os.fstat(descriptor),
            mode=mode,
            label=label,
            expected_uid=expected_uid,
        )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _open_directory_at(
    parent: int,
    name: str,
    *,
    mode: int,
    label: str,
) -> int:
    expected_uid = os.fstat(parent).st_uid
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent,
        )
    except OSError as error:
        raise HostStateConflict(f"{label} is missing or unsafe") from error
    try:
        _validate_directory(
            os.fstat(descriptor),
            mode=mode,
            label=label,
            expected_uid=expected_uid,
        )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _ensure_directory_path(
    path: Path, *, mode: int, label: str, expected_uid: int
) -> int:
    try:
        os.mkdir(path, mode)
    except FileExistsError:
        pass
    except OSError as error:
        raise HostStateConflict(f"{label} cannot be created safely") from error
    return _open_directory_path(path, mode=mode, label=label, expected_uid=expected_uid)


def _ensure_directory_at(
    parent: int,
    name: str,
    *,
    mode: int,
    label: str,
) -> int:
    try:
        os.mkdir(name, mode, dir_fd=parent)
        os.fsync(parent)
    except FileExistsError:
        pass
    except OSError as error:
        raise HostStateConflict(f"{label} cannot be created safely") from error
    return _open_directory_at(parent, name, mode=mode, label=label)


def _read_file_at(
    parent: int,
    name: str,
    *,
    mode: int,
    label: str,
    maximum: int = _MAX_DOCUMENT_BYTES,
) -> bytes:
    expected_uid = os.fstat(parent).st_uid
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent,
        )
    except OSError as error:
        raise HostStateConflict(f"{label} is missing or unsafe") from error
    try:
        before = os.fstat(descriptor)
        _validate_file(
            before,
            mode=mode,
            label=label,
            maximum=maximum,
            expected_uid=expected_uid,
        )
        content = bytearray()
        while len(content) <= maximum:
            chunk = os.read(descriptor, min(65536, maximum + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        os.lseek(descriptor, 0, os.SEEK_SET)
        repeated = bytearray()
        while len(repeated) < len(content):
            chunk = os.read(
                descriptor,
                min(65536, len(content) - len(repeated)),
            )
            if not chunk:
                raise HostStateConflict(f"{label} changed while being read")
            repeated.extend(chunk)
        if os.read(descriptor, 1) or repeated != content:
            raise HostStateConflict(f"{label} changed while being read")
        after = os.fstat(descriptor)
        _validate_file(
            after,
            mode=mode,
            label=label,
            maximum=maximum,
            expected_uid=expected_uid,
        )
        if len(content) != before.st_size or _identity(before) != _identity(after):
            raise HostStateConflict(f"{label} changed while being read")
        return bytes(content)
    except OSError as error:
        raise HostStateConflict(f"{label} cannot be read safely") from error
    finally:
        os.close(descriptor)


def _exists_at(parent: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise HostStateConflict("host state path cannot be inspected safely") from error
    return True


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise HostStateConflict("host state write was incomplete")
        offset += written


def _write_new_at(
    parent: int,
    name: str,
    content: bytes,
    *,
    mode: int,
    label: str,
) -> None:
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            mode,
            dir_fd=parent,
        )
    except OSError as error:
        raise HostStateConflict(f"{label} already exists or is unsafe") from error
    try:
        _write_all(descriptor, content)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(parent)


def _validate_replace_target(
    parent: int,
    name: str,
    *,
    mode: int,
    label: str,
) -> None:
    if not _exists_at(parent, name):
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=parent,
        )
        _validate_file(
            os.fstat(descriptor),
            mode=mode,
            label=label,
            allow_empty=True,
            expected_uid=os.fstat(parent).st_uid,
        )
    except OSError as error:
        raise HostStateConflict(f"{label} is unsafe") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_atomic_at(
    parent: int,
    name: str,
    content: bytes,
    *,
    mode: int,
    label: str,
) -> None:
    _validate_replace_target(parent, name, mode=mode, label=label)
    temporary = f".{name}.{os.getpid()}.{secrets.token_hex(8)}.new"
    try:
        _write_new_at(parent, temporary, content, mode=mode, label=label)
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass


def _write_new_atomic_at(
    parent: int,
    name: str,
    content: bytes,
    *,
    mode: int,
    label: str,
) -> None:
    if _exists_at(parent, name):
        raise HostStateConflict(f"{label} already exists")
    temporary = f".{name}.{os.getpid()}.{secrets.token_hex(8)}.new"
    try:
        _write_new_at(parent, temporary, content, mode=mode, label=label)
        if _exists_at(parent, name):
            raise HostStateConflict(f"{label} already exists")
        os.rename(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass


def _valid_identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _validate_identity_fields(value: object) -> None:
    target_name = getattr(value, "platform_target_name", None)
    target = (
        _VERSIONED_TARGET.fullmatch(target_name)
        if isinstance(target_name, str)
        else None
    )
    target_sha256 = getattr(value, "platform_target_sha256", None)
    platform_version = getattr(value, "platform_version", None)
    release_digest = getattr(value, "release_digest", None)
    valid = (
        _valid_identifier(getattr(value, "operation_id", None))
        and isinstance(getattr(value, "plan_digest", None), str)
        and _DIGEST.fullmatch(value.plan_digest) is not None
        and _valid_identifier(getattr(value, "generation_id", None))
        and target is not None
        and isinstance(target_sha256, str)
        and _SHA256.fullmatch(target_sha256) is not None
        and value.generation_id == "gen-" + target_sha256[:24]
        and target.group("sha256") == target_sha256
        and type(getattr(value, "tuf_targets_version", None)) is int
        and value.tuf_targets_version >= 1
        and isinstance(release_digest, str)
        and release_digest == "sha256:" + target_sha256
        and isinstance(getattr(value, "build_digest", None), str)
        and _DIGEST.fullmatch(value.build_digest) is not None
        and isinstance(platform_version, str)
        and _SEMVER.fullmatch(platform_version) is not None
        and target.group("version") == platform_version
        and isinstance(getattr(value, "deployment_bundle_digest", None), str)
        and _DIGEST.fullmatch(value.deployment_bundle_digest) is not None
        and isinstance(getattr(value, "api_image", None), str)
        and _IMAGE.fullmatch(value.api_image) is not None
        and isinstance(getattr(value, "worker_image", None), str)
        and _IMAGE.fullmatch(value.worker_image) is not None
        and _valid_identifier(getattr(value, "database_revision", None))
    )
    if not valid:
        raise ValueError("host operation identity is invalid")


@dataclass(frozen=True)
class HostOperationPlan:
    operation_id: str
    plan_digest: str
    generation_id: str
    platform_target_name: str
    platform_target_sha256: str
    tuf_targets_version: int
    release_digest: str
    build_digest: str
    platform_version: str
    deployment_bundle_digest: str
    api_image: str
    worker_image: str
    database_revision: str

    def __post_init__(self) -> None:
        _validate_identity_fields(self)

    def document(self) -> dict[str, object]:
        return {
            "api_image": self.api_image,
            "build_digest": self.build_digest,
            "database_revision": self.database_revision,
            "deployment_bundle_digest": self.deployment_bundle_digest,
            "generation_id": self.generation_id,
            "operation_id": self.operation_id,
            "plan_digest": self.plan_digest,
            "platform_target_name": self.platform_target_name,
            "platform_target_sha256": self.platform_target_sha256,
            "platform_version": self.platform_version,
            "release_digest": self.release_digest,
            "schema_version": 1,
            "tuf_targets_version": self.tuf_targets_version,
            "worker_image": self.worker_image,
        }

    def generation_receipt(self) -> GenerationReceipt:
        return GenerationReceipt(
            generation_id=self.generation_id,
            platform_target_name=self.platform_target_name,
            platform_target_sha256=self.platform_target_sha256,
            tuf_targets_version=self.tuf_targets_version,
            release_digest=self.release_digest,
            build_digest=self.build_digest,
            platform_version=self.platform_version,
            deployment_bundle_digest=self.deployment_bundle_digest,
            api_image=self.api_image,
            worker_image=self.worker_image,
            database_revision=self.database_revision,
        )

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> Self:
        if (
            set(document)
            != {
                "api_image",
                "build_digest",
                "database_revision",
                "deployment_bundle_digest",
                "generation_id",
                "operation_id",
                "plan_digest",
                "platform_target_name",
                "platform_target_sha256",
                "platform_version",
                "release_digest",
                "schema_version",
                "tuf_targets_version",
                "worker_image",
            }
            or document.get("schema_version") != 1
        ):
            raise HostStateConflict("host operation plan fields are invalid")
        try:
            return cls(
                **{key: document[key] for key in document if key != "schema_version"}
            )  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise HostStateConflict("host operation plan is invalid") from error


@dataclass(frozen=True)
class GenerationReceipt:
    generation_id: str
    platform_target_name: str
    platform_target_sha256: str
    tuf_targets_version: int
    release_digest: str
    build_digest: str
    platform_version: str
    deployment_bundle_digest: str
    api_image: str
    worker_image: str
    database_revision: str

    def __post_init__(self) -> None:
        synthetic = type(
            "_GenerationIdentity",
            (),
            {
                **self.__dict__,
                "operation_id": "generation-receipt",
                "plan_digest": "sha256:" + "0" * 64,
            },
        )()
        _validate_identity_fields(synthetic)

    @classmethod
    def from_plan(cls, plan: HostOperationPlan) -> Self:
        return plan.generation_receipt()

    def document(self) -> dict[str, object]:
        return {
            "api_image": self.api_image,
            "build_digest": self.build_digest,
            "database_revision": self.database_revision,
            "deployment_bundle_digest": self.deployment_bundle_digest,
            "generation_id": self.generation_id,
            "platform_target_name": self.platform_target_name,
            "platform_target_sha256": self.platform_target_sha256,
            "platform_version": self.platform_version,
            "receipt_kind": "generation",
            "release_digest": self.release_digest,
            "schema_version": 1,
            "tuf_targets_version": self.tuf_targets_version,
            "worker_image": self.worker_image,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> Self:
        if (
            document.get("receipt_kind") != "generation"
            or document.get("schema_version") != 1
        ):
            raise HostStateConflict("generation receipt kind is invalid")
        base = dict(document)
        base.pop("receipt_kind", None)
        base.pop("schema_version", None)
        try:
            return cls(**base)  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise HostStateConflict("generation receipt is invalid") from error


@dataclass(frozen=True)
class SelectionReceipt:
    operation_id: str
    plan_digest: str
    generation: GenerationReceipt
    previous_generation: str | None

    def __post_init__(self) -> None:
        if (
            not _valid_identifier(self.operation_id)
            or _DIGEST.fullmatch(self.plan_digest) is None
            or not isinstance(self.generation, GenerationReceipt)
            or (
                self.previous_generation is not None
                and not _valid_identifier(self.previous_generation)
            )
        ):
            raise ValueError("selection receipt is invalid")

    @classmethod
    def from_plan(
        cls,
        plan: HostOperationPlan,
        *,
        previous_generation: str | None,
    ) -> Self:
        return cls(
            operation_id=plan.operation_id,
            plan_digest=plan.plan_digest,
            generation=plan.generation_receipt(),
            previous_generation=previous_generation,
        )

    @classmethod
    def for_generation(
        cls,
        generation: GenerationReceipt,
        *,
        operation_id: str,
        plan_digest: str,
        previous_generation: str | None,
    ) -> Self:
        return cls(operation_id, plan_digest, generation, previous_generation)

    @property
    def generation_id(self) -> str:
        return self.generation.generation_id

    def document(self) -> dict[str, object]:
        generation_raw = _canonical(self.generation.document())
        return {
            "generation": self.generation.document(),
            "generation_receipt_sha256": _digest(generation_raw),
            "operation_id": self.operation_id,
            "plan_digest": self.plan_digest,
            "previous_generation": self.previous_generation,
            "receipt_kind": "selection",
            "schema_version": 1,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, object]) -> Self:
        if (
            set(document)
            != {
                "generation",
                "generation_receipt_sha256",
                "operation_id",
                "plan_digest",
                "previous_generation",
                "receipt_kind",
                "schema_version",
            }
            or document.get("receipt_kind") != "selection"
            or document.get("schema_version") != 1
        ):
            raise HostStateConflict("selection receipt fields are invalid")
        generation_document = document.get("generation")
        if not isinstance(generation_document, dict):
            raise HostStateConflict("selection generation receipt is invalid")
        generation = GenerationReceipt.from_document(generation_document)
        expected_generation_digest = _digest(_canonical(generation.document()))
        if document.get("generation_receipt_sha256") != expected_generation_digest:
            raise HostStateConflict("selection generation receipt digest is invalid")
        try:
            return cls(
                operation_id=document["operation_id"],  # type: ignore[arg-type]
                plan_digest=document["plan_digest"],  # type: ignore[arg-type]
                generation=generation,
                previous_generation=document["previous_generation"],  # type: ignore[arg-type]
            )
        except (TypeError, ValueError) as error:
            raise HostStateConflict("selection receipt is invalid") from error


@dataclass(frozen=True)
class CandidateProjection:
    projection_kind: str
    operation_id: str
    plan_digest: str
    generation_id: str
    platform_target_name: str
    platform_target_sha256: str
    tuf_targets_version: int
    release_digest: str
    build_digest: str
    platform_version: str
    deployment_bundle_digest: str
    api_image: str
    worker_image: str
    database_revision: str


@dataclass(frozen=True)
class SelectedGeneration:
    projection_kind: str
    operation_id: str
    plan_digest: str
    generation_id: str
    platform_target_name: str
    platform_target_sha256: str
    tuf_targets_version: int
    release_digest: str
    build_digest: str
    platform_version: str
    deployment_bundle_digest: str
    api_image: str
    worker_image: str
    database_revision: str
    previous_generation: str | None
    generation_receipt_sha256: str
    selection_receipt_sha256: str
    projection_sequence: int


def _candidate_document(plan: HostOperationPlan) -> dict[str, object]:
    document = plan.document()
    document["projection_kind"] = "candidate"
    return document


def _active_document(
    receipt: SelectionReceipt,
    *,
    sequence: int,
) -> dict[str, object]:
    selection = receipt.document()
    return {
        "generation_receipt_sha256": selection["generation_receipt_sha256"],
        "projection_kind": "active",
        "projection_sequence": sequence,
        "schema_version": 1,
        "selection": selection,
        "selection_receipt_sha256": _digest(_canonical(selection)),
    }


def _selection_intent(active_raw: bytes) -> bytes:
    active_document = _parse_mapping(active_raw, "active projection")
    selected = _parse_active(active_raw)
    return _canonical(
        {
            "active_projection": active_document,
            "active_projection_sha256": _digest(active_raw),
            "generation_id": selected.generation_id,
            "intent_kind": "selection",
            "schema_version": 1,
        }
    )


def _parse_selection_intent(raw: bytes) -> tuple[bytes, SelectedGeneration]:
    document = _parse_mapping(raw, "selection intent")
    if (
        set(document)
        != {
            "active_projection",
            "active_projection_sha256",
            "generation_id",
            "intent_kind",
            "schema_version",
        }
        or document.get("intent_kind") != "selection"
        or document.get("schema_version") != 1
    ):
        raise HostStateConflict("selection intent fields are invalid")
    active_document = document["active_projection"]
    if not isinstance(active_document, dict):
        raise HostStateConflict("selection intent projection is invalid")
    active_raw = _canonical(active_document)
    selected = _parse_active(active_raw)
    if (
        document["active_projection_sha256"] != _digest(active_raw)
        or document["generation_id"] != selected.generation_id
    ):
        raise HostStateConflict("selection intent binding is invalid")
    return active_raw, selected


def _parse_candidate(raw: bytes, operation_id: str) -> CandidateProjection:
    document = _parse_mapping(raw, "candidate projection")
    if document.get("projection_kind") != "candidate":
        raise HostStateConflict("candidate projection kind is invalid")
    content = dict(document)
    content.pop("projection_kind")
    plan = HostOperationPlan.from_document(content)
    if plan.operation_id != operation_id:
        raise HostStateConflict("candidate projection operation binding is invalid")
    return CandidateProjection(projection_kind="candidate", **plan.__dict__)


def _parse_active(raw: bytes) -> SelectedGeneration:
    document = _parse_mapping(raw, "active projection")
    if document.get("projection_kind") != "active":
        raise HostStateConflict("active projection kind is invalid")
    if (
        set(document)
        != {
            "generation_receipt_sha256",
            "projection_kind",
            "projection_sequence",
            "schema_version",
            "selection",
            "selection_receipt_sha256",
        }
        or document.get("schema_version") != 1
    ):
        raise HostStateConflict("active projection fields are invalid")
    sequence = document["projection_sequence"]
    selection_document = document["selection"]
    if (
        type(sequence) is not int
        or sequence < 1
        or not isinstance(selection_document, dict)
    ):
        raise HostStateConflict("active projection is invalid")
    receipt = SelectionReceipt.from_document(selection_document)
    generation_sha256 = _digest(_canonical(receipt.generation.document()))
    selection_sha256 = _digest(_canonical(receipt.document()))
    if document.get("generation_receipt_sha256") != generation_sha256:
        raise HostStateConflict("active projection receipt digest binding is invalid")
    if document.get("selection_receipt_sha256") != selection_sha256:
        raise HostStateConflict("active projection selection digest binding is invalid")
    generation = receipt.generation
    return SelectedGeneration(
        projection_kind="active",
        operation_id=receipt.operation_id,
        plan_digest=receipt.plan_digest,
        generation_id=generation.generation_id,
        platform_target_name=generation.platform_target_name,
        platform_target_sha256=generation.platform_target_sha256,
        tuf_targets_version=generation.tuf_targets_version,
        release_digest=generation.release_digest,
        build_digest=generation.build_digest,
        platform_version=generation.platform_version,
        deployment_bundle_digest=generation.deployment_bundle_digest,
        api_image=generation.api_image,
        worker_image=generation.worker_image,
        database_revision=generation.database_revision,
        previous_generation=receipt.previous_generation,
        generation_receipt_sha256=generation_sha256,
        selection_receipt_sha256=selection_sha256,
        projection_sequence=sequence,
    )


class HostOperationLock:
    """The single nonblocking host mutation lock, held for an operation lifetime."""

    def __init__(self, state_root: Path, *, owner_uid: int = 0) -> None:
        self._state_root = _validate_absolute(Path(state_root), "host state root")
        if (
            isinstance(owner_uid, bool)
            or not isinstance(owner_uid, int)
            or owner_uid < 0
        ):
            raise ValueError("host owner UID is invalid")
        self._owner_uid = owner_uid
        self._descriptor: int | None = None
        self._depth = 0

    def __enter__(self) -> Self:
        if self._descriptor is not None:
            self._depth += 1
            return self
        root = _ensure_directory_path(
            self._state_root,
            mode=0o700,
            label="host state root",
            expected_uid=self._owner_uid,
        )
        try:
            try:
                descriptor = os.open(
                    "operation.lock",
                    os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=root,
                )
            except OSError as error:
                raise HostStateConflict("host operation lock is unsafe") from error
            try:
                _validate_file(
                    os.fstat(descriptor),
                    mode=0o600,
                    label="host operation lock",
                    maximum=0,
                    allow_empty=True,
                    expected_uid=self._owner_uid,
                )
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(descriptor)
                raise HostStateConflict("another host operation is active") from None
            except Exception:
                os.close(descriptor)
                raise
            self._descriptor = descriptor
            self._depth = 1
            os.fsync(root)
            return self
        finally:
            os.close(root)

    def __exit__(self, *_args: object) -> None:
        if self._descriptor is None:
            return
        self._depth -= 1
        if self._depth > 0:
            return
        try:
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self._descriptor)
            self._descriptor = None
            self._depth = 0


def _validate_staging_entry(
    metadata: os.stat_result, label: str, *, expected_uid: int
) -> None:
    if not _owner_is_trusted(metadata, expected_uid):
        raise HostStateConflict(f"unsafe staging entry owner: {label}")
    mode = stat.S_IMODE(metadata.st_mode)
    if stat.S_ISDIR(metadata.st_mode):
        if mode != 0o700:
            raise HostStateConflict(f"unsafe staging entry mode: {label}")
        return
    if (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_nlink == 1
        and mode
        in {
            0o400,
            0o444,
            0o600,
            0o644,
            0o755,
        }
    ):
        return
    raise HostStateConflict(f"unsafe staging entry: {label}")


def _clear_directory(descriptor: int, label: str) -> None:
    expected_uid = os.fstat(descriptor).st_uid
    with os.scandir(descriptor) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            _validate_staging_entry(
                metadata, f"{label}/{name}", expected_uid=expected_uid
            )
            child = _open_directory_at(
                descriptor,
                name,
                mode=0o700,
                label=f"staging directory {name}",
            )
            try:
                _clear_directory(child, f"{label}/{name}")
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=descriptor)
        else:
            # Unlinking a non-directory entry by dirfd never follows it. This
            # safely removes links/devices left by an interrupted population.
            os.unlink(name, dir_fd=descriptor)
    os.fsync(descriptor)


def _validate_generation_tree(descriptor: int, label: str) -> None:
    expected_uid = os.fstat(descriptor).st_uid
    with os.scandir(descriptor) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        entry_label = f"{label}/{name}"
        _validate_staging_entry(metadata, entry_label, expected_uid=expected_uid)
        if stat.S_ISDIR(metadata.st_mode):
            child = _open_directory_at(
                descriptor,
                name,
                mode=0o700,
                label=entry_label,
            )
            try:
                _validate_generation_tree(child, entry_label)
            finally:
                os.close(child)
        elif not 0 < metadata.st_size <= 16 * 1024 * 1024:
            raise HostStateConflict(f"generation asset size is unsafe: {entry_label}")


class HostGenerationStore:
    """Root host generations and read-only container identity projections."""

    def __init__(
        self, state_root: Path, identity_root: Path, *, owner_uid: int = 0
    ) -> None:
        self._state_root = _validate_absolute(Path(state_root), "host state root")
        self._identity_root = _validate_absolute(
            Path(identity_root), "control identity root"
        )
        if (
            isinstance(owner_uid, bool)
            or not isinstance(owner_uid, int)
            or owner_uid < 0
        ):
            raise ValueError("host owner UID is invalid")
        self._owner_uid = owner_uid

    @property
    def state_root(self) -> Path:
        return self._state_root

    @property
    def identity_root(self) -> Path:
        return self._identity_root

    def initialize(self) -> None:
        host = _ensure_directory_path(
            self._state_root,
            mode=0o700,
            label="host state root",
            expected_uid=self._owner_uid,
        )
        try:
            for name in ("generations", "operations"):
                child = _ensure_directory_at(
                    host, name, mode=0o700, label=f"host {name} directory"
                )
                os.close(child)
        finally:
            os.close(host)
        identity = _ensure_directory_path(
            self._identity_root,
            mode=0o755,
            label="control identity root",
            expected_uid=self._owner_uid,
        )
        try:
            candidates = _ensure_directory_at(
                identity,
                "candidates",
                mode=0o755,
                label="candidate identity directory",
            )
            os.close(candidates)
        finally:
            os.close(identity)

    def prepare_staging(
        self,
        generation_id: str,
        populate: Callable[[Path], object],
    ) -> Path:
        if not _valid_identifier(generation_id):
            raise HostStateConflict("generation ID is invalid")
        if not callable(populate):
            raise TypeError("staging populate callback must be callable")
        self.initialize()
        host = _open_directory_path(
            self._state_root,
            mode=0o700,
            label="host state root",
            expected_uid=self._owner_uid,
        )
        try:
            generations = _open_directory_at(
                host,
                "generations",
                mode=0o700,
                label="host generations directory",
            )
            try:
                staging_name = f".{generation_id}.staging"
                if _exists_at(generations, generation_id):
                    raise HostStateConflict("control generation already exists")
                if _exists_at(generations, staging_name):
                    staging = _open_directory_at(
                        generations,
                        staging_name,
                        mode=0o700,
                        label="interrupted generation staging directory",
                    )
                    try:
                        _clear_directory(staging, staging_name)
                    finally:
                        os.close(staging)
                    os.rmdir(staging_name, dir_fd=generations)
                    os.fsync(generations)
                callback_path = Path(f"/proc/self/fd/{generations}/{staging_name}")
                try:
                    populate(callback_path)
                except Exception:
                    if _exists_at(generations, staging_name):
                        staging = _open_directory_at(
                            generations,
                            staging_name,
                            mode=0o700,
                            label="failed generation staging directory",
                        )
                        try:
                            _clear_directory(staging, staging_name)
                        finally:
                            os.close(staging)
                        os.rmdir(staging_name, dir_fd=generations)
                        os.fsync(generations)
                    raise
                staging = _open_directory_at(
                    generations,
                    staging_name,
                    mode=0o700,
                    label="generation staging directory",
                )
                os.close(staging)
                os.fsync(generations)
            finally:
                os.close(generations)
        finally:
            os.close(host)
        return self._state_root / "generations" / f".{generation_id}.staging"

    def commit_generation(
        self,
        staged: Path,
        receipt: GenerationReceipt,
    ) -> Path:
        if not isinstance(receipt, GenerationReceipt):
            raise HostStateConflict("generation receipt is invalid")
        expected = (
            self._state_root / "generations" / f".{receipt.generation_id}.staging"
        )
        if Path(staged) != expected:
            raise HostStateConflict("generation staging path is not trusted")
        host = _open_directory_path(
            self._state_root,
            mode=0o700,
            label="host state root",
            expected_uid=self._owner_uid,
        )
        try:
            generations = _open_directory_at(
                host,
                "generations",
                mode=0o700,
                label="host generations directory",
            )
            try:
                staging_name = f".{receipt.generation_id}.staging"
                staging = _open_directory_at(
                    generations,
                    staging_name,
                    mode=0o700,
                    label="generation staging directory",
                )
                try:
                    _validate_generation_tree(
                        staging, f"generation {receipt.generation_id}"
                    )
                    _write_new_at(
                        staging,
                        "generation.json",
                        _canonical(receipt.document()),
                        mode=0o400,
                        label="generation receipt",
                    )
                    _validate_generation_tree(
                        staging, f"generation {receipt.generation_id}"
                    )
                    os.fsync(staging)
                finally:
                    os.close(staging)
                if _exists_at(generations, receipt.generation_id):
                    raise HostStateConflict("control generation already exists")
                os.rename(
                    staging_name,
                    receipt.generation_id,
                    src_dir_fd=generations,
                    dst_dir_fd=generations,
                )
                os.fsync(generations)
            finally:
                os.close(generations)
        finally:
            os.close(host)
        return self._state_root / "generations" / receipt.generation_id

    def load_generation(self, generation_id: str) -> GenerationReceipt:
        receipt, _raw = self._load_generation(generation_id)
        return receipt

    def _load_generation(self, generation_id: str) -> tuple[GenerationReceipt, bytes]:
        if not _valid_identifier(generation_id):
            raise HostStateConflict("generation ID is invalid")
        host = _open_directory_path(
            self._state_root,
            mode=0o700,
            label="host state root",
            expected_uid=self._owner_uid,
        )
        try:
            generations = _open_directory_at(
                host,
                "generations",
                mode=0o700,
                label="host generations directory",
            )
            try:
                generation = _open_directory_at(
                    generations,
                    generation_id,
                    mode=0o700,
                    label="selected generation directory",
                )
                try:
                    raw = _read_file_at(
                        generation,
                        "generation.json",
                        mode=0o400,
                        label="generation receipt",
                    )
                finally:
                    os.close(generation)
            finally:
                os.close(generations)
        finally:
            os.close(host)
        parsed = GenerationReceipt.from_document(
            _parse_mapping(raw, "generation receipt")
        )
        if parsed.generation_id != generation_id or raw != _canonical(
            parsed.document()
        ):
            raise HostStateConflict("generation receipt binding is invalid")
        return parsed, raw

    def select(self, receipt: SelectionReceipt) -> SelectedGeneration:
        if not isinstance(receipt, SelectionReceipt):
            raise HostStateConflict("selection receipt is invalid")
        generation, raw_receipt = self._load_generation(receipt.generation_id)
        if generation != receipt.generation:
            raise HostStateConflict("selection generation binding is invalid")
        current = self.reconcile_selection()
        sequence = 1 if current is None else current.projection_sequence + 1
        active_raw = _canonical(
            _active_document(
                receipt,
                sequence=sequence,
            )
        )
        if _parse_active(active_raw).generation_receipt_sha256 != _digest(raw_receipt):
            raise HostStateConflict("selection generation receipt digest is invalid")
        intent_raw = _selection_intent(active_raw)
        host = _open_directory_path(
            self._state_root,
            mode=0o700,
            label="host state root",
            expected_uid=self._owner_uid,
        )
        identity = _open_directory_path(
            self._identity_root,
            mode=0o755,
            label="control identity root",
            expected_uid=self._owner_uid,
        )
        try:
            if _exists_at(host, "selection.pending.json"):
                existing_intent = _read_file_at(
                    host,
                    "selection.pending.json",
                    mode=0o600,
                    label="selection intent",
                )
                if existing_intent != intent_raw:
                    raise HostStateConflict("another selection intent is pending")
            else:
                _write_atomic_at(
                    host,
                    "selection.pending.json",
                    intent_raw,
                    mode=0o600,
                    label="selection intent",
                )
            _write_atomic_at(
                identity,
                "active.json",
                active_raw,
                mode=0o444,
                label="active identity projection",
            )
            _write_atomic_at(
                host,
                "active-generation",
                (receipt.generation_id + "\n").encode("ascii"),
                mode=0o600,
                label="active generation pointer",
            )
            os.unlink("selection.pending.json", dir_fd=host)
            os.fsync(host)
        finally:
            os.close(identity)
            os.close(host)
        return _parse_active(active_raw)

    def project_candidate(self, operation: HostOperationPlan) -> CandidateProjection:
        if not isinstance(operation, HostOperationPlan):
            raise HostStateConflict("host operation plan is invalid")
        self.initialize()
        candidate_raw = _canonical(_candidate_document(operation))
        identity = _open_directory_path(
            self._identity_root,
            mode=0o755,
            label="control identity root",
            expected_uid=self._owner_uid,
        )
        try:
            candidates = _open_directory_at(
                identity,
                "candidates",
                mode=0o755,
                label="candidate identity directory",
            )
            try:
                name = operation.operation_id + ".json"
                if _exists_at(candidates, name):
                    existing = _read_file_at(
                        candidates,
                        name,
                        mode=0o444,
                        label="candidate identity projection",
                    )
                    if existing != candidate_raw:
                        raise HostStateConflict(
                            "candidate projection operation already exists"
                        )
                else:
                    _write_atomic_at(
                        candidates,
                        name,
                        candidate_raw,
                        mode=0o444,
                        label="candidate identity projection",
                    )
            finally:
                os.close(candidates)
        finally:
            os.close(identity)
        return _parse_candidate(candidate_raw, operation.operation_id)

    def _load_identity_active(self) -> SelectedGeneration | None:
        try:
            identity = _open_directory_path(
                self._identity_root,
                mode=0o755,
                label="control identity root",
                expected_uid=self._owner_uid,
            )
        except HostStateConflict:
            if not self._identity_root.exists():
                return None
            raise
        try:
            try:
                raw = _read_file_at(
                    identity,
                    "active.json",
                    mode=0o444,
                    label="active identity projection",
                )
            except HostStateConflict:
                if not _exists_at(identity, "active.json"):
                    return None
                raise
        finally:
            os.close(identity)
        return _parse_active(raw)

    def load_active_projection(self) -> SelectedGeneration | None:
        """Load only the read-only identity projection, without host authority.

        Container callers use this method because they mount only
        ``control-identity``. Root host operations use :meth:`load_active`,
        which additionally reconciles the host pointer and selection intent.
        """

        return self._load_identity_active()

    def load_pointer(self) -> str | None:
        try:
            host = _open_directory_path(
                self._state_root,
                mode=0o700,
                label="host state root",
                expected_uid=self._owner_uid,
            )
        except HostStateConflict:
            if not self._state_root.exists():
                return None
            raise
        try:
            if not _exists_at(host, "active-generation"):
                return None
            raw = _read_file_at(
                host,
                "active-generation",
                mode=0o600,
                label="active generation pointer",
                maximum=256,
            )
        finally:
            os.close(host)
        try:
            value = raw.decode("ascii")
        except UnicodeDecodeError as error:
            raise HostStateConflict("active generation pointer is invalid") from error
        if not value.endswith("\n") or value.count("\n") != 1:
            raise HostStateConflict("active generation pointer is invalid")
        generation_id = value[:-1]
        if not _valid_identifier(generation_id):
            raise HostStateConflict("active generation pointer is invalid")
        return generation_id

    def _load_selection_intent(self) -> tuple[bytes, SelectedGeneration] | None:
        try:
            host = _open_directory_path(
                self._state_root,
                mode=0o700,
                label="host state root",
                expected_uid=self._owner_uid,
            )
        except HostStateConflict:
            if not self._state_root.exists():
                return None
            raise
        try:
            if not _exists_at(host, "selection.pending.json"):
                return None
            raw = _read_file_at(
                host,
                "selection.pending.json",
                mode=0o600,
                label="selection intent",
            )
        finally:
            os.close(host)
        return _parse_selection_intent(raw)

    def reconcile_selection(self) -> SelectedGeneration | None:
        active = self._load_identity_active()
        pointer = self.load_pointer()
        intent = self._load_selection_intent()
        if intent is not None:
            intended_raw, intended = intent
            active_is_intended = active == intended
            pointer_is_intended = pointer == intended.generation_id
            old_state_is_consistent = (active is None and pointer is None) or (
                active is not None and pointer == active.generation_id
            )
            if active_is_intended or pointer_is_intended:
                host = _open_directory_path(
                    self._state_root,
                    mode=0o700,
                    label="host state root",
                    expected_uid=self._owner_uid,
                )
                identity = _open_directory_path(
                    self._identity_root,
                    mode=0o755,
                    label="control identity root",
                    expected_uid=self._owner_uid,
                )
                try:
                    if not active_is_intended:
                        _write_atomic_at(
                            identity,
                            "active.json",
                            intended_raw,
                            mode=0o444,
                            label="active identity projection",
                        )
                    if not pointer_is_intended:
                        _write_atomic_at(
                            host,
                            "active-generation",
                            (intended.generation_id + "\n").encode("ascii"),
                            mode=0o600,
                            label="active generation pointer",
                        )
                    os.unlink("selection.pending.json", dir_fd=host)
                    os.fsync(host)
                finally:
                    os.close(identity)
                    os.close(host)
                return intended
            if old_state_is_consistent:
                return active
            raise HostStateConflict("pending selection state is inconsistent")
        if active is None and pointer is None:
            return None
        if active is None or pointer != active.generation_id:
            raise HostStateConflict("active pointer and projection disagree")
        return active

    def load_active(self) -> SelectedGeneration | None:
        return self.reconcile_selection()

    def load_candidate(self, operation_id: str) -> CandidateProjection:
        if not _valid_identifier(operation_id):
            raise HostStateConflict("operation ID is invalid")
        identity = _open_directory_path(
            self._identity_root,
            mode=0o755,
            label="control identity root",
            expected_uid=self._owner_uid,
        )
        try:
            candidates = _open_directory_at(
                identity,
                "candidates",
                mode=0o755,
                label="candidate identity directory",
            )
            try:
                raw = _read_file_at(
                    candidates,
                    operation_id + ".json",
                    mode=0o444,
                    label="candidate identity projection",
                )
            finally:
                os.close(candidates)
        finally:
            os.close(identity)
        return _parse_candidate(raw, operation_id)

    def remove_candidate(self, operation_id: str) -> None:
        if not _valid_identifier(operation_id):
            raise HostStateConflict("operation ID is invalid")
        identity = _open_directory_path(
            self._identity_root,
            mode=0o755,
            label="control identity root",
            expected_uid=self._owner_uid,
        )
        try:
            candidates = _open_directory_at(
                identity,
                "candidates",
                mode=0o755,
                label="candidate identity directory",
            )
            try:
                name = operation_id + ".json"
                if not _exists_at(candidates, name):
                    return
                _read_file_at(
                    candidates,
                    name,
                    mode=0o444,
                    label="candidate identity projection",
                )
                os.unlink(name, dir_fd=candidates)
                os.fsync(candidates)
            finally:
                os.close(candidates)
        finally:
            os.close(identity)


@dataclass(frozen=True)
class JournalEntry:
    sequence: int
    phase: str
    evidence: Mapping[str, object]
    previous_entry_digest: str | None
    entry_digest: str
    timestamp: str
    raw: bytes


@dataclass(frozen=True)
class JournalState:
    operation_id: str
    plan_document: Mapping[str, object]
    entries: tuple[JournalEntry, ...]

    @property
    def plan_digest(self) -> str:
        value = self.plan_document["plan_digest"]
        assert isinstance(value, str)
        return value

    @property
    def terminal(self) -> bool:
        return bool(self.entries and self.entries[-1].phase in _TERMINAL_PHASES)


class PhaseJournal:
    """Immutable canonical operation plans with contiguous hash-chained phases."""

    def __init__(
        self,
        state_root: Path,
        *,
        operation_id: str | None = None,
        clock: Callable[[], str] | None = None,
        owner_uid: int = 0,
    ) -> None:
        self._state_root = _validate_absolute(Path(state_root), "host state root")
        if operation_id is not None and not _valid_identifier(operation_id):
            raise HostStateConflict("operation ID is invalid")
        self._operation_id = operation_id
        if (
            isinstance(owner_uid, bool)
            or not isinstance(owner_uid, int)
            or owner_uid < 0
        ):
            raise ValueError("host owner UID is invalid")
        self._owner_uid = owner_uid
        self._clock = clock or (
            lambda: datetime.now(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

    def _layout(self) -> tuple[int, int]:
        host = _ensure_directory_path(
            self._state_root,
            mode=0o700,
            label="host state root",
            expected_uid=self._owner_uid,
        )
        operations = _ensure_directory_at(
            host, "operations", mode=0o700, label="host operations directory"
        )
        return host, operations

    @staticmethod
    def _plan_document(
        plan: HostOperationPlan | Mapping[str, object],
    ) -> dict[str, object]:
        document = (
            plan.document() if isinstance(plan, HostOperationPlan) else dict(plan)
        )
        operation_id = document.get("operation_id")
        plan_digest = document.get("plan_digest")
        raw = _canonical(document)
        if (
            not _valid_identifier(operation_id)
            or not isinstance(plan_digest, str)
            or _DIGEST.fullmatch(plan_digest) is None
            or not 0 < len(raw) <= _MAX_DOCUMENT_BYTES
        ):
            raise HostStateConflict("host operation plan is invalid")
        return document

    def create(self, plan: HostOperationPlan | Mapping[str, object]) -> JournalState:
        plan_document = self._plan_document(plan)
        operation_id = plan_document["operation_id"]
        assert isinstance(operation_id, str)
        if self._operation_id is not None and self._operation_id != operation_id:
            raise HostStateConflict("journal operation binding is invalid")
        self._operation_id = operation_id
        host, operations = self._layout()
        try:
            with os.scandir(operations) as entries:
                existing_names = sorted(entry.name for entry in entries)
            for name in existing_names:
                if name.startswith("."):
                    continue
                existing_state = self._load_operation(operations, name)
                if name != operation_id and not existing_state.terminal:
                    raise HostStateConflict("another host operation is pending")
            if _exists_at(operations, operation_id):
                existing = self._load_operation(operations, operation_id)
                if dict(existing.plan_document) != plan_document:
                    raise HostStateConflict("operation journal already exists")
                return existing
            staging_name = f".{operation_id}.creating"
            if _exists_at(operations, staging_name):
                staging = _open_directory_at(
                    operations,
                    staging_name,
                    mode=0o700,
                    label="incomplete operation journal directory",
                )
                try:
                    _clear_directory(staging, staging_name)
                finally:
                    os.close(staging)
                os.rmdir(staging_name, dir_fd=operations)
            os.mkdir(staging_name, 0o700, dir_fd=operations)
            operation = _open_directory_at(
                operations,
                staging_name,
                mode=0o700,
                label="operation journal staging directory",
            )
            try:
                _write_new_at(
                    operation,
                    "plan.json",
                    _canonical(plan_document),
                    mode=0o400,
                    label="operation plan",
                )
                os.fsync(operation)
            finally:
                os.close(operation)
            os.rename(
                staging_name,
                operation_id,
                src_dir_fd=operations,
                dst_dir_fd=operations,
            )
            os.fsync(operations)
        finally:
            os.close(operations)
            os.close(host)
        return JournalState(operation_id, plan_document, ())

    def append(
        self,
        phase: str,
        evidence: Mapping[str, object],
    ) -> JournalState:
        if self._operation_id is None:
            raise HostStateConflict("journal operation is not selected")
        if not isinstance(phase, str) or _PHASE.fullmatch(phase) is None:
            raise HostStateConflict("journal phase is invalid")
        if not isinstance(evidence, Mapping):
            raise HostStateConflict("journal evidence is invalid")
        evidence_document = dict(evidence)
        _canonical(evidence_document)
        host, operations = self._layout()
        try:
            state = self._load_operation(operations, self._operation_id)
            if state.terminal:
                raise HostStateConflict("operation journal is already terminal")
            sequence = len(state.entries) + 1
            previous = state.entries[-1].entry_digest if state.entries else None
            timestamp = self._clock()
            if not isinstance(timestamp, str) or not 1 <= len(timestamp) <= 64:
                raise HostStateConflict("journal timestamp is invalid")
            core = {
                "evidence": evidence_document,
                "operation_id": state.operation_id,
                "phase": phase,
                "plan_digest": state.plan_digest,
                "previous_entry_digest": previous,
                "schema_version": 1,
                "sequence": sequence,
                "timestamp": timestamp,
            }
            document = {
                **core,
                "entry_digest": _DIGEST_PREFIX + _digest(_canonical(core)),
            }
            raw_document = _canonical(document)
            if len(raw_document) > _MAX_DOCUMENT_BYTES:
                raise HostStateConflict("journal phase entry exceeds its size bound")
            operation = _open_directory_at(
                operations,
                state.operation_id,
                mode=0o700,
                label="operation journal directory",
            )
            try:
                _write_new_atomic_at(
                    operation,
                    f"{sequence:04d}-{phase}.json",
                    raw_document,
                    mode=0o400,
                    label="operation phase entry",
                )
            finally:
                os.close(operation)
            return self._load_operation(operations, state.operation_id)
        finally:
            os.close(operations)
            os.close(host)

    def _load_operation(self, operations: int, operation_id: str) -> JournalState:
        operation = _open_directory_at(
            operations,
            operation_id,
            mode=0o700,
            label="operation journal directory",
        )
        try:
            plan_raw = _read_file_at(
                operation,
                "plan.json",
                mode=0o400,
                label="operation plan",
            )
            plan_document = _parse_mapping(plan_raw, "operation plan")
            normalized_plan = self._plan_document(plan_document)
            if normalized_plan["operation_id"] != operation_id:
                raise HostStateConflict("operation plan binding is invalid")
            with os.scandir(operation) as directory_entries:
                names = sorted(entry.name for entry in directory_entries)
            entry_names = [name for name in names if name != "plan.json"]
            entries: list[JournalEntry] = []
            previous: str | None = None
            for expected_sequence, name in enumerate(entry_names, start=1):
                match = _ENTRY_NAME.fullmatch(name)
                if match is None or int(match.group(1)) != expected_sequence:
                    raise HostStateConflict("journal phases are not contiguous")
                raw = _read_file_at(
                    operation,
                    name,
                    mode=0o400,
                    label="operation phase entry",
                )
                document = _parse_mapping(raw, "operation phase entry")
                if set(document) != {
                    "entry_digest",
                    "evidence",
                    "operation_id",
                    "phase",
                    "plan_digest",
                    "previous_entry_digest",
                    "schema_version",
                    "sequence",
                    "timestamp",
                }:
                    raise HostStateConflict("journal phase fields are invalid")
                phase = document["phase"]
                sequence = document["sequence"]
                evidence = document["evidence"]
                entry_digest = document["entry_digest"]
                core = dict(document)
                core.pop("entry_digest")
                valid = (
                    document["schema_version"] == 1
                    and document["operation_id"] == operation_id
                    and document["plan_digest"] == normalized_plan["plan_digest"]
                    and type(sequence) is int
                    and sequence == expected_sequence
                    and phase == match.group(2)
                    and isinstance(phase, str)
                    and _PHASE.fullmatch(phase) is not None
                    and isinstance(evidence, dict)
                    and document["previous_entry_digest"] == previous
                    and isinstance(entry_digest, str)
                    and entry_digest == _DIGEST_PREFIX + _digest(_canonical(core))
                    and isinstance(document["timestamp"], str)
                    and 1 <= len(document["timestamp"]) <= 64
                )
                if not valid:
                    if entry_digest != _DIGEST_PREFIX + _digest(_canonical(core)):
                        raise HostStateConflict("journal entry digest is invalid")
                    raise HostStateConflict("journal phase chain is invalid")
                entries.append(
                    JournalEntry(
                        sequence=sequence,
                        phase=phase,
                        evidence=evidence,
                        previous_entry_digest=previous,
                        entry_digest=entry_digest,
                        timestamp=document["timestamp"],
                        raw=raw,
                    )
                )
                previous = entry_digest
            return JournalState(operation_id, normalized_plan, tuple(entries))
        finally:
            os.close(operation)

    def load_pending(self) -> JournalState | None:
        try:
            host = _open_directory_path(
                self._state_root,
                mode=0o700,
                label="host state root",
                expected_uid=self._owner_uid,
            )
        except HostStateConflict:
            if not self._state_root.exists():
                return None
            raise
        try:
            try:
                operations = _open_directory_at(
                    host,
                    "operations",
                    mode=0o700,
                    label="host operations directory",
                )
            except HostStateConflict:
                if not _exists_at(host, "operations"):
                    return None
                raise
            try:
                with os.scandir(operations) as directory_entries:
                    names = sorted(entry.name for entry in directory_entries)
                pending: list[JournalState] = []
                for operation_id in names:
                    if operation_id.startswith("."):
                        raise HostStateConflict(
                            "incomplete operation journal publication exists"
                        )
                    if not _valid_identifier(operation_id):
                        raise HostStateConflict("operation journal name is invalid")
                    state = self._load_operation(operations, operation_id)
                    if not state.terminal:
                        pending.append(state)
                if len(pending) > 1:
                    raise HostStateConflict("multiple pending host operations exist")
                return pending[0] if pending else None
            finally:
                os.close(operations)
        finally:
            os.close(host)


_DIGEST_PREFIX = "sha256:"
