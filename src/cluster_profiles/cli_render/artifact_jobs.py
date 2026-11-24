"""Terminal presentation for artifact jobs responses."""

from __future__ import annotations

import shlex
from collections.abc import Container, Mapping, Sequence
from enum import StrEnum

from cluster_profiles.cli_json_contract import Word
from cluster_profiles.cli_render.common import _bytes, _field, _records, _text, _time
from cluster_profiles.cli_states import ARTIFACT_JOB_IN_FLIGHT, lifecycle_state
from cluster_profiles.cli_states_generated import ENDPOINT_UNAVAILABLE, SUCCEEDED
from cluster_profiles.control_client.errors import ControlMalformedResponse
from cluster_profiles.generated_control.models.artifact_job_contract_response import (
    ArtifactJobContractResponse,
)
from cluster_profiles.generated_control.models.artifact_job_response import (
    ArtifactJobResponse,
)
from cluster_profiles.generated_control.models.artifact_slot_contract import (
    ArtifactSlotContract,
)
from cluster_profiles.generated_control.models.boolean_parameter import BooleanParameter
from cluster_profiles.generated_control.models.enum_parameter import EnumParameter
from cluster_profiles.generated_control.models.float_parameter import FloatParameter
from cluster_profiles.generated_control.models.integer_parameter import IntegerParameter
from cluster_profiles.generated_control.models.string_parameter import StringParameter
from cluster_profiles.generated_control.types import Unset

type Knob = (
    StringParameter
    | IntegerParameter
    | FloatParameter
    | BooleanParameter
    | EnumParameter
)


#: The job actions that print one job's record.
RECORD_ACTIONS = frozenset(
    {Word.DETAIL, Word.CREATE, Word.UPLOAD, Word.SUBMIT, Word.CANCEL, Word.RUN}
)


def prints_record(action: str, fields: Container[str]) -> bool:
    """Whether this job action prints one job's record (not a download receipt)."""
    return action in RECORD_ACTIONS and not (action == Word.RUN and "job_id" in fields)


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


def _artifact_job_record(job: ArtifactJobResponse) -> None:
    """The short record: at most eight lines; hashes and per-file rows are --wide."""
    document = job.to_dict()
    _field("Artifact job", job.id)
    _field("Run", job.run_id)
    state_line = str(lifecycle_state(document))
    if declared(job.cancel_requested_at) is not None:
        state_line += (
            f" (cancel requested {_time(document.get('cancel_requested_at'))})"
        )
    _field("State", state_line)
    if declared(job.status_reason) is not None:
        _field("Reason", job.status_reason)
    declarations = job.input_declarations or []
    _field("Inputs", f"{len(declarations)} declared, {len(job.input_files)} uploaded")
    files = result_files(job.state, job.output_files)
    if files is ResultFiles.EMPTY:
        _field("Result files", "none (job succeeded with an empty result)")
    elif files is ResultFiles.FILES:
        _field("Result files", len(job.output_files))
    else:
        _field("Result files", f"unavailable until job succeeds ({_text(job.state)})")
    _field(
        "Created",
        f"{_time(document.get('created_at'))} "
        f"(updated {_time(document.get('updated_at'))})",
    )
    operation_id = declared(job.operation_id)
    if job_reconnects(job.state, operation_id):
        _field("Reconnect", f"vonkctl job detail {shlex.quote(job.id)} --follow")


def artifact_job_details(job: ArtifactJobResponse) -> None:
    """What `--wide` adds to the short record: hashes, ids and per-file rows."""
    _field("Interface", job.interface)
    _field("Contract SHA-256", job.contract_sha256)
    _field("Input manifest SHA-256", job.input_manifest_sha256)
    _field("Input bytes", _bytes(job.input_total_bytes))
    evidence = declared(job.result_evidence)
    if evidence is not None and declared(evidence.exit_code) is not None:
        _field("Exit code", evidence.exit_code)
    if declared(job.operation_id) is not None:
        _field("Operation", job.operation_id)
        _field("Submit request", declared(job.submit_request_id))
    uploaded = {item.name for item in job.input_files}
    for declaration in job.input_declarations or []:
        _field("Input", declaration.name)
        _field("Input slot", declaration.slot)
        _field(
            "Input state",
            "uploaded" if declaration.name in uploaded else "not uploaded",
        )
        _field("Input media type", declaration.media_type)
        _field("Input size", _bytes(declaration.size_bytes))
        _field("Input SHA-256", declaration.sha256)
    if job.output_files:
        _field("Output manifest SHA-256", declared(job.output_manifest_sha256))
        for output in job.output_files:
            _field("Output file", output.name)
            _field("Output media type", output.media_type)
            _field("Output size", _bytes(output.size_bytes))
            _field("Output SHA-256", output.sha256)


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


def knob_range(low: object, high: object, step: object) -> str:
    """The range and step a numeric knob declares, in words."""
    if low is not None and high is not None:
        text = f"{low} to {high}"
    else:
        text = f"at least {low}" if low is not None else f"at most {high}"
    return f"{text} in steps of {step}" if step is not None else text


def contract_knob_fields(knob: Knob) -> list[tuple[str, object]]:
    """One knob's printed fields: only what its definition sets."""
    low, high, step = (
        declared(knob.minimum),
        declared(knob.maximum),
        declared(knob.step),
    )
    allowed = declared(knob.allowed_values)
    fields: list[tuple[str, object | None]] = [
        ("Knob", knob.name),
        ("Knob label", declared(knob.label)),
        ("Knob type", knob.type_),
        ("Knob default", knob.default),
        (
            "Knob range",
            knob_range(low, high, step)
            if low is not None or high is not None
            else None,
        ),
        (
            "Knob allowed values",
            ", ".join(str(value) for value in allowed) if allowed else None,
        ),
        ("Knob unit", declared(knob.unit)),
        ("Knob description", declared(knob.description)),
    ]
    return [(label, value) for label, value in fields if value is not None]


def _artifact_job_contract(answer: ArtifactJobContractResponse) -> None:
    contract = answer.contract
    if contract is None or isinstance(contract, Unset):
        raise ControlMalformedResponse("job contract observation is unavailable")
    _field("Recipe", declared(answer.recipe))
    _field("Recipe revision", declared(answer.recipe_revision_id))
    run_id = declared(answer.run_id)
    if run_id is not None:
        _field("Run", run_id)
        _field("Alias", declared(answer.alias))
    _field("Interface", contract.interface)
    _field("Contract SHA-256", declared(answer.contract_sha256))
    _field("Max job timeout", f"{contract.max_timeout_seconds} seconds")
    _field("Input required", "yes" if contract.input_.required else "no")
    _field("Input max bytes", _bytes(contract.input_.max_bytes))
    _contract_slots("Input slot", contract.input_.slots)
    _field("Output max bytes", _bytes(contract.output.max_total_bytes))
    _contract_slots("Output slot", contract.output.slots)
    for knob in contract.parameters:
        for label, value in contract_knob_fields(knob):
            _field(label, value)
    alias = declared(answer.alias)
    parts = ["vonkctl job run", shlex.quote(alias or "<alias>")]
    parts += [f"--input {slot.id}=<path>" for slot in contract.input_.slots[:1]]
    parts += [f"--option {knob.name}=<value>" for knob in contract.parameters[:1]]
    _field("Next", " ".join(parts))


def declared[T](value: T | Unset) -> T | None:
    """A generated optional field, absent as ``None``."""
    return None if isinstance(value, Unset) else value


def _artifact_job(payload: Mapping[str, object], action: str) -> None:
    if action == "contract":
        _artifact_job_contract(ArtifactJobContractResponse.from_dict(dict(payload)))
    elif action == "list":
        _artifact_job_list(payload)
    elif action == Word.DOWNLOAD or (action == Word.RUN and "job_id" in payload):
        _artifact_job_download(payload)
    elif prints_record(action, payload.keys()):
        _artifact_job_record(ArtifactJobResponse.from_dict(dict(payload)))
    else:
        _field("Observation", ENDPOINT_UNAVAILABLE)
