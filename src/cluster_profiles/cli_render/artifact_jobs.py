"""Terminal presentation for artifact jobs responses."""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from enum import StrEnum

from ..cli_states import ARTIFACT_JOB_IN_FLIGHT, lifecycle_state
from ..cli_states_generated import ENDPOINT_UNAVAILABLE, SUCCEEDED
from ..control_client import ControlMalformedResponse
from ..generated_control.models.artifact_job_contract_response import (
    ArtifactJobContractResponse,
)
from ..generated_control.models.artifact_slot_contract import ArtifactSlotContract
from ..generated_control.types import Unset
from .common import _bytes, _field, _records, _text, _time


class ResultFiles(StrEnum):
    FILES = "files"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"


def result_files(state: object, output_files: Sequence[object]) -> ResultFiles:
    """A succeeded job's files, or its empty result; any other state has none yet."""
    if state != SUCCEEDED:
        return ResultFiles.UNAVAILABLE
    return ResultFiles.FILES if output_files else ResultFiles.EMPTY


def job_reconnects(state: object, operation_id: object) -> bool:
    """A job can be followed while its operation is in flight."""
    return isinstance(operation_id, str) and state in ARTIFACT_JOB_IN_FLIGHT


def _artifact_job_record(payload: Mapping[str, object], action: str) -> None:
    _field("Artifact job", payload.get("id"))
    _field("Command", action)
    _field("Run", payload.get("run_id"))
    _field("State", lifecycle_state(dict(payload)))
    if payload.get("cancel_requested_at") is not None:
        _field("Cancel requested", _time(payload.get("cancel_requested_at")))
    _field("Interface", payload.get("interface"))
    _field("Contract SHA-256", payload.get("contract_sha256"))
    _field("Input manifest SHA-256", payload.get("input_manifest_sha256"))
    _field("Input bytes", _bytes(payload.get("input_total_bytes")))
    _field("Created", _time(payload.get("created_at")))
    _field("Updated", _time(payload.get("updated_at")))
    if payload.get("status_reason") is not None:
        _field("Reason", payload.get("status_reason"))
    evidence = payload.get("result_evidence")
    if isinstance(evidence, Mapping) and type(evidence.get("exit_code")) is int:
        _field("Exit code", evidence["exit_code"])

    operation_id = payload.get("operation_id")
    if operation_id is not None:
        _field("Operation", operation_id)
        _field("Submit request", payload.get("submit_request_id"))

    input_declarations = _records(payload, "input_declarations")
    input_files = _records(payload, "input_files")
    uploaded_names = {item.get("name") for item in input_files}
    _field("Inputs", f"{len(input_declarations)} declared, {len(input_files)} uploaded")
    for item in input_declarations:
        name = item.get("name")
        _field("Input", name)
        _field("Input slot", item.get("slot"))
        _field("Input state", "uploaded" if name in uploaded_names else "not uploaded")
        _field("Input media type", item.get("media_type"))
        _field("Input size", _bytes(item.get("size_bytes")))
        _field("Input SHA-256", item.get("sha256"))

    state = payload.get("state")
    output_files = _records(payload, "output_files")
    files = result_files(state, output_files)
    if files is not ResultFiles.UNAVAILABLE:
        _field("Output manifest SHA-256", payload.get("output_manifest_sha256"))
        if files is ResultFiles.EMPTY:
            print("Result files: none (job succeeded with an empty result).")
        else:
            _field("Result files", len(output_files))
            for item in output_files:
                _field("Output file", item.get("name"))
                _field("Output media type", item.get("media_type"))
                _field("Output size", _bytes(item.get("size_bytes")))
                _field("Output SHA-256", item.get("sha256"))
    else:
        _field(
            "Result files", f"unavailable until job succeeds (state: {_text(state)})"
        )

    if job_reconnects(state, operation_id):
        _field(
            "Reconnect",
            f"vonkctl job detail {shlex.quote(str(payload.get('id')))} --follow",
        )


def _artifact_job_list(payload: Mapping[str, object]) -> None:
    jobs = _records(payload, "jobs")
    _field("Artifact jobs", len(jobs))
    if not jobs:
        print("No artifact jobs for this run.")
        return
    for job in jobs:
        print()
        _field("Artifact job", job.get("id"))
        _field("Run", job.get("run_id"))
        _field("State", lifecycle_state(dict(job)))
        _field("Interface", job.get("interface"))
        if job.get("status_reason") is not None:
            _field("Reason", job.get("status_reason"))
        inputs = _records(job, "input_declarations")
        uploaded_inputs = _records(job, "input_files")
        _field("Inputs", f"{len(inputs)} declared, {len(uploaded_inputs)} uploaded")
        outputs = _records(job, "output_files")
        files = result_files(job.get("state"), outputs)
        if files is not ResultFiles.UNAVAILABLE:
            _field(
                "Outputs",
                "none (successful empty result)"
                if files is ResultFiles.EMPTY
                else f"{len(outputs)} verified result files",
            )
        else:
            _field(
                "Outputs",
                f"unavailable until job succeeds (state: {_text(job.get('state'))})",
            )
        operation_id = job.get("operation_id")
        if operation_id is not None:
            _field("Operation", operation_id)
        if job_reconnects(job.get("state"), operation_id):
            _field(
                "Reconnect",
                f"vonkctl job detail {shlex.quote(str(job.get('id')))} --follow",
            )


def _artifact_job_download(payload: Mapping[str, object]) -> None:
    _field("Artifact job", payload.get("job_id"))
    state = payload.get("state")
    _field("State", state)
    _field("Output manifest SHA-256", payload.get("output_manifest_sha256"))
    _field("Total output bytes", _bytes(payload.get("total_bytes")))
    files = _records(payload, "files")
    outcome = result_files(state, files)
    if outcome is ResultFiles.UNAVAILABLE:
        _field("Output files", ENDPOINT_UNAVAILABLE)
        return
    if outcome is ResultFiles.EMPTY:
        print("Result files: none (job succeeded with an empty result).")
        return
    _field("Verified output files", len(files))
    for item in files:
        _field("Output file", item.get("name"))
        _field("File state", item.get("state"))
        _field("Verified path", item.get("path"))
        _field("Verified size", _bytes(item.get("size_bytes")))
        _field("Verified SHA-256", item.get("sha256"))


def _contract_slots(label: str, slots: list[ArtifactSlotContract]) -> None:
    for slot in slots:
        _field(label, slot.id)
        _field(f"{label} label", slot.label)
        _field(f"{label} media types", ", ".join(slot.media_types))
        _field(f"{label} extensions", ", ".join(slot.extensions))
        _field(f"{label} files", f"{slot.min_files} to {slot.max_files}")
        _field(f"{label} max file size", _bytes(slot.max_file_bytes))
        _field(f"{label} max total size", _bytes(slot.max_total_bytes))


def _artifact_job_contract(answer: ArtifactJobContractResponse) -> None:
    contract = answer.contract
    if contract is None or isinstance(contract, Unset):
        raise ControlMalformedResponse("job contract observation is unavailable")
    _field("Recipe", _set(answer.recipe))
    _field("Recipe revision", _set(answer.recipe_revision_id))
    run_id = _set(answer.run_id)
    if run_id is not None:
        _field("Run", run_id)
        _field("Alias", _set(answer.alias))
    _field("Interface", contract.interface)
    _field("Contract SHA-256", _set(answer.contract_sha256))
    _field("Max job timeout", f"{contract.max_timeout_seconds} seconds")
    _field("Input required", "yes" if contract.input_.required else "no")
    _field("Input max bytes", _bytes(contract.input_.max_bytes))
    _contract_slots("Input slot", contract.input_.slots)
    _field("Output max bytes", _bytes(contract.output.max_total_bytes))
    _contract_slots("Output slot", contract.output.slots)
    for knob in contract.parameters:
        _field("Knob", knob.name)
        _field("Knob type", knob.type_)
        _field("Knob default", knob.default)
    _field(
        "Next",
        f"vonkctl job create --run {shlex.quote(run_id or '<run>')} "
        "--file <binding.json>",
    )


def _set[T](value: T | Unset) -> T | None:
    """A generated optional field, absent as ``None``."""
    return None if isinstance(value, Unset) else value


def _artifact_job(payload: Mapping[str, object], action: str) -> None:
    if action == "contract":
        _artifact_job_contract(ArtifactJobContractResponse.from_dict(dict(payload)))
    elif action == "list":
        _artifact_job_list(payload)
    elif action == "download" or (action == "run" and "job_id" in payload):
        _artifact_job_download(payload)
    elif action in {"detail", "create", "upload", "submit", "cancel", "run"}:
        _artifact_job_record(payload, action)
    else:
        _field("Observation", ENDPOINT_UNAVAILABLE)
