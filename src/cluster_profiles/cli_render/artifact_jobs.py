"""Terminal presentation for artifact jobs responses."""

from __future__ import annotations

import shlex
from collections.abc import Mapping

from ..cli_states import ARTIFACT_JOB_IN_FLIGHT, lifecycle_state
from ..cli_states_generated import SUCCEEDED
from .common import _bytes, _field, _records, _text, _time


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
    if state == "succeeded":
        _field("Output manifest SHA-256", payload.get("output_manifest_sha256"))
        if not output_files:
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

    if isinstance(operation_id, str) and state in ARTIFACT_JOB_IN_FLIGHT:
        _field(
            "Reconnect",
            f"vonkctl recipe job detail {shlex.quote(str(payload.get('id')))} --follow",
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
        if job.get("state") == "succeeded":
            _field(
                "Outputs",
                "none (successful empty result)"
                if not outputs
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
        if isinstance(operation_id, str) and job.get("state") in ARTIFACT_JOB_IN_FLIGHT:
            _field(
                "Reconnect",
                f"vonkctl recipe job detail {shlex.quote(str(job.get('id')))} --follow",
            )


def _artifact_job_download(payload: Mapping[str, object]) -> None:
    _field("Artifact job", payload.get("job_id"))
    state = payload.get("state")
    _field("State", state)
    _field("Output manifest SHA-256", payload.get("output_manifest_sha256"))
    _field("Total output bytes", _bytes(payload.get("total_bytes")))
    files = _records(payload, "files")
    if state != SUCCEEDED:
        _field("Output files", "unavailable")
        return
    if not files:
        print("Result files: none (job succeeded with an empty result).")
        return
    _field("Verified output files", len(files))
    for item in files:
        _field("Output file", item.get("name"))
        _field("File state", item.get("state"))
        _field("Verified path", item.get("path"))
        _field("Verified size", _bytes(item.get("size_bytes")))
        _field("Verified SHA-256", item.get("sha256"))


def _artifact_job(payload: Mapping[str, object], action: str) -> None:
    if action == "list":
        _artifact_job_list(payload)
    elif action == "download":
        _artifact_job_download(payload)
    elif action in {"detail", "create", "upload", "submit", "cancel"}:
        _artifact_job_record(payload, action)
    else:
        _field("Observation", "unavailable")
