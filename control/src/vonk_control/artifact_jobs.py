"""Durable, bounded, content-addressed artifact-producing recipe jobs."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import AsyncIterable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    AgentProtocolError,
    RecipeJobFile,
    RecipeJobInputFile,
    RecipeJobOutputLimits,
    RecipeJobRunRequest,
    RecipeJobRunResult,
    canonical_message,
    recipe_job_manifest_document,
    recipe_job_manifest_sha256,
)
from vonk_forge_contracts import RecipeDefinition, content_sha256

from .artifact_blob_store import (
    ArtifactBlobStore,
    ArtifactBlobStoreError,
    StoredArtifactBlob,
)
from .models import (
    AgentOperation,
    ArtifactJob,
    ArtifactJobBlob,
    ArtifactJobFile,
    CatalogDocumentRevision,
    Job,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from .recipe_operations import RecipeOperationConflict, RecipeOperationService

MAX_INPUT_FILES = 32
MAX_INPUT_FILE_BYTES = 512 * 1024**2
MAX_INPUT_TOTAL_BYTES = 1024**3
_SLOT_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
_EXTENSION = re.compile(r"\.[a-z0-9][a-z0-9._-]{0,15}\Z")
_PARAMETER_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_UNSAFE_PARAMETER_KEY = re.compile(
    r"^(?:apikey|passwordhash)$|"
    r"(?:^|[_-])(?:password|secret|authorization|command|shell|environment)"
    r"(?:$|[_-])|"
    r"(?:^|[_-])(?:api|access|auth|bearer|github|hf|huggingface)[_-]?token$|"
    r"(?:^|[_-])private[_-]?key$|"
    r"^token$|"
    r"(?:^|[_-])(?:path|file|filename|filepath|directory|folder)(?:$|[_-])",
    re.IGNORECASE,
)


class ArtifactJobError(ValueError):
    pass


def _active_recipe_revision(
    session: Session, revision_id: str
) -> tuple[CatalogDocumentRevision, RecipeDefinition] | None:
    revision = session.get(CatalogDocumentRevision, revision_id)
    if (
        revision is None
        or revision.kind != "recipe"
        or revision.schema_version != 2
        or revision.state != "active"
    ):
        return None
    try:
        recipe = RecipeDefinition.model_validate(revision.document)
    except (TypeError, ValueError):
        return None
    if content_sha256(recipe) != revision.content_digest:
        return None
    return revision, recipe


@dataclass(frozen=True, slots=True)
class ArtifactJobView:
    id: str
    run_id: str
    operation_id: str | None
    interface: str
    state: str
    contract_sha256: str
    compiled_contract: dict[str, object]
    input_manifest_sha256: str
    input_total_bytes: int
    input_declarations: tuple[dict[str, object], ...]
    input_files: tuple[dict[str, object], ...]
    output_limits: dict[str, object]
    output_manifest_sha256: str | None
    output_files: tuple[dict[str, object], ...]
    result_evidence: dict[str, object] | None
    status_reason: str | None
    timeout_seconds: int
    created_at: datetime
    updated_at: datetime


def _json_copy(value: object) -> object:
    return json.loads(canonical_message(value))


def _recipe_interface(document: Mapping[str, object]) -> str:
    interfaces = document.get("interfaces")
    artifact_interfaces = (
        [
            item.get("adapter")
            for item in interfaces
            if isinstance(item, Mapping) and item.get("adapter") != "openai"
        ]
        if isinstance(interfaces, list)
        else []
    )
    if len(artifact_interfaces) != 1 or not isinstance(artifact_interfaces[0], str):
        raise ArtifactJobError("recipe interface is unavailable")
    return artifact_interfaces[0]


def _finite_parameter_number(value: object) -> bool:
    return (
        (isinstance(value, float) and math.isfinite(value))
        or (isinstance(value, int) and not isinstance(value, bool))
    )


def _settings_parameters(document: Mapping[str, object]) -> list[dict[str, object]]:
    settings = document.get("settings")
    knobs = settings.get("knobs") if isinstance(settings, Mapping) else None
    if knobs is None:
        return []
    if not isinstance(knobs, Mapping) or len(knobs) > 64:
        raise ArtifactJobError("artifact parameter contract is invalid")
    parameters: list[dict[str, object]] = []
    for name, setting in knobs.items():
        if not isinstance(name, str) or not isinstance(setting, Mapping):
            raise ArtifactJobError("artifact parameter contract is invalid")
        value = setting.get("value")
        if isinstance(value, bool):
            kind = "boolean"
        elif isinstance(value, int):
            kind = "integer"
        elif isinstance(value, float):
            kind = "float"
        elif isinstance(value, str):
            kind = "string"
        else:
            raise ArtifactJobError("artifact parameter contract is invalid")
        parameters.append(
            {
                "name": name,
                "type": kind,
                "default": value,
                "minimum": None,
                "maximum": None,
            }
        )
    return parameters


def _validate_parameter_definition(raw: Mapping[str, object]) -> dict[str, object]:
    name = raw.get("name")
    kind = raw.get("type")
    if (
        not isinstance(name, str)
        or _PARAMETER_NAME.fullmatch(name) is None
        or _UNSAFE_PARAMETER_KEY.search(name)
        or kind not in {"string", "integer", "float", "boolean", "enum"}
    ):
        raise ArtifactJobError("artifact parameter contract is invalid")
    default = raw.get("default")
    minimum = raw.get("minimum")
    maximum = raw.get("maximum")
    if kind == "string":
        valid = (
            isinstance(default, str)
            and "\x00" not in default
            and len(default.encode("utf-8")) <= 4096
            and minimum is None
            and maximum is None
        )
    elif kind == "integer":
        valid = (
            type(default) is int
            and type(minimum) in (int, type(None))
            and type(maximum) in (int, type(None))
        )
    elif kind == "float":
        valid = (
            _finite_parameter_number(default)
            and type(minimum) in (int, float, type(None))
            and type(maximum) in (int, float, type(None))
            and (minimum is None or _finite_parameter_number(minimum))
            and (maximum is None or _finite_parameter_number(maximum))
        )
    elif kind == "boolean":
        valid = type(default) is bool and minimum is None and maximum is None
    else:
        allowed = raw.get("allowed_values")
        valid = (
            isinstance(allowed, list)
            and 1 <= len(allowed) <= 128
            and all(
                isinstance(item, (str, int, float, bool))
                and not (isinstance(item, float) and not math.isfinite(item))
                for item in allowed
            )
            and default in allowed
            and minimum is None
            and maximum is None
        )
    if not valid or (
        minimum is not None
        and maximum is not None
        and minimum > maximum
    ):
        raise ArtifactJobError("artifact parameter contract is invalid")
    pattern = raw.get("pattern")
    if pattern is not None and (
        not isinstance(pattern, str)
        or "\x00" in pattern
        or len(pattern) > 256
    ):
        raise ArtifactJobError("artifact parameter contract is invalid")
    return {
        "name": name,
        "type": kind,
        "default": default,
        "minimum": minimum,
        "maximum": maximum,
        "allowed_values": list(raw.get("allowed_values", [])),
        "pattern": pattern,
    }


def _compile_contract(
    document: Mapping[str, object], interface_name: str
) -> dict[str, object]:
    interfaces = document.get("interfaces")
    interface = (
        next(
            (
                item
                for item in interfaces
                if isinstance(item, Mapping) and item.get("adapter") == interface_name
            ),
            None,
        )
        if isinstance(interfaces, list)
        else None
    )
    if interface is None:
        raise ArtifactJobError("artifact job interface contract is unavailable")
    raw_input = interface.get("input")
    if raw_input is None:
        input_contract: dict[str, object] = {
            "required": False,
            "media_types": [],
            "max_bytes": 0,
            "slots": [],
        }
    elif isinstance(raw_input, Mapping):
        media_types = raw_input.get("media_types")
        max_bytes = raw_input.get("max_bytes")
        required = raw_input.get("required")
        if (
            not isinstance(media_types, list)
            or not media_types
            or not all(isinstance(item, str) for item in media_types)
            or not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or not 1 <= max_bytes <= MAX_INPUT_TOTAL_BYTES
            or not isinstance(required, bool)
        ):
            raise ArtifactJobError("artifact input contract is invalid")
        aggregate_media = sorted(set(media_types))
        raw_slots = raw_input.get("slots")
        if raw_slots is None:
            raw_slots = [
                {
                    "id": "input",
                    "label": "Input",
                    "description": "Recipe input",
                    "media_types": aggregate_media,
                    "extensions": [],
                    "min_files": 1 if required else 0,
                    "max_files": MAX_INPUT_FILES,
                    "max_file_bytes": min(max_bytes, MAX_INPUT_FILE_BYTES),
                    "max_total_bytes": max_bytes,
                }
            ]
        if (
            not isinstance(raw_slots, list)
            or not 1 <= len(raw_slots) <= MAX_INPUT_FILES
        ):
            raise ArtifactJobError("artifact input slot contract is invalid")
        slots: list[dict[str, object]] = []
        for raw_slot in raw_slots:
            if not isinstance(raw_slot, Mapping):
                raise ArtifactJobError("artifact input slot contract is invalid")
            slot_id = raw_slot.get("id")
            label = raw_slot.get("label")
            description = raw_slot.get("description")
            slot_media = raw_slot.get("media_types")
            extensions = raw_slot.get("extensions")
            min_files = raw_slot.get("min_files")
            max_files = raw_slot.get("max_files")
            max_file_bytes = raw_slot.get("max_file_bytes")
            max_total_bytes = raw_slot.get("max_total_bytes")
            if (
                not isinstance(slot_id, str)
                or _SLOT_ID.fullmatch(slot_id) is None
                or not isinstance(label, str)
                or not 1 <= len(label) <= 64
                or not isinstance(description, str)
                or not 1 <= len(description) <= 256
                or not isinstance(slot_media, list)
                or not 1 <= len(slot_media) <= 16
                or not all(isinstance(item, str) for item in slot_media)
                or len(set(slot_media)) != len(slot_media)
                or not set(slot_media) <= set(aggregate_media)
                or not isinstance(extensions, list)
                or len(extensions) > 16
                or not all(
                    isinstance(item, str) and _EXTENSION.fullmatch(item) is not None
                    for item in extensions
                )
                or len(set(extensions)) != len(extensions)
                or not isinstance(min_files, int)
                or isinstance(min_files, bool)
                or not isinstance(max_files, int)
                or isinstance(max_files, bool)
                or not 0 <= min_files <= max_files <= MAX_INPUT_FILES
                or max_files < 1
                or not isinstance(max_file_bytes, int)
                or isinstance(max_file_bytes, bool)
                or not 1 <= max_file_bytes <= MAX_INPUT_FILE_BYTES
                or not isinstance(max_total_bytes, int)
                or isinstance(max_total_bytes, bool)
                or not max_file_bytes <= max_total_bytes <= max_bytes
            ):
                raise ArtifactJobError("artifact input slot contract is invalid")
            slots.append(
                {
                    "id": slot_id,
                    "label": label,
                    "description": description,
                    "media_types": sorted(slot_media),
                    "extensions": sorted(extensions),
                    "min_files": min_files,
                    "max_files": max_files,
                    "max_file_bytes": max_file_bytes,
                    "max_total_bytes": max_total_bytes,
                }
            )
        if len({str(item["id"]) for item in slots}) != len(slots):
            raise ArtifactJobError("artifact input slot ids must be unique")
        if required and not any(int(item["min_files"]) > 0 for item in slots):
            raise ArtifactJobError("required artifact input has no required slot")
        input_contract = {
            "required": required,
            "media_types": aggregate_media,
            "max_bytes": max_bytes,
            "slots": sorted(slots, key=lambda item: str(item["id"])),
        }
    else:
        raise ArtifactJobError("artifact input contract is invalid")
    raw_parameters = _settings_parameters(document)
    parameters: list[dict[str, object]] = []
    for raw in raw_parameters:
        if not isinstance(raw, Mapping):
            raise ArtifactJobError("artifact parameter contract is invalid")
        parameters.append(_validate_parameter_definition(raw))
    raw_output = interface.get("output")
    if not isinstance(raw_output, Mapping) or raw_output.get("path") != "/outputs":
        raise ArtifactJobError("artifact output contract is unavailable")
    aggregate_output_bytes = raw_output.get("max_total_bytes")
    raw_output_slots = raw_output.get("slots")
    if (
        not isinstance(aggregate_output_bytes, int)
        or isinstance(aggregate_output_bytes, bool)
        or not 1 <= aggregate_output_bytes <= 2 * 1024**3
        or not isinstance(raw_output_slots, list)
        or not 1 <= len(raw_output_slots) <= 32
    ):
        raise ArtifactJobError("artifact output contract is invalid")
    output_slots: list[dict[str, object]] = []
    for raw_slot in raw_output_slots:
        if not isinstance(raw_slot, Mapping):
            raise ArtifactJobError("artifact output slot contract is invalid")
        slot_id = raw_slot.get("id")
        label = raw_slot.get("label")
        description = raw_slot.get("description")
        slot_media = raw_slot.get("media_types")
        extensions = raw_slot.get("extensions")
        min_files = raw_slot.get("min_files")
        max_files = raw_slot.get("max_files")
        max_file_bytes = raw_slot.get("max_file_bytes")
        max_total_bytes = raw_slot.get("max_total_bytes")
        if (
            not isinstance(slot_id, str)
            or _SLOT_ID.fullmatch(slot_id) is None
            or not isinstance(label, str)
            or not 1 <= len(label) <= 64
            or not isinstance(description, str)
            or not 1 <= len(description) <= 256
            or not isinstance(slot_media, list)
            or len(slot_media) != 1
            or not all(isinstance(item, str) for item in slot_media)
            or len(set(slot_media)) != len(slot_media)
            or not isinstance(extensions, list)
            or not 1 <= len(extensions) <= 16
            or not all(
                isinstance(item, str) and _EXTENSION.fullmatch(item) is not None
                for item in extensions
            )
            or len(set(extensions)) != len(extensions)
            or not isinstance(min_files, int)
            or isinstance(min_files, bool)
            or not isinstance(max_files, int)
            or isinstance(max_files, bool)
            or not 0 <= min_files <= max_files <= 32
            or max_files < 1
            or not isinstance(max_file_bytes, int)
            or isinstance(max_file_bytes, bool)
            or not 1 <= max_file_bytes <= 1024**3
            or not isinstance(max_total_bytes, int)
            or isinstance(max_total_bytes, bool)
            or not max_file_bytes <= max_total_bytes <= aggregate_output_bytes
        ):
            raise ArtifactJobError("artifact output slot contract is invalid")
        output_slots.append(
            {
                "id": slot_id,
                "label": label,
                "description": description,
                "media_types": sorted(slot_media),
                "extensions": sorted(extensions),
                "min_files": min_files,
                "max_files": max_files,
                "max_file_bytes": max_file_bytes,
                "max_total_bytes": max_total_bytes,
            }
        )
    if len({str(item["id"]) for item in output_slots}) != len(output_slots):
        raise ArtifactJobError("artifact output slot ids must be unique")
    all_extensions = [
        str(extension) for slot in output_slots for extension in slot["extensions"]
    ]
    if len(set(all_extensions)) != len(all_extensions):
        raise ArtifactJobError("artifact output extensions must identify one slot")
    output_slots.sort(key=lambda item: str(item["id"]))
    output_media = sorted(
        {str(media) for slot in output_slots for media in slot["media_types"]}
    )
    output_contract = {
        "path": "/outputs",
        "max_total_bytes": aggregate_output_bytes,
        "slots": output_slots,
    }
    return {
        "schema_version": 1,
        "interface": interface_name,
        "input": input_contract,
        "parameters": sorted(parameters, key=lambda item: str(item["name"])),
        "output": output_contract,
        "output_limits": {
            "max_files": min(32, sum(int(item["max_files"]) for item in output_slots)),
            "max_file_bytes": max(int(item["max_file_bytes"]) for item in output_slots),
            "max_total_bytes": aggregate_output_bytes,
            "allowed_media_types": output_media,
        },
        "max_timeout_seconds": 3600,
    }


def _contract_sha256(contract: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_message(contract)).hexdigest()


def _output_mappings(contract: Mapping[str, object]) -> list[dict[str, object]]:
    output = contract.get("output")
    slots = output.get("slots") if isinstance(output, Mapping) else None
    if not isinstance(slots, list):
        raise ArtifactJobError("artifact output mappings are unavailable")
    mappings: list[dict[str, object]] = []
    for slot in slots:
        if not isinstance(slot, Mapping):
            raise ArtifactJobError("artifact output mappings are unavailable")
        media_types = slot.get("media_types")
        extensions = slot.get("extensions")
        if (
            not isinstance(slot.get("id"), str)
            or not isinstance(media_types, list)
            or len(media_types) != 1
            or not isinstance(media_types[0], str)
            or not isinstance(extensions, list)
            or not extensions
            or not all(isinstance(item, str) for item in extensions)
        ):
            raise ArtifactJobError("artifact output mappings are unavailable")
        mappings.append(
            {
                "slot": slot["id"],
                "media_type": media_types[0],
                "extensions": sorted(extensions),
            }
        )
    return sorted(mappings, key=lambda item: str(item["slot"]).encode("utf-8"))


def _effective_output_limits(
    contract: Mapping[str, object], supplied: Mapping[str, object]
) -> RecipeJobOutputLimits:
    try:
        requested = RecipeJobOutputLimits.parse(supplied)
        allowed = RecipeJobOutputLimits.parse(contract["output_limits"])
    except (AgentProtocolError, KeyError, TypeError) as error:
        raise ArtifactJobError("artifact output limits are invalid") from error
    if (
        requested.max_files > allowed.max_files
        or requested.max_file_bytes > allowed.max_file_bytes
        or requested.max_total_bytes > allowed.max_total_bytes
        or not set(requested.allowed_media_types) <= set(allowed.allowed_media_types)
    ):
        raise ArtifactJobError("artifact output limits exceed the recipe contract")
    output = contract.get("output")
    slots = output.get("slots") if isinstance(output, Mapping) else None
    required_slots = (
        [
            item
            for item in slots
            if isinstance(item, Mapping) and item.get("min_files", 0) > 0
        ]
        if isinstance(slots, list)
        else []
    )
    if requested.max_files < sum(
        int(item["min_files"]) for item in required_slots
    ) or any(
        not set(item["media_types"]) & set(requested.allowed_media_types)
        for item in required_slots
    ):
        raise ArtifactJobError(
            "artifact output limits cannot satisfy the recipe contract"
        )
    return requested


def _validate_inputs_against_contract(
    contract: Mapping[str, object], inputs: tuple[RecipeJobInputFile, ...]
) -> None:
    raw_input = contract.get("input")
    if not isinstance(raw_input, Mapping):
        raise ArtifactJobError("artifact input contract is invalid")
    raw_slots = raw_input.get("slots")
    if not isinstance(raw_slots, list):
        raise ArtifactJobError("artifact input contract is invalid")
    slots = {
        item["id"]: item
        for item in raw_slots
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    if not slots and inputs:
        raise ArtifactJobError("recipe does not accept artifact input files")
    for item in inputs:
        slot = slots.get(item.slot)
        if slot is None:
            raise ArtifactJobError(f"artifact input slot {item.slot} is undeclared")
        if item.media_type not in slot["media_types"]:
            raise ArtifactJobError(
                f"artifact input {item.name} media type is not allowed"
            )
        if item.size_bytes > slot["max_file_bytes"]:
            raise ArtifactJobError(f"artifact input {item.name} exceeds its slot limit")
        extensions = slot["extensions"]
        if extensions and not any(
            item.name.lower().endswith(ext) for ext in extensions
        ):
            raise ArtifactJobError(
                f"artifact input {item.name} extension is not allowed"
            )
    for slot_id, slot in slots.items():
        selected = [item for item in inputs if item.slot == slot_id]
        if not slot["min_files"] <= len(selected) <= slot["max_files"]:
            raise ArtifactJobError(
                f"artifact input slot {slot_id} file count is invalid"
            )
        if sum(item.size_bytes for item in selected) > slot["max_total_bytes"]:
            raise ArtifactJobError(
                f"artifact input slot {slot_id} bytes exceed the limit"
            )
    if sum(item.size_bytes for item in inputs) > raw_input["max_bytes"]:
        raise ArtifactJobError("artifact job input bytes exceed the recipe contract")


def _validate_outputs_against_contract(
    contract: Mapping[str, object], outputs: Sequence[RecipeJobFile], *, terminal: bool
) -> None:
    raw_output = contract.get("output")
    slots_value = raw_output.get("slots") if isinstance(raw_output, Mapping) else None
    if not isinstance(raw_output, Mapping) or not isinstance(slots_value, list):
        raise ArtifactJobError("artifact output contract is invalid")
    slots = [item for item in slots_value if isinstance(item, Mapping)]
    assignments: dict[str, list[RecipeJobFile]] = {
        str(slot["id"]): [] for slot in slots
    }
    for output in outputs:
        matches = [
            (len(extension.encode("utf-8")), slot)
            for slot in slots
            if output.media_type in slot["media_types"]
            for extension in slot["extensions"]
            if output.name.endswith(extension)
        ]
        if not matches:
            raise ArtifactJobError(f"artifact output {output.name} has no unique slot")
        longest = max(length for length, _slot in matches)
        longest_slots = {
            str(slot["id"]): slot for length, slot in matches if length == longest
        }
        if len(longest_slots) != 1:
            raise ArtifactJobError(f"artifact output {output.name} has no unique slot")
        slot = next(iter(longest_slots.values()))
        if output.size_bytes > slot["max_file_bytes"]:
            raise ArtifactJobError(
                f"artifact output {output.name} exceeds its slot limit"
            )
        assignments[str(slot["id"])].append(output)
    for slot in slots:
        selected = assignments[str(slot["id"])]
        minimum = int(slot["min_files"]) if terminal else 0
        if not minimum <= len(selected) <= int(slot["max_files"]):
            raise ArtifactJobError(
                f"artifact output slot {slot['id']} file count is invalid"
            )
        if sum(item.size_bytes for item in selected) > int(slot["max_total_bytes"]):
            raise ArtifactJobError(
                f"artifact output slot {slot['id']} bytes exceed the limit"
            )
    if sum(item.size_bytes for item in outputs) > int(raw_output["max_total_bytes"]):
        raise ArtifactJobError("artifact output bytes exceed the recipe contract")


def _effective_parameters(
    contract: Mapping[str, object], supplied: Mapping[str, object]
) -> dict[str, object]:
    definitions = contract.get("parameters")
    if not isinstance(definitions, list):
        raise ArtifactJobError("artifact parameter contract is invalid")
    by_name = {
        item["name"]: item
        for item in definitions
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }
    if set(supplied) - set(by_name):
        raise ArtifactJobError("artifact job contains undeclared parameters")
    effective: dict[str, object] = {}
    for name, definition in by_name.items():
        value = supplied.get(name, definition.get("default"))
        kind = definition.get("type")
        valid_type = (
            kind == "string"
            and isinstance(value, str)
            and "\x00" not in value
            and len(value.encode("utf-8")) <= 4096
            or kind == "integer"
            and isinstance(value, int)
            and not isinstance(value, bool)
            or kind == "float"
            and _finite_parameter_number(value)
            or kind == "boolean"
            and isinstance(value, bool)
            or kind == "enum"
            and value in definition.get("allowed_values", [])
        )
        if not valid_type:
            raise ArtifactJobError(f"artifact job parameter {name} has the wrong type")
        minimum = definition.get("minimum")
        maximum = definition.get("maximum")
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and (
                isinstance(minimum, (int, float))
                and value < minimum
                or isinstance(maximum, (int, float))
                and value > maximum
            )
        ):
            raise ArtifactJobError(
                f"artifact job parameter {name} is outside its range"
            )
        pattern = definition.get("pattern")
        if isinstance(pattern, str) and isinstance(value, str):
            try:
                matched = re.fullmatch(pattern, value) is not None
            except re.error as error:
                raise ArtifactJobError(
                    "artifact parameter pattern is invalid"
                ) from error
            if not matched:
                raise ArtifactJobError(f"artifact job parameter {name} does not match")
        effective[name] = value
    return effective


class ArtifactJobService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        recipe_operations: RecipeOperationService,
        blob_store: ArtifactBlobStore,
        clock: Callable[[], datetime],
        retention_seconds: int = 7 * 24 * 60 * 60,
    ) -> None:
        if not 3600 <= retention_seconds <= 365 * 24 * 60 * 60:
            raise ValueError("artifact job retention is invalid")
        self._sessions = sessions
        self._recipe_operations = recipe_operations
        self._blob_store = blob_store
        self._clock = clock
        self._retention_seconds = retention_seconds

    def reconcile_storage(self, *, batch_limit: int = 1000) -> dict[str, object]:
        if not 1 <= batch_limit <= 10_000:
            raise ValueError("artifact reconciliation batch limit is invalid")
        with self._blob_store.reference_reconciliation():
            return self._reconcile_storage_fenced(batch_limit=batch_limit)

    def _reconcile_storage_fenced(self, *, batch_limit: int) -> dict[str, object]:
        cutoff = self._clock() - timedelta(seconds=self._retention_seconds)
        with self._sessions.begin() as session:
            expired = tuple(
                session.scalars(
                    select(ArtifactJob)
                    .where(
                        ArtifactJob.state.in_({"succeeded", "failed", "cancelled"}),
                        ArtifactJob.completed_at.is_not(None),
                        ArtifactJob.completed_at < cutoff,
                    )
                    .limit(batch_limit)
                )
            )
            if expired:
                session.execute(
                    delete(ArtifactJobFile).where(
                        ArtifactJobFile.artifact_job_id.in_([job.id for job in expired])
                    )
                )
            for job in expired:
                session.delete(job)
            session.flush()
            referenced = set(session.scalars(select(ArtifactJobFile.blob_sha256)))
            orphan_rows = (
                tuple(
                    session.scalars(
                        select(ArtifactJobBlob)
                        .where(ArtifactJobBlob.sha256.not_in(referenced))
                        .limit(batch_limit)
                    )
                )
                if referenced
                else tuple(session.scalars(select(ArtifactJobBlob).limit(batch_limit)))
            )
            for blob in orphan_rows:
                session.delete(blob)
        result = self._blob_store.reconcile(
            referenced,
            batch_limit=batch_limit,
            _reference_fenced=True,
        )
        return {
            "expired_jobs": len(expired),
            "removed_blob_records": len(orphan_rows),
            **result,
            "remaining_work": bool(
                len(expired) == batch_limit
                or len(orphan_rows) == batch_limit
                or result.get("remaining_work") is True
            ),
        }

    def create(
        self,
        run_id: str,
        *,
        interface: str,
        parameters: Mapping[str, object],
        inputs: Sequence[Mapping[str, object]],
        output_limits: Mapping[str, object],
        timeout_seconds: int,
        actor: str,
        request_id: str,
    ) -> ArtifactJobView:
        parsed_inputs = tuple(
            sorted(
                (
                    RecipeJobInputFile.parse(item, maximum_bytes=MAX_INPUT_FILE_BYTES)
                    for item in inputs
                ),
                key=lambda item: item.name.encode("utf-8"),
            )
        )
        if len(parsed_inputs) > MAX_INPUT_FILES:
            raise ArtifactJobError("artifact job has too many input files")
        if len({item.name for item in parsed_inputs}) != len(parsed_inputs):
            raise ArtifactJobError("artifact input names must be unique")
        total = sum(item.size_bytes for item in parsed_inputs)
        if total > MAX_INPUT_TOTAL_BYTES:
            raise ArtifactJobError("artifact job input bytes exceed the limit")
        if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
            raise ArtifactJobError("artifact job timeout is invalid")
        if not 1 <= timeout_seconds <= 3_600:
            raise ArtifactJobError("artifact job timeout is invalid")
        supplied_parameters = _json_copy(parameters)
        if not isinstance(supplied_parameters, dict):
            raise ArtifactJobError("artifact job parameters must be an object")
        manifest = recipe_job_manifest_document(parsed_inputs)
        manifest_digest = recipe_job_manifest_sha256(parsed_inputs)
        now = self._clock()
        try:
            with self._sessions.begin() as session:
                return self._create_in_session(
                    session,
                    run_id=run_id,
                    interface=interface,
                    supplied_parameters=supplied_parameters,
                    parsed_inputs=parsed_inputs,
                    output_limits=output_limits,
                    timeout_seconds=timeout_seconds,
                    actor=actor,
                    request_id=request_id,
                    manifest=manifest,
                    manifest_digest=manifest_digest,
                    total=total,
                    now=now,
                )
        except IntegrityError:
            # A concurrent controller process may win the globally unique
            # request key after our initial lookup. Re-open a transaction and
            # apply the same semantic replay comparison to the committed row.
            with self._sessions.begin() as session:
                existing = session.scalar(
                    select(ArtifactJob).where(ArtifactJob.request_id == request_id)
                )
                if existing is None:
                    raise ArtifactJobError(
                        "artifact job request key collision"
                    ) from None
                return self._create_in_session(
                    session,
                    run_id=run_id,
                    interface=interface,
                    supplied_parameters=supplied_parameters,
                    parsed_inputs=parsed_inputs,
                    output_limits=output_limits,
                    timeout_seconds=timeout_seconds,
                    actor=actor,
                    request_id=request_id,
                    manifest=manifest,
                    manifest_digest=manifest_digest,
                    total=total,
                    now=now,
                    existing=existing,
                )

    def _create_in_session(
        self,
        session: Session,
        *,
        run_id: str,
        interface: str,
        supplied_parameters: Mapping[str, object],
        parsed_inputs: tuple[RecipeJobInputFile, ...],
        output_limits: Mapping[str, object],
        timeout_seconds: int,
        actor: str,
        request_id: str,
        manifest: dict[str, object],
        manifest_digest: str,
        total: int,
        now: datetime,
        existing: ArtifactJob | None = None,
    ) -> ArtifactJobView:
        existing = existing or session.scalar(
            select(ArtifactJob)
            .where(ArtifactJob.request_id == request_id)
            .with_for_update()
        )
        if existing is not None and (
            existing.run_id != run_id
            or existing.interface != interface
            or existing.input_manifest != manifest
            or existing.input_manifest_sha256 != manifest_digest
            or existing.input_total_bytes != total
            or existing.timeout_seconds != timeout_seconds
            or existing.actor != actor
        ):
            raise ArtifactJobError("request key was already used differently")
        run = session.get(RecipeRun, run_id)
        if run is None or existing is None and run.state != "running":
            raise ArtifactJobError("recipe run is not accepting jobs")
        installation = session.get(RecipeInstallation, run.installation_id)
        resolved = (
            _active_recipe_revision(session, installation.recipe_revision_id)
            if installation is not None
            else None
        )
        if resolved is None:
            raise ArtifactJobError("recipe revision is unavailable")
        _revision, recipe = resolved
        document = recipe.model_dump(mode="json")
        if _recipe_interface(document) != interface or interface == "openai":
            if existing is not None:
                raise ArtifactJobError("request key was already used differently")
            raise ArtifactJobError("artifact job interface does not match the run")
        try:
            contract = _compile_contract(document, interface)
            contract_digest = _contract_sha256(contract)
            parameters_copy = _effective_parameters(contract, supplied_parameters)
            limits = _effective_output_limits(contract, output_limits)
            max_timeout = contract.get("max_timeout_seconds")
            if not isinstance(max_timeout, int) or timeout_seconds > max_timeout:
                raise ArtifactJobError(
                    "artifact job timeout exceeds the recipe contract"
                )
            _validate_inputs_against_contract(contract, parsed_inputs)
        except ArtifactJobError:
            if existing is not None:
                raise ArtifactJobError(
                    "request key was already used differently"
                ) from None
            raise
        effective_limits = limits.to_mapping()
        if existing is not None:
            if (
                existing.parameters != parameters_copy
                or existing.output_limits != effective_limits
                or existing.compiled_contract != contract
                or existing.contract_sha256 != contract_digest
            ):
                raise ArtifactJobError("request key was already used differently")
            return self._view_in_session(session, existing)
        artifact_job = ArtifactJob(
            id=str(uuid.uuid4()),
            run_id=run_id,
            request_id=request_id,
            interface=interface,
            parameters=parameters_copy,
            output_limits=effective_limits,
            compiled_contract=contract,
            contract_sha256=contract_digest,
            state="draft",
            input_manifest=manifest,
            input_manifest_sha256=manifest_digest,
            input_total_bytes=total,
            timeout_seconds=timeout_seconds,
            actor=actor,
            created_at=now,
            updated_at=now,
        )
        session.add(artifact_job)
        session.flush()
        return self._view_in_session(session, artifact_job)

    def capabilities(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "transport": {
                "max_input_files": MAX_INPUT_FILES,
                "max_input_file_bytes": MAX_INPUT_FILE_BYTES,
                "max_input_total_bytes": MAX_INPUT_TOTAL_BYTES,
                "max_output_files": 32,
                "max_output_file_bytes": 1024**3,
                "max_output_total_bytes": 2 * 1024**3,
                "max_timeout_seconds": 3_600,
                "reserved_input_names": ["manifest.json"],
            },
            "storage": self._blob_store.usage(),
        }

    def put_input(
        self,
        job_id: str,
        *,
        name: str,
        media_type: str,
        expected_sha256: str,
        content: bytes,
    ) -> ArtifactJobView:
        with self._blob_store.reference_attachment():
            try:
                stored = self._blob_store.put_bytes(
                    expected_sha256, content, maximum_bytes=MAX_INPUT_FILE_BYTES
                )
            except ArtifactBlobStoreError as error:
                raise ArtifactJobError(str(error)) from error
            return self._attach_input(
                job_id, name=name, media_type=media_type, stored=stored
            )

    async def put_input_stream(
        self,
        job_id: str,
        *,
        name: str,
        media_type: str,
        expected_sha256: str,
        content_length: int,
        chunks: AsyncIterable[bytes],
    ) -> ArtifactJobView:
        expected_bytes = self.input_upload_size(
            job_id, name=name, media_type=media_type, expected_sha256=expected_sha256
        )
        if content_length != expected_bytes:
            raise ArtifactJobError("artifact input Content-Length does not match")
        with self._blob_store.reference_attachment():
            try:
                stored = await self._blob_store.put_stream(
                    expected_sha256,
                    chunks,
                    expected_bytes=expected_bytes,
                    maximum_bytes=MAX_INPUT_FILE_BYTES,
                )
            except ArtifactBlobStoreError as error:
                raise ArtifactJobError(str(error)) from error
            return self._attach_input(
                job_id, name=name, media_type=media_type, stored=stored
            )

    def input_upload_size(
        self, job_id: str, *, name: str, media_type: str, expected_sha256: str
    ) -> int:
        with self._sessions() as session:
            job = session.get(ArtifactJob, job_id)
            if job is None:
                raise KeyError(job_id)
            if job.state != "draft":
                raise ArtifactJobError("artifact job inputs are immutable")
            declaration = self._input_declaration(job, name)
            if (
                declaration is None
                or declaration.get("media_type") != media_type
                or declaration.get("sha256") != expected_sha256
                or not isinstance(declaration.get("size_bytes"), int)
            ):
                raise ArtifactJobError("artifact input does not match its declaration")
            return declaration["size_bytes"]

    def _attach_input(
        self,
        job_id: str,
        *,
        name: str,
        media_type: str,
        stored: StoredArtifactBlob,
    ) -> ArtifactJobView:
        now = self._clock()
        with self._sessions.begin() as session:
            job = session.get(ArtifactJob, job_id, with_for_update=True)
            if job is None:
                raise KeyError(job_id)
            if job.state != "draft":
                raise ArtifactJobError("artifact job inputs are immutable")
            declaration = self._input_declaration(job, name)
            if (
                declaration is None
                or declaration.get("media_type") != media_type
                or declaration.get("sha256") != stored.sha256
                or declaration.get("size_bytes") != stored.size_bytes
            ):
                raise ArtifactJobError("artifact input does not match its declaration")
            self._put_blob_in_session(session, stored, now)
            existing = self._file_in_session(session, job_id, "input", name)
            if existing is not None:
                if existing.blob_sha256 != stored.sha256:
                    raise ArtifactJobError("artifact input changed")
                return self._view_in_session(session, job)
            session.add(
                ArtifactJobFile(
                    artifact_job_id=job_id,
                    direction="input",
                    slot=str(declaration["slot"]),
                    name=name,
                    media_type=media_type,
                    size_bytes=stored.size_bytes,
                    blob_sha256=stored.sha256,
                    created_at=now,
                )
            )
            job.updated_at = now
            session.flush()
            return self._view_in_session(session, job)

    def finalize(self, job_id: str) -> ArtifactJobView:
        now = self._clock()
        with self._sessions.begin() as session:
            job = session.get(ArtifactJob, job_id, with_for_update=True)
            if job is None:
                raise KeyError(job_id)
            if job.state == "ready":
                return self._view_in_session(session, job)
            if job.state != "draft":
                raise ArtifactJobError("artifact job cannot be finalized")
            expected = job.input_manifest.get("files")
            uploaded = self._files_in_session(session, job_id, "input")
            observed = [self._file_mapping(item) for item in uploaded]
            if expected != observed:
                raise ArtifactJobError("artifact job inputs are incomplete")
            job.state = "ready"
            job.finalized_at = now
            job.updated_at = now
            return self._view_in_session(session, job)

    def submit(self, job_id: str, *, actor: str, request_id: str) -> ArtifactJobView:
        now = self._clock()
        with self._sessions.begin() as session:
            artifact_job = session.get(ArtifactJob, job_id, with_for_update=True)
            if artifact_job is None:
                raise KeyError(job_id)
            if artifact_job.operation_id is not None:
                return self._view_in_session(session, artifact_job)
            if artifact_job.state != "ready":
                raise ArtifactJobError("artifact job is not ready")
            run = session.get(RecipeRun, artifact_job.run_id, with_for_update=True)
            if run is None or run.state != "running":
                raise ArtifactJobError("recipe run is not accepting jobs")
            concurrent = session.scalar(
                select(ArtifactJob.id)
                .where(
                    ArtifactJob.run_id == run.id,
                    ArtifactJob.id != artifact_job.id,
                    ArtifactJob.state.in_(
                        {"queued", "running", "cancelling", "waiting-for-operator"}
                    ),
                )
                .limit(1)
            )
            if concurrent is not None:
                raise ArtifactJobError(
                    "another artifact job already owns this run reservation"
                )
            installation = session.get(RecipeInstallation, run.installation_id)
            resolved = (
                _active_recipe_revision(session, installation.recipe_revision_id)
                if installation is not None
                else None
            )
            if (
                installation is None
                or resolved is None
            ):
                raise ArtifactJobError("recipe job workload identity is unavailable")
            revision, _recipe = resolved
            node = self._job_node_in_session(session, run)
            raw_files = artifact_job.input_manifest["files"]
            payload = {
                "schema_version": 1,
                "job_id": artifact_job.id,
                "run_id": run.id,
                "installation_id": installation.id,
                "recipe_revision_id": revision.id,
                "recipe_content_sha256": revision.content_digest,
                "image_digest": installation.image_digest,
                "plan_digest": run.plan_digest,
                "interface": artifact_job.interface,
                "rank": node.rank,
                "role": node.role,
                "reserved_memory_bytes": node.reserved_memory_bytes,
                "contract_sha256": artifact_job.contract_sha256,
                "input_manifest_sha256": artifact_job.input_manifest_sha256,
                "input_total_bytes": artifact_job.input_total_bytes,
                "inputs": raw_files,
                "parameters": artifact_job.parameters,
                "output_mappings": _output_mappings(artifact_job.compiled_contract),
                "output_limits": artifact_job.output_limits,
                "timeout_seconds": artifact_job.timeout_seconds,
            }
            RecipeJobRunRequest.parse(payload)
            operation = self._recipe_operations.enqueue_one_shot_job_in_session(
                session,
                artifact_job_id=artifact_job.id,
                run_id=run.id,
                node_id=node.node_id,
                payload=payload,
                actor=actor,
                request_id=request_id,
                authority_digest=revision.content_digest,
                now=now,
            )
            artifact_job.operation_id = operation.id
            artifact_job.state = "queued"
            artifact_job.submitted_at = now
            artifact_job.updated_at = now
        self._recipe_operations.notify_agents()
        return self.get(job_id)

    def get(self, job_id: str) -> ArtifactJobView:
        with self._sessions() as session:
            job = session.get(ArtifactJob, job_id)
            if job is None:
                raise KeyError(job_id)
            return self._view_in_session(session, job)

    def list_for_run(
        self, run_id: str, *, limit: int = 100
    ) -> tuple[ArtifactJobView, ...]:
        if not 1 <= limit <= 100:
            raise ArtifactJobError("artifact job list limit is invalid")
        with self._sessions() as session:
            if session.get(RecipeRun, run_id) is None:
                raise KeyError(run_id)
            jobs = tuple(
                session.scalars(
                    select(ArtifactJob)
                    .where(ArtifactJob.run_id == run_id)
                    .order_by(ArtifactJob.created_at.desc(), ArtifactJob.id.desc())
                    .limit(limit)
                )
            )
            return tuple(self._view_in_session(session, job) for job in jobs)

    def cancel(
        self, job_id: str, *, actor: str, request_id: str, reason: str
    ) -> ArtifactJobView:
        cancellation_reason = " ".join(reason.split())[:512]
        with self._sessions() as session:
            job = session.get(ArtifactJob, job_id)
            if job is None:
                raise KeyError(job_id)
            operation_id = job.operation_id
            state = job.state
            evidence = (
                job.result_evidence if isinstance(job.result_evidence, Mapping) else {}
            )
        if state in {"succeeded", "failed"}:
            raise ArtifactJobError("artifact job is not cancellable")
        if state == "cancelled" and operation_id is None:
            if (
                evidence.get("cancel_request_id") == request_id
                and evidence.get("cancel_actor") == actor
                and evidence.get("cancel_reason") == cancellation_reason
            ):
                return self.get(job_id)
            raise ArtifactJobError(
                "cancellation request key was already used differently"
            )
        if operation_id is not None:
            try:
                self._recipe_operations.cancel(
                    operation_id, actor=actor, request_id=request_id, reason=reason
                )
            except RecipeOperationConflict as error:
                raise ArtifactJobError(str(error)) from error
        now = self._clock()
        with self._sessions.begin() as session:
            job = session.get(ArtifactJob, job_id, with_for_update=True)
            assert job is not None
            parent = (
                session.get(Job, operation_id) if operation_id is not None else None
            )
            cancel_pending = bool(
                parent is not None
                and parent.state == "running"
                and isinstance(parent.result, Mapping)
                and parent.result.get("cancel_requested") is True
            )
            if job.state not in {"succeeded", "failed", "cancelled"}:
                job.state = "cancelling" if cancel_pending else "cancelled"
                job.status_reason = cancellation_reason
                job.result_evidence = {
                    **(
                        dict(job.result_evidence)
                        if isinstance(job.result_evidence, Mapping)
                        else {}
                    ),
                    "cancel_request_id": request_id,
                    "cancel_actor": actor,
                    "cancel_reason": cancellation_reason,
                }
                job.completed_at = None if cancel_pending else now
                job.updated_at = now
            return self._view_in_session(session, job)

    def input_blob(
        self, job_id: str, sha256: str, *, node_id: str
    ) -> tuple[Path, str, int]:
        with self._sessions() as session:
            job = self._authorized_agent_job(session, job_id, node_id)
            row = session.scalar(
                select(ArtifactJobFile).where(
                    ArtifactJobFile.artifact_job_id == job.id,
                    ArtifactJobFile.direction == "input",
                    ArtifactJobFile.blob_sha256 == sha256,
                )
            )
            if row is None:
                raise KeyError(sha256)
            blob = session.get(ArtifactJobBlob, sha256)
            if blob is None:
                raise ArtifactJobError("stored artifact input is inconsistent")
            try:
                path = self._blob_store.resolve(
                    blob.storage_key, sha256, blob.size_bytes
                )
            except ArtifactBlobStoreError as error:
                raise ArtifactJobError(str(error)) from error
            return path, row.media_type, row.size_bytes

    def put_output(
        self,
        job_id: str,
        *,
        node_id: str,
        name: str,
        media_type: str,
        expected_sha256: str,
        content: bytes,
    ) -> None:
        parsed = RecipeJobFile.parse(
            {
                "name": name,
                "media_type": media_type,
                "size_bytes": len(content),
                "sha256": expected_sha256,
            },
            maximum_bytes=1024**3,
        )
        with self._blob_store.reference_attachment():
            try:
                stored = self._blob_store.put_bytes(
                    expected_sha256, content, maximum_bytes=1024**3
                )
            except ArtifactBlobStoreError as error:
                raise ArtifactJobError(str(error)) from error
            self._attach_output(job_id, node_id=node_id, parsed=parsed, stored=stored)

    async def put_output_stream(
        self,
        job_id: str,
        *,
        node_id: str,
        name: str,
        media_type: str,
        expected_sha256: str,
        content_length: int,
        chunks: AsyncIterable[bytes],
    ) -> None:
        parsed = RecipeJobFile.parse(
            {
                "name": name,
                "media_type": media_type,
                "size_bytes": content_length,
                "sha256": expected_sha256,
            },
            maximum_bytes=1024**3,
        )
        self._validate_output_upload(job_id, node_id=node_id, parsed=parsed)
        with self._blob_store.reference_attachment():
            try:
                stored = await self._blob_store.put_stream(
                    expected_sha256,
                    chunks,
                    expected_bytes=content_length,
                    maximum_bytes=1024**3,
                )
            except ArtifactBlobStoreError as error:
                raise ArtifactJobError(str(error)) from error
            self._attach_output(job_id, node_id=node_id, parsed=parsed, stored=stored)

    def _validate_output_upload(
        self, job_id: str, *, node_id: str, parsed: RecipeJobFile
    ) -> None:
        with self._sessions() as session:
            job = self._authorized_agent_job(session, job_id, node_id)
            limits = RecipeJobOutputLimits.parse(job.output_limits)
            existing = self._files_in_session(session, job_id, "output")
            projected = tuple(
                RecipeJobFile.parse(self._file_mapping(item), maximum_bytes=1024**3)
                for item in existing
                if item.name != parsed.name
            ) + (parsed,)
            _validate_outputs_against_contract(
                job.compiled_contract, projected, terminal=False
            )
            if parsed.media_type not in limits.allowed_media_types:
                raise ArtifactJobError("artifact output media type is not allowed")
            if (
                len(existing)
                + (0 if any(item.name == parsed.name for item in existing) else 1)
                > limits.max_files
            ):
                raise ArtifactJobError("artifact output file count exceeds the limit")
            if (
                parsed.size_bytes > limits.max_file_bytes
                or sum(item.size_bytes for item in existing if item.name != parsed.name)
                + parsed.size_bytes
                > limits.max_total_bytes
            ):
                raise ArtifactJobError("artifact output bytes exceed the limit")

    def _attach_output(
        self,
        job_id: str,
        *,
        node_id: str,
        parsed: RecipeJobFile,
        stored: StoredArtifactBlob,
    ) -> None:
        if stored.sha256 != parsed.sha256 or stored.size_bytes != parsed.size_bytes:
            raise ArtifactJobError("stored artifact output does not match declaration")
        now = self._clock()
        with self._sessions.begin() as session:
            job = self._authorized_agent_job(session, job_id, node_id, lock=True)
            limits = RecipeJobOutputLimits.parse(job.output_limits)
            if parsed.media_type not in limits.allowed_media_types:
                raise ArtifactJobError("artifact output media type is not allowed")
            existing = self._files_in_session(session, job_id, "output")
            same_name = next(
                (item for item in existing if item.name == parsed.name), None
            )
            if same_name is not None:
                if same_name.blob_sha256 != parsed.sha256:
                    raise ArtifactJobError("artifact output changed")
                return
            projected = tuple(
                RecipeJobFile.parse(self._file_mapping(item), maximum_bytes=1024**3)
                for item in existing
            ) + (parsed,)
            _validate_outputs_against_contract(
                job.compiled_contract, projected, terminal=False
            )
            if len(existing) + 1 > limits.max_files:
                raise ArtifactJobError("artifact output file count exceeds the limit")
            if (
                parsed.size_bytes > limits.max_file_bytes
                or sum(item.size_bytes for item in existing) + parsed.size_bytes
                > limits.max_total_bytes
            ):
                raise ArtifactJobError("artifact output bytes exceed the limit")
            self._put_blob_in_session(session, stored, now)
            session.add(
                ArtifactJobFile(
                    artifact_job_id=job_id,
                    direction="output",
                    slot=None,
                    name=parsed.name,
                    media_type=parsed.media_type,
                    size_bytes=stored.size_bytes,
                    blob_sha256=stored.sha256,
                    created_at=now,
                )
            )
            job.updated_at = now

    def result_metadata(self, job_id: str) -> ArtifactJobView:
        view = self.get(job_id)
        if view.state != "succeeded":
            raise ArtifactJobError("artifact job result is not available")
        return view

    def result_blob(self, job_id: str, sha256: str) -> tuple[Path, str, str, int]:
        with self._sessions() as session:
            job = session.get(ArtifactJob, job_id)
            if job is None:
                raise KeyError(job_id)
            if job.state != "succeeded":
                raise ArtifactJobError("artifact job result is not available")
            row = session.scalar(
                select(ArtifactJobFile).where(
                    ArtifactJobFile.artifact_job_id == job_id,
                    ArtifactJobFile.direction == "output",
                    ArtifactJobFile.blob_sha256 == sha256,
                )
            )
            blob = session.get(ArtifactJobBlob, sha256) if row is not None else None
            if row is None or blob is None:
                raise KeyError(sha256)
            try:
                path = self._blob_store.resolve(
                    blob.storage_key, sha256, blob.size_bytes
                )
            except ArtifactBlobStoreError as error:
                raise ArtifactJobError(str(error)) from error
            return path, row.media_type, row.name, row.size_bytes

    def consume_agent_result(
        self,
        session: Session,
        operation: AgentOperation,
        _attempt: object,
        message: object,
    ) -> None:
        parent = session.get(Job, operation.parent_job_id)
        if parent is None or parent.kind != "recipe.job.run.v1":
            return
        artifact_job = session.scalar(
            select(ArtifactJob)
            .where(ArtifactJob.operation_id == parent.id)
            .with_for_update(of=ArtifactJob)
        )
        if artifact_job is None:
            raise ArtifactJobError("artifact job authority is unavailable")
        state = getattr(message, "state", None)
        raw_result = getattr(message, "result", None)
        now = self._clock()
        if state == "waiting-for-operator":
            try:
                waiting_result = RecipeJobRunResult.parse(raw_result)
                if (
                    waiting_result.job_id != artifact_job.id
                    or waiting_result.run_id != artifact_job.run_id
                    or waiting_result.exit_code != 130
                    or waiting_result.outputs
                ):
                    raise AgentProtocolError(
                        "waiting artifact result identity or output is invalid"
                    )
            except (AgentProtocolError, TypeError, ValueError) as error:
                operation.state = "failed"
                artifact_job.state = "failed"
                artifact_job.status_reason = str(error)[:512]
                artifact_job.completed_at = now
                artifact_job.updated_at = now
                return
            artifact_job.state = "waiting-for-operator"
            artifact_job.status_reason = (
                waiting_result.reason
                if waiting_result.reason
                else "artifact cancellation could not safely stop the active scope"
            )[:512]
            artifact_job.result_evidence = {
                "failure_kind": "cancellation-stop-uncertain",
                "recoverable": True,
                "active_scope_may_remain": True,
                "elapsed_milliseconds": waiting_result.elapsed_milliseconds,
                "peak_memory_bytes": waiting_result.peak_memory_bytes,
            }
            artifact_job.updated_at = now
            return
        try:
            result = RecipeJobRunResult.parse(raw_result)
            if result.job_id != artifact_job.id or result.run_id != artifact_job.run_id:
                raise AgentProtocolError("artifact result identity does not match")
            uploaded = self._files_in_session(session, artifact_job.id, "output")
            observed = tuple(
                RecipeJobFile.parse(self._file_mapping(item), maximum_bytes=1024**3)
                for item in uploaded
            )
            limits = RecipeJobOutputLimits.parse(artifact_job.output_limits)
            if tuple(item.to_mapping() for item in result.outputs) != tuple(
                item.to_mapping() for item in observed
            ):
                raise AgentProtocolError(
                    "artifact result does not match uploaded outputs"
                )
            if any(
                item.media_type not in limits.allowed_media_types
                for item in result.outputs
            ):
                raise AgentProtocolError("artifact result media type is not allowed")
            if (
                len(result.outputs) > limits.max_files
                or sum(item.size_bytes for item in result.outputs)
                > limits.max_total_bytes
            ):
                raise AgentProtocolError("artifact result exceeds output limits")
            succeeded = (
                state == "succeeded" and result.exit_code == 0 and bool(result.outputs)
            )
            failed = state == "failed" and result.exit_code != 0
            cancelled = bool(
                state == "cancelled"
                and result.exit_code == 130
                and not result.outputs
                and isinstance(parent.result, Mapping)
                and parent.result.get("cancel_requested") is True
            )
            if cancelled and uploaded:
                raise AgentProtocolError(
                    "cancelled artifact result cannot retain uploaded outputs"
                )
            if succeeded:
                _validate_outputs_against_contract(
                    artifact_job.compiled_contract, result.outputs, terminal=True
                )
            if not (succeeded or failed or cancelled):
                raise AgentProtocolError("artifact result state and exit code disagree")
        except (AgentProtocolError, TypeError, ValueError) as error:
            operation.state = "failed"
            artifact_job.state = "failed"
            artifact_job.status_reason = str(error)[:512]
            artifact_job.completed_at = now
            artifact_job.updated_at = now
            return
        artifact_job.output_manifest_sha256 = result.output_manifest_sha256
        artifact_job.output_manifest = {
            **recipe_job_manifest_document(result.outputs),
            "manifest_sha256": result.output_manifest_sha256,
        }
        artifact_job.result_evidence = {
            "elapsed_milliseconds": result.elapsed_milliseconds,
            "peak_memory_bytes": result.peak_memory_bytes,
        }
        artifact_job.state = (
            "succeeded" if succeeded else "cancelled" if cancelled else "failed"
        )
        artifact_job.status_reason = (
            None
            if succeeded
            else result.reason
            or ("artifact job cancelled" if cancelled else "recipe job failed")
        )
        artifact_job.completed_at = now
        artifact_job.updated_at = now

    def _authorized_agent_job(
        self, session: Session, job_id: str, node_id: str, *, lock: bool = False
    ) -> ArtifactJob:
        statement = select(ArtifactJob).where(ArtifactJob.id == job_id)
        if lock:
            statement = statement.with_for_update(of=ArtifactJob)
        job = session.scalar(statement)
        if job is None or job.operation_id is None:
            raise KeyError(job_id)
        parent = session.get(Job, job.operation_id)
        if parent is None or node_id not in parent.targets:
            raise ArtifactJobError("agent is not authorized for this artifact job")
        if job.state not in {"queued", "running"}:
            raise ArtifactJobError("artifact job transfer is closed")
        return job

    @staticmethod
    def _job_node_in_session(session: Session, run: RecipeRun) -> RunNode:
        nodes = tuple(
            session.scalars(
                select(RunNode).where(RunNode.run_id == run.id).order_by(RunNode.rank)
            )
        )
        plan_nodes = run.plan.get("nodes") if isinstance(run.plan, Mapping) else None
        endpoint_ids = (
            {
                item.get("node_id")
                for item in plan_nodes
                if isinstance(item, Mapping) and item.get("endpoint_owner") is True
            }
            if isinstance(plan_nodes, list)
            else set()
        )
        candidates = [node for node in nodes if node.node_id in endpoint_ids]
        if not candidates and len(nodes) == 1:
            candidates = list(nodes)
        if len(candidates) != 1 or candidates[0].state != "running":
            raise ArtifactJobError("artifact job endpoint owner is not running")
        return candidates[0]

    @staticmethod
    def _put_blob_in_session(
        session: Session, stored: StoredArtifactBlob, now: datetime
    ) -> None:
        blob = session.get(ArtifactJobBlob, stored.sha256)
        if blob is not None:
            if (
                blob.size_bytes != stored.size_bytes
                or blob.storage_key != stored.storage_key
            ):
                raise ArtifactJobError("content-addressed artifact collision")
            return
        session.add(
            ArtifactJobBlob(
                sha256=stored.sha256,
                size_bytes=stored.size_bytes,
                storage_key=stored.storage_key,
                created_at=now,
            )
        )

    @staticmethod
    def _file_in_session(
        session: Session, job_id: str, direction: str, name: str
    ) -> ArtifactJobFile | None:
        return session.scalar(
            select(ArtifactJobFile).where(
                ArtifactJobFile.artifact_job_id == job_id,
                ArtifactJobFile.direction == direction,
                ArtifactJobFile.name == name,
            )
        )

    @staticmethod
    def _input_declaration(job: ArtifactJob, name: str) -> Mapping[str, object] | None:
        declarations = job.input_manifest.get("files")
        if not isinstance(declarations, list):
            return None
        return next(
            (
                item
                for item in declarations
                if isinstance(item, Mapping) and item.get("name") == name
            ),
            None,
        )

    @staticmethod
    def _files_in_session(
        session: Session, job_id: str, direction: str
    ) -> tuple[ArtifactJobFile, ...]:
        return tuple(
            session.scalars(
                select(ArtifactJobFile)
                .where(
                    ArtifactJobFile.artifact_job_id == job_id,
                    ArtifactJobFile.direction == direction,
                )
                .order_by(ArtifactJobFile.name)
            )
        )

    @staticmethod
    def _file_mapping(item: ArtifactJobFile) -> dict[str, object]:
        value: dict[str, object] = {
            "name": item.name,
            "media_type": item.media_type,
            "size_bytes": item.size_bytes,
            "sha256": item.blob_sha256,
        }
        if item.direction == "input":
            if item.slot is None:
                raise ArtifactJobError("artifact input slot is unavailable")
            value = {"slot": item.slot, **value}
        return value

    def _view_in_session(self, session: Session, job: ArtifactJob) -> ArtifactJobView:
        state = job.state
        if state == "queued" and job.operation_id is not None:
            operation_state = session.scalar(
                select(AgentOperation.state).where(
                    AgentOperation.parent_job_id == job.operation_id
                )
            )
            if operation_state == "running":
                state = "running"
        inputs = tuple(
            self._file_mapping(item)
            for item in self._files_in_session(session, job.id, "input")
        )
        outputs = tuple(
            self._file_mapping(item)
            for item in self._files_in_session(session, job.id, "output")
        )
        return ArtifactJobView(
            id=job.id,
            run_id=job.run_id,
            operation_id=job.operation_id,
            interface=job.interface,
            state=state,
            contract_sha256=job.contract_sha256,
            compiled_contract=dict(job.compiled_contract),
            input_manifest_sha256=job.input_manifest_sha256,
            input_total_bytes=job.input_total_bytes,
            input_declarations=tuple(
                dict(item)
                for item in job.input_manifest.get("files", [])
                if isinstance(item, Mapping)
            ),
            input_files=inputs,
            output_limits=dict(job.output_limits),
            output_manifest_sha256=job.output_manifest_sha256,
            output_files=outputs,
            result_evidence=dict(job.result_evidence) if job.result_evidence else None,
            status_reason=job.status_reason,
            timeout_seconds=job.timeout_seconds,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )


__all__ = [
    "MAX_INPUT_FILE_BYTES",
    "ArtifactJobError",
    "ArtifactJobService",
    "ArtifactJobView",
]
