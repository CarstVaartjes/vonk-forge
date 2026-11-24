"""Operator workflow for durable recipe artifact jobs."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import stat
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NamedTuple, Protocol, cast, runtime_checkable

from cluster_profiles.cli_files import read_json_document
from cluster_profiles.cli_json_contract import Word
from cluster_profiles.cli_render.artifact_jobs import Knob
from cluster_profiles.cli_select import SelectorError
from cluster_profiles.cli_states import (
    ARTIFACT_JOB_STATES,
    ENDED_WITHOUT_RESULT,
    preparation,
)
from cluster_profiles.cli_states_generated import (
    NEEDS_OPERATOR,
    READY,
    RUN_RUNNING,
    SUCCEEDED,
    TERMINAL_LIFECYCLE_STATES,
    JobContractState,
)
from cluster_profiles.control_client.errors import (
    ControlClientError,
    ControlHTTPError,
    ControlMalformedResponse,
    ControlNotFound,
    ControlResponseTooLarge,
    ControlTransportError,
    JobEndedWithoutResult,
    JobNotFinished,
    LocalFilesystemError,
    TransferUnavailable,
)
from cluster_profiles.control_client.schema import validate_control_document
from cluster_profiles.control_client.transfers import (
    TransferReceipt,
    local_filesystem_error,
)
from cluster_profiles.control_client.transfers import (
    download_destination as _download_destination,
)
from cluster_profiles.control_client.transfers import (
    verified_output as _existing_verified_file,
)
from cluster_profiles.generated_control.media_types import canonical_media_types
from cluster_profiles.generated_control.models.artifact_job_contract_response import (
    ArtifactJobContractResponse,
)
from cluster_profiles.generated_control.models.boolean_parameter import BooleanParameter
from cluster_profiles.generated_control.models.enum_parameter import EnumParameter
from cluster_profiles.generated_control.models.float_parameter import FloatParameter
from cluster_profiles.generated_control.models.integer_parameter import IntegerParameter
from cluster_profiles.generated_control.types import Unset

_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MEDIA_TYPE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$"
)
_CREATE_FIELDS = {
    "interface",
    "parameters",
    "inputs",
    "output_limits",
    "timeout_seconds",
}
_INPUT_FIELDS = {"slot", "name"}
_JOB_TERMINAL_STATES = TERMINAL_LIFECYCLE_STATES | {NEEDS_OPERATOR}
_RECOVERY_ATTEMPTS = 3
_RUN_KEY_NAMESPACE = uuid.UUID("7d0c9a64-3f0e-5b1a-8c52-1f0b6a9e2d41")


@runtime_checkable
class ArtifactJobClient(Protocol):
    @property
    def request_timeout_seconds(self) -> float: ...

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        extra_headers: Mapping[str, str] | None = None,
        query: Mapping[str, object] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]: ...

    def upload_file(
        self,
        path: str,
        source: Path,
        *,
        media_type: str,
        expected_sha256: str,
        expected_size: int,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]: ...

    def download_file(
        self,
        path: str,
        destination: Path,
        *,
        media_type: str,
        expected_sha256: str,
        expected_size: int,
        overwrite: bool,
        timeout_seconds: float | None = None,
    ) -> TransferReceipt: ...


def add_artifact_job_commands(
    commands: Any,
    *,
    add_output: Callable[[argparse.ArgumentParser], None],
    watch_controls: Callable[[argparse.ArgumentParser], None],
    interval_seconds: Callable[[str], float],
) -> None:
    """Attach the ``job`` group: one-command ``run`` and the step commands."""

    job = commands.add_parser(
        "job",
        help="Send files to a running recipe and collect its results",
        description="Jobs send input files to a running recipe and collect its "
        "result files. `run` does it in one command; the other commands are the "
        "steps it is made of.",
    )
    add_output(job)
    actions = job.add_subparsers(dest="job_action", parser_class=type(job))

    run = actions.add_parser(
        Word.RUN,
        help="Create, upload, submit, follow and download one job",
        description="Resolve the running workload, create the job and deliver its "
        "inputs, submit it, follow it to the end and download its result files. "
        "Describe the job with --input and --option (checked against the "
        "recipe's job contract) or with a binding --file. Running the same "
        "command again resumes the same job while it is in progress or after it "
        "succeeded; a changed input makes a new one, and so does running it "
        "again after the job failed or was cancelled.",
        epilog="Example: vonkctl job run image --input prompt=prompt.txt "
        "--option steps=4 --output results",
    )
    run.add_argument(
        "selector",
        nargs="?",
        metavar="ALIAS",
        help="Alias of a running workload (see `vonkctl profile endpoint`)",
    )
    run.add_argument(
        "--file",
        help="Binding file: the job's inputs and options, as JSON "
        "(instead of --input/--option)",
    )
    run.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="SLOT=PATH",
        help="A local file for one of the recipe's input slots (repeatable)",
    )
    run.add_argument(
        "--option",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="A knob of the recipe's job contract; see `vonkctl job contract` for its type, range and allowed values (repeatable)",
    )
    run.add_argument(
        "--output",
        type=Path,
        metavar="DIRECTORY",
        help="Where the result files go (default: ./<job-id>); created if missing",
    )
    run.add_argument(
        "--run",
        type=_uuid_argument,
        metavar="ID",
        help="Use this run (its id is in `vonkctl fleet`) instead of an alias",
    )
    run.add_argument(
        "--no-wait",
        action="store_true",
        help="Stop after submit and print the job id",
    )
    run.add_argument(
        "--interval-seconds",
        type=interval_seconds,
        default=1.0,
        metavar="SECONDS",
        help="Time between refreshes while following, 0.01-30 (default: 1)",
    )
    run.set_defaults(outcome_context="await")
    add_output(run)

    contract = actions.add_parser(
        "contract",
        help="Show the input and output slots a running recipe takes jobs by",
        description="Read the job contract (slot names, media types, size limits "
        "and knobs) before creating a job. Name the run (its id is in "
        "`vonkctl fleet`) or an exact recipe revision.",
    )
    key = contract.add_mutually_exclusive_group(required=True)
    key.add_argument("--run", type=_uuid_argument, metavar="ID")
    key.add_argument(
        "--revision",
        type=_uuid_argument,
        metavar="ID",
        help="An exact recipe revision id (no run needed)",
    )
    add_output(contract)

    list_jobs = actions.add_parser("list", help="List artifact jobs for a run")
    list_jobs.add_argument("--run", required=True, type=_uuid_argument, metavar="ID")
    add_output(list_jobs)

    create = actions.add_parser(
        Word.CREATE,
        help="Create a job and deliver its input files, without submitting it",
    )
    create.add_argument("--run", required=True, type=_uuid_argument, metavar="ID")
    create.add_argument(
        "--file",
        required=True,
        help="Bounded JSON file with a canonical create object and local input paths",
    )
    create.add_argument("--request-key", type=_uuid_argument)
    create.set_defaults(outcome_context="mutation")
    add_output(create)

    upload = actions.add_parser(
        "upload", help="Resume missing inputs on an existing unsubmitted job"
    )
    upload.add_argument("job_id", type=_uuid_argument, metavar="ID")
    upload.add_argument(
        "--file", required=True, help="Same input binding JSON used at create"
    )
    upload.set_defaults(outcome_context="mutation")
    add_output(upload)

    submit = actions.add_parser("submit", help="Submit a ready job for execution")
    submit.add_argument("job_id", type=_uuid_argument, metavar="ID")
    submit.add_argument("--request-key", type=_uuid_argument)
    submit.set_defaults(outcome_context="mutation")
    add_output(submit)

    detail = actions.add_parser("detail", help="Inspect or follow one artifact job")
    detail.add_argument("job_id", type=_uuid_argument, metavar="ID")
    detail.add_argument("--follow", action="store_true")
    watch_controls(detail)
    add_output(detail)

    cancel = actions.add_parser(
        "cancel", help="Request cancellation of an artifact job"
    )
    cancel.add_argument("job_id", type=_uuid_argument, metavar="ID")
    cancel.add_argument("--yes", action="store_true", help="Confirm this cancellation")
    cancel.add_argument(
        "--reason",
        default="Operator requested cancellation with vonkctl",
        help="Reason retained with the cancellation request",
    )
    cancel.add_argument("--request-key", type=_uuid_argument)
    cancel.set_defaults(outcome_context="mutation")
    add_output(cancel)

    download = actions.add_parser(
        "download", help="Retrieve verified result files from a completed job"
    )
    download.add_argument("job_id", type=_uuid_argument, metavar="ID")
    download.add_argument("--output", required=True, type=Path, metavar="DIRECTORY")
    add_output(download)


def run_artifact_job(
    args: argparse.Namespace,
    client: ArtifactJobClient,
    request_id_factory: Callable[[], str],
    *,
    request_key: Callable[[argparse.Namespace, Callable[[], str]], str],
    quote: Callable[[str], str],
    poll: Callable[..., dict[str, object]],
) -> dict[str, object]:
    action = getattr(args, "job_action", None)
    if action == Word.RUN:
        return _run_job(args, client, request_id_factory, request_key, quote, poll)
    if action == "list":

        def read(timeout: float):
            response = client.request(
                "GET",
                f"/api/recipe/runs/{args.run}/artifact-jobs",
                timeout_seconds=timeout,
            )
            jobs = response.get("jobs")
            if not isinstance(jobs, list):
                raise ControlMalformedResponse("artifact job list is invalid")
            return {"jobs": [_job(item, expected_run=args.run) for item in jobs]}

        return dict(
            _recover(client, read, lambda _timeout: None, retry_if=_transient_failure)
        )
    if action == "contract":
        return _contract(args, client, quote)
    if action == "create":
        return _create(args, client, request_id_factory, request_key, quote)
    if action == "upload":
        return _upload_existing(args, client, quote)
    if action == "submit":
        return _submit(args, client, request_id_factory, request_key, quote)
    if action == "detail":
        path = f"/api/artifact-jobs/{quote(args.job_id)}"
        result = _recover(
            client,
            lambda timeout: _job(
                client.request("GET", path, timeout_seconds=timeout),
                expected_id=args.job_id,
            ),
            lambda _timeout: None,
            retry_if=_transient_failure,
        )
        if not args.follow:
            return result
        return poll(
            client,
            path,
            result,
            args,
            terminal=lambda value: value.get("state") in _JOB_TERMINAL_STATES,
            validate=lambda value: _same_job(value, args.job_id),
        )
    if action == "cancel":
        return _cancel(args, client, request_id_factory, request_key, quote)
    if action == "download":
        return _recover(
            client,
            lambda timeout: _download(args, client, quote, timeout),
            lambda _timeout: None,
            retry_if=_transient_failure,
        )
    raise ValueError(f"unsupported job action: {action}")


def _content_request_key(run_id: str, canonical_body: str, previous: str = "") -> str:
    """One key per run and job content: the canonical create body already
    carries the sha256 of every input file, so equal content resumes the same
    job and any change makes a new one. ``previous`` chains the next key after
    a job with this content ended failed or cancelled."""
    name = f"{run_id}\n{canonical_body}\n{previous}"
    return str(uuid.uuid5(_RUN_KEY_NAMESPACE, name))


def _content_key_for_rerun(
    args: argparse.Namespace,
    client: ArtifactJobClient,
    quote: Callable[[str], str],
    canonical_body: str,
) -> str:
    """The key of the job this content should continue, or of a new one.

    A job in progress, or one that succeeded, is resumed (reconnect, no
    duplicate work). One that ended failed or cancelled answered an earlier
    request; running the command again is a new request, so the next key in a
    deterministic chain (this key plus that job's id) starts a new job. The
    chain is followed to its end and the last ended job is reported once.
    """
    key = _content_request_key(args.run, canonical_body)

    def lookup(request_key: str):
        def read(timeout: float):
            return _job(
                client.request(
                    "GET",
                    f"/api/artifact-jobs/requests/{quote(request_key)}",
                    timeout_seconds=timeout,
                ),
                expected_run=args.run,
            )

        try:
            return _recover(
                client, read, lambda _timeout: None, retry_if=_transient_failure
            )
        except ControlNotFound:
            return None

    found = lookup(key)
    ended = None
    # Each iteration consumes one distinct ended job of this content; the
    # Controller holds finitely many.
    while found is not None and found.get("state") in ENDED_WITHOUT_RESULT:
        ended = found
        key = _content_request_key(args.run, canonical_body, cast(str, found["id"]))
        found = lookup(key)
    if ended is not None and not _json_mode(args):
        print(
            f"previous job {ended['id']} {ended['state']}: "
            f"{ended.get('status_reason') or 'no reason recorded'}; "
            "starting a new one",
            file=sys.stderr,
            flush=True,
        )
    return key


class _LoadedRun(NamedTuple):
    alias: str | None


def _resolve_run(client: ArtifactJobClient, selector: str | None) -> str:
    """The one running workload of the fleet that ``selector`` names.

    The fleet's loaded workloads are the source (as for ``vonkctl fleet``); the
    alias selects. Nothing or several matches are refused with the candidates,
    never guessed. A run on several Sparks is one candidate.
    """
    if not selector:
        raise SelectorError(
            "name the running workload by its alias or pass --run ID; "
            "`vonkctl profile endpoint` lists the aliases"
        )

    def read(timeout: float):
        return client.request("GET", "/api/fleet", timeout_seconds=timeout)

    fleet = _recover(client, read, lambda _timeout: None, retry_if=_transient_failure)
    nodes = fleet.get("nodes")
    if not isinstance(nodes, list):
        raise ControlMalformedResponse("fleet membership is unavailable")
    running: dict[str, _LoadedRun] = {}
    for node in nodes:
        for run in node.get("loaded") or () if isinstance(node, Mapping) else ():
            if (
                isinstance(run, Mapping)
                and run.get("run_state") == RUN_RUNNING
                and isinstance(run.get("run_id"), str)
            ):
                running.setdefault(
                    cast(str, run["run_id"]), _LoadedRun(run.get("alias"))
                )

    def label(run_id: str, loaded: _LoadedRun) -> str:
        return f"{loaded.alias} ({run_id})"

    matches = [run_id for run_id, loaded in running.items() if loaded.alias == selector]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise SelectorError(
            f"{len(matches)} running workloads have the alias '{selector}'; "
            "pass --run ID to choose one",
            candidates=[label(run_id, running[run_id]) for run_id in matches],
        )
    raise SelectorError(
        f"no running workload has the alias '{selector}'"
        + ("" if running else "; nothing is running")
        + " (see `vonkctl fleet`)",
        candidates=[label(run_id, loaded) for run_id, loaded in running.items()],
    )


def _binding_from_flags(
    args: argparse.Namespace,
    client: ArtifactJobClient,
    quote: Callable[[str], str],
    capabilities: Any,
):
    """The create object and input paths for ``--input``/``--option``, checked
    against the run's job contract (slot names and counts, knob names and types)."""
    args.revision = None
    answer = ArtifactJobContractResponse.from_dict(_contract(args, client, quote))
    contract = answer.contract
    if contract is None or isinstance(contract, Unset):
        raise ControlMalformedResponse("job contract observation is unavailable")
    slots = {slot.id: slot for slot in contract.input_.slots}
    chosen: dict[str, list[Path]] = {slot_id: [] for slot_id in slots}
    for item in args.input:
        slot_id, separator, raw_path = item.partition("=")
        if not separator or not raw_path:
            raise ValueError(f"--input takes SLOT=PATH, got '{item}'")
        if slot_id not in slots:
            raise ValueError(
                f"unknown input slot '{slot_id}'; this recipe takes: "
                + ", ".join(slots)
            )
        chosen[slot_id].append(Path(raw_path).absolute())
    inputs = []
    paths: dict[str, Path] = {}
    for slot_id in slots:
        for path in chosen[slot_id]:
            if path.name in paths:
                raise ValueError(f"two inputs are named {path.name}")
            paths[path.name] = path
            inputs.append({"slot": slot_id, "name": path.name})
    knobs = {knob.name: knob for knob in contract.parameters}
    parameters = {}
    for item in args.option:
        key, separator, raw_value = item.partition("=")
        if not separator:
            raise ValueError(f"--option takes KEY=VALUE, got '{item}'")
        if key not in knobs:
            raise ValueError(
                f"unknown option '{key}'; this recipe takes: " + ", ".join(knobs)
            )
        parameters[key] = _knob_value(knobs[key], raw_value)
    transport = _mapping(capabilities.get("transport"), "artifact job capabilities")
    limits = contract.output_limits
    output_limits = {
        "max_files": min(
            limits.max_files, _integer(transport.get("max_output_files"), "limit")
        ),
        "max_file_bytes": min(
            limits.max_file_bytes,
            _integer(transport.get("max_output_file_bytes"), "limit"),
        ),
        "max_total_bytes": min(
            limits.max_total_bytes,
            _integer(transport.get("max_output_total_bytes"), "limit"),
        ),
        "allowed_media_types": limits.allowed_media_types,
    }
    create = {
        "interface": str(contract.interface),
        "parameters": parameters,
        "inputs": inputs,
        "output_limits": output_limits,
        "timeout_seconds": min(
            contract.max_timeout_seconds,
            _integer(transport.get("max_timeout_seconds"), "job timeout limit"),
        ),
    }
    return create, paths


def _knob_value(knob: Knob, raw: str) -> object:
    """The typed value of ``--option``. An enum names its allowed values when the
    text matches none; range and step stay with the Controller."""
    if isinstance(knob, EnumParameter):
        for member in knob.allowed_values:
            if str(member) == raw:
                return member
        listed = ", ".join(str(member) for member in knob.allowed_values)
        raise ValueError(f"option '{knob.name}' takes one of: {listed}; got '{raw}'")
    try:
        if isinstance(knob, IntegerParameter):
            return int(raw)
        if isinstance(knob, FloatParameter):
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError(raw)
            return value
        if isinstance(knob, BooleanParameter):
            if raw.lower() not in {"true", "false"}:
                raise ValueError(raw)
            return raw.lower() == "true"
    except ValueError:
        raise ValueError(
            f"option '{knob.name}' takes a {knob.type_} value, got '{raw}'"
        ) from None
    return raw


def _run_job(
    args: argparse.Namespace,
    client: ArtifactJobClient,
    request_id_factory: Callable[[], str],
    request_key: Callable[[argparse.Namespace, Callable[[], str]], str],
    quote: Callable[[str], str],
    poll: Callable[..., Any],
):
    """Create, deliver, submit, follow and download through the step functions.

    The create key is derived from the job's content, so running the same
    command again finds the same job and continues from where it stopped.
    """
    if args.file is not None and (args.input or args.option):
        raise ValueError("--file cannot be combined with --input or --option")
    args.run = args.run or _resolve_run(client, args.selector)
    args.artifact_job_run = True
    rerun = shlex.join(["vonkctl", *getattr(args, "invocation", ())])
    args.artifact_job_reconcile = rerun
    try:
        job = _create(
            args, client, request_id_factory, request_key, quote, content_key=True
        )
        args.job_id = cast(str, job["id"])
        if job.get("operation_id") is None:
            args.request_key = str(
                uuid.uuid5(_RUN_KEY_NAMESPACE, f"submit\n{args.request_key}")
            )
            job = _submit(args, client, request_id_factory, request_key, quote)
    except BaseException:
        args.artifact_job_reconcile = rerun
        raise
    args.artifact_job_reconcile = rerun
    # Submit marks its own receipt a read; the run still ends in the job's outcome.
    args.outcome_context = "await"
    if args.no_wait:
        return job
    if not _json_mode(args):
        print(f"Job ID: {args.job_id}", file=sys.stderr, flush=True)
    path = f"/api/artifact-jobs/{quote(args.job_id)}"
    # Follow until the job's own timeout, not the short watch cap of other commands.
    follow_seconds = _integer(job.get("timeout_seconds"), "job timeout")
    job = poll(
        client,
        path,
        job,
        args,
        terminal=lambda value: value.get("state") in _JOB_TERMINAL_STATES,
        validate=lambda value: _same_job(value, args.job_id),
        timeout_seconds=follow_seconds,
    )
    if job.get("state") != SUCCEEDED:
        # Unfinished (watch deadline) or ended without a result: the job
        # document carries the state and the reason; the outcome decides the exit.
        return job
    if args.output is None:
        args.output = Path(args.job_id)
    return _recover(
        client,
        lambda timeout: _download(args, client, quote, timeout),
        lambda _timeout: None,
        retry_if=_transient_failure,
    )


def _create(
    args: argparse.Namespace,
    client: ArtifactJobClient,
    request_id_factory: Callable[[], str],
    request_key: Callable[[argparse.Namespace, Callable[[], str]], str],
    quote: Callable[[str], str],
    *,
    content_key: bool = False,
) -> dict[str, object]:
    capabilities = _capabilities(client)
    if args.file is None:
        binding, paths = _binding_from_flags(args, client, quote, capabilities)
    else:
        binding, paths = _read_binding(args.file)
    declarations = _recover(
        client,
        lambda _timeout: _prepare_inputs(binding, paths, capabilities),
        lambda _timeout: None,
    )
    body = _canonical_create(binding, declarations, capabilities)
    key = (
        _content_key_for_rerun(args, client, quote, json.dumps(body, sort_keys=True))
        if content_key
        else _stable_request_key(args, request_id_factory, request_key)
    )
    args.request_key = key
    args.artifact_job_run_id = args.run
    args.artifact_job_binding_file = args.file
    args.artifact_job_uploaded = []
    if not _json_mode(args) and not content_key:
        print(
            f"Request key: {key}\nReconnect: vonkctl job create --run {args.run} "
            f"--file {args.file} --request-key {key}",
            file=sys.stderr,
            flush=True,
        )
    path = f"/api/recipe/runs/{quote(args.run)}/artifact-jobs"
    job = _create_with_reconcile(
        args, client, path, body, key, quote, expected_run=args.run
    )
    if not _json_mode(args):
        print(f"Job ID (not submitted): {job['id']}", file=sys.stderr, flush=True)
    args.artifact_job_id = job["id"]
    args.artifact_job_reconcile = f"vonkctl job upload {job['id']} --file {args.file}"
    if preparation(job) is not None:
        job = _recover(
            client,
            lambda _timeout: _deliver_inputs(
                args, client, job, declarations, paths, quote
            ),
            lambda _timeout: None,
        )
    return job


def _create_with_reconcile(
    args: argparse.Namespace,
    client: ArtifactJobClient,
    path: str,
    body: dict[str, object],
    key: str,
    quote: Callable[[str], str],
    *,
    expected_run: str,
) -> dict[str, object]:
    def send(timeout: float):
        return _create_receipt(
            client.request(
                "POST",
                path,
                body,
                extra_headers={"X-Request-ID": key},
                timeout_seconds=timeout,
            ),
            expected_run=expected_run,
            expected_body=body,
        )

    def observe(timeout: float):
        observed = _job(
            client.request(
                "GET",
                f"/api/artifact-jobs/requests/{quote(key)}",
                timeout_seconds=timeout,
            ),
            expected_run=expected_run,
        )
        args.artifact_job_id = observed["id"]
        # The projection does not echo all parameters. Only the identical POST
        # can confirm the complete accepted create intent at its owner.

    return _recover(client, send, observe)


def _create_receipt(
    value: Mapping[str, object],
    *,
    expected_run: str,
    expected_body: Mapping[str, object],
) -> dict[str, object]:
    job = _job(value, expected_run=expected_run)
    expected_inputs = expected_body.get("inputs")
    if not isinstance(expected_inputs, list):
        raise ControlMalformedResponse(
            "artifact create receipt cannot be compared with its request"
        )
    _match_input_binding(job, expected_body, expected_inputs)
    if preparation(job) is None and job["state"] not in ARTIFACT_JOB_STATES:
        raise ControlMalformedResponse("artifact create receipt has an invalid state")
    return job


def _upload_existing(
    args: argparse.Namespace, client: ArtifactJobClient, quote: Callable[[str], str]
) -> dict[str, object]:
    binding, paths = _read_binding(args.file)
    capabilities = _capabilities(client)
    declarations = _recover(
        client,
        lambda _timeout: _prepare_inputs(binding, paths, capabilities),
        lambda _timeout: None,
    )
    _canonical_create(binding, declarations, capabilities)
    args.artifact_job_id = args.job_id
    args.artifact_job_binding_file = args.file
    args.artifact_job_uploaded = []
    args.artifact_job_reconcile = f"vonkctl job upload {args.job_id} --file {args.file}"
    job = _recover(
        client,
        lambda timeout: _job(
            client.request(
                "GET",
                f"/api/artifact-jobs/{quote(args.job_id)}",
                timeout_seconds=timeout,
            ),
            expected_id=args.job_id,
        ),
        lambda _timeout: None,
    )
    return _deliver_inputs(args, client, job, declarations, paths, quote)


def _deliver_inputs(
    args: argparse.Namespace,
    client: ArtifactJobClient,
    job: dict[str, object],
    declarations: list[dict[str, object]],
    paths: dict[str, Path],
    quote: Callable[[str], str],
) -> dict[str, object]:
    transfer = _transfer_client(client)
    uploaded = cast(list[dict[str, object]], args.artifact_job_uploaded)
    uploaded_by_name = _declared_files(job.get("input_files"), "uploaded inputs")
    # The Controller derived each input's media type; the upload carries it.
    accepted_by_name = _declared_files(job.get("input_declarations"), "declarations")
    for local in declarations:
        name = cast(str, local["name"])
        declaration = accepted_by_name.get(name)
        if (
            declaration is None
            or {key: value for key, value in declaration.items() if key != "media_type"}
            != local
        ):
            raise ControlMalformedResponse(
                "artifact job declarations differ from the requested inputs"
            )
        current = uploaded_by_name.get(name)
        if current == declaration:
            continue
        path = f"/api/artifact-jobs/{quote(cast(str, job['id']))}/inputs/{quote(name)}"
        identifier = cast(str, job["id"])

        def send(
            timeout: float,
            path=path,
            name=name,
            declaration=declaration,
            identifier=identifier,
        ):
            accepted = _job(
                transfer.upload_file(
                    path,
                    paths[name],
                    media_type=cast(str, declaration["media_type"]),
                    expected_sha256=cast(str, declaration["sha256"]),
                    expected_size=cast(int, declaration["size_bytes"]),
                    timeout_seconds=timeout,
                ),
                expected_id=identifier,
            )
            files = _declared_files(accepted.get("input_files"), "uploaded inputs")
            if files.get(name) != declaration:
                raise ControlMalformedResponse(
                    "artifact upload acknowledgement is incomplete"
                )
            return accepted

        def observe(
            timeout: float, name=name, declaration=declaration, identifier=identifier
        ):
            observed = _job(
                client.request(
                    "GET",
                    f"/api/artifact-jobs/{quote(identifier)}",
                    timeout_seconds=timeout,
                ),
                expected_id=identifier,
            )
            files = _declared_files(observed.get("input_files"), "uploaded inputs")
            return observed if files.get(name) == declaration else None

        job = _recover(client, send, observe)
        uploaded.append({"name": name, "sha256": declaration["sha256"]})
        uploaded_by_name = _declared_files(job.get("input_files"), "uploaded inputs")
    if preparation(job) == READY or (
        declarations
        and all(
            uploaded_by_name.get(cast(str, item["name"]))
            == accepted_by_name.get(cast(str, item["name"]))
            for item in declarations
        )
        and preparation(job) is None
    ):
        return job
    # Finalization is idempotent at the owner. Its lost reply is observed; a
    # concurrent submit is accepted as evidence that preparation completed.
    identifier = cast(str, job["id"])

    def finalize(timeout: float):
        return _job(
            client.request(
                "POST",
                f"/api/artifact-jobs/{quote(identifier)}/finalize",
                timeout_seconds=timeout,
            ),
            expected_id=identifier,
        )

    def observe_finalization(timeout: float):
        observed = _job(
            client.request(
                "GET",
                f"/api/artifact-jobs/{quote(identifier)}",
                timeout_seconds=timeout,
            ),
            expected_id=identifier,
        )
        return (
            observed
            if preparation(observed) == READY
            or observed.get("operation_id") is not None
            else None
        )

    return _recover(client, finalize, observe_finalization)


def _submit(
    args: argparse.Namespace,
    client: ArtifactJobClient,
    request_id_factory: Callable[[], str],
    request_key: Callable[[argparse.Namespace, Callable[[], str]], str],
    quote: Callable[[str], str],
) -> dict[str, object]:
    key = _stable_request_key(args, request_id_factory, request_key)
    args.request_key = key
    args.artifact_job_id = args.job_id
    command = [
        "vonkctl",
        "recipe",
        "job",
        "submit",
        args.job_id,
        "--request-key",
        key,
    ]
    args.artifact_job_reconcile = shlex.join(command)
    if not _json_mode(args) and not getattr(args, "artifact_job_run", False):
        print(
            f"Request key: {key}\nReconnect: {args.artifact_job_reconcile}",
            file=sys.stderr,
            flush=True,
        )
    path = f"/api/artifact-jobs/{quote(args.job_id)}/submit"

    def receipt(value):
        job = _job(value, expected_id=args.job_id, expected_submit_request_id=key)
        if not isinstance(job.get("operation_id"), str):
            raise ControlMalformedResponse(
                "artifact submit receipt has no operation identity"
            )
        return job

    def send(timeout: float):
        return receipt(
            client.request(
                "POST",
                path,
                extra_headers={"X-Request-ID": key},
                timeout_seconds=timeout,
            )
        )

    def observe(timeout: float):
        job = _job(
            client.request(
                "GET",
                f"/api/artifact-jobs/{quote(args.job_id)}",
                timeout_seconds=timeout,
            ),
            expected_id=args.job_id,
        )
        # State is not acceptance evidence. A concurrent ending or another
        # submit belongs to the owner; replay the original key there.
        if job.get("submit_request_id") == key and job.get("operation_id") is not None:
            return receipt(job)
        return None

    job = _recover(client, send, observe)
    # Exact acceptance succeeded; later execution state cannot veto the request.
    args.outcome_context = "read"
    return job


def _cancel(
    args: argparse.Namespace,
    client: ArtifactJobClient,
    request_id_factory: Callable[[], str],
    request_key: Callable[[argparse.Namespace, Callable[[], str]], str],
    quote: Callable[[str], str],
) -> dict[str, object]:
    if not args.yes:
        raise ValueError("job cancel requires --yes")
    reason = " ".join(args.reason.split())
    if not reason or len(reason) > 512:
        raise ValueError("cancellation reason must contain 1 to 512 characters")
    key = _stable_request_key(args, request_id_factory, request_key)
    args.request_key = key
    args.artifact_job_id = args.job_id
    args.artifact_job_reconcile = shlex.join(
        [
            "vonkctl",
            "recipe",
            "job",
            "cancel",
            args.job_id,
            "--yes",
            "--reason",
            reason,
            "--request-key",
            key,
        ]
    )
    if not _json_mode(args):
        print(
            f"Request key: {key}\nReconnect: {args.artifact_job_reconcile}",
            file=sys.stderr,
            flush=True,
        )
    path = f"/api/artifact-jobs/{quote(args.job_id)}/cancel"

    def accepted(job) -> bool:
        evidence = job.get("result_evidence")
        return (
            isinstance(evidence, Mapping)
            and evidence.get("cancel_request_id") == key
            and evidence.get("cancel_reason") == reason
        )

    def send(timeout: float):
        job = _job(
            client.request(
                "POST",
                path,
                {"reason": reason},
                extra_headers={"X-Request-ID": key},
                timeout_seconds=timeout,
            ),
            expected_id=args.job_id,
        )
        if not accepted(job):
            raise ControlMalformedResponse(
                "artifact cancellation receipt does not identify this request"
            )
        return job

    def observe(timeout: float):
        job = _job(
            client.request(
                "GET",
                f"/api/artifact-jobs/{quote(args.job_id)}",
                timeout_seconds=timeout,
            ),
            expected_id=args.job_id,
        )
        return job if accepted(job) else None

    job = _recover(client, send, observe)
    # Cancellation acceptance belongs to the owner, even after concurrent failure.
    args.outcome_context = "read"
    return job


def _download(
    args: argparse.Namespace,
    client: ArtifactJobClient,
    quote: Callable[[str], str],
    timeout: float,
) -> dict[str, object]:
    args.artifact_job_id = args.job_id
    args.artifact_job_downloaded = []
    result = _job(
        client.request(
            "GET", f"/api/artifact-jobs/{quote(args.job_id)}", timeout_seconds=timeout
        ),
        expected_id=args.job_id,
    )
    if result["state"] != SUCCEEDED:
        state = result["state"]
        if state in ENDED_WITHOUT_RESULT:
            raise JobEndedWithoutResult(
                f"job {args.job_id} ended {state} and has no result files: "
                f"{result.get('status_reason') or 'no reason recorded'}",
                detail=f"vonkctl {Word.JOB} {Word.DETAIL} {args.job_id}",
            )
        follow = f"vonkctl {Word.JOB} {Word.DETAIL} {args.job_id} --follow"
        raise JobNotFinished(
            f"job {args.job_id} is {state} and has no result yet; wait with {follow}",
            follow=follow,
        )
    digest = result.get("output_manifest_sha256")
    outputs = result.get("output_files")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise ControlMalformedResponse("artifact result manifest digest is invalid")
    if not isinstance(outputs, list):
        raise ControlMalformedResponse("artifact job result files are invalid")
    limits = _mapping(result.get("output_limits"), "artifact output limits")
    maximum_files = _integer(limits.get("max_files"), "artifact output file limit")
    maximum_file_bytes = _integer(
        limits.get("max_file_bytes"), "artifact output file byte limit"
    )
    maximum_total_bytes = _integer(
        limits.get("max_total_bytes"), "artifact output total byte limit"
    )
    allowed_media_types = limits.get("allowed_media_types")
    if not isinstance(allowed_media_types, list) or any(
        not isinstance(media_type, str) for media_type in allowed_media_types
    ):
        raise ControlMalformedResponse("artifact output media type limits are invalid")
    if len(outputs) > maximum_files:
        raise ControlMalformedResponse(
            "artifact result exceeds its declared file limit"
        )
    names: set[str] = set()
    total = 0
    declared: list[dict[str, object]] = []
    for item in outputs:
        if not isinstance(item, dict):
            raise ControlMalformedResponse("artifact result file metadata is invalid")
        name = item.get("name")
        media_type = item.get("media_type")
        size_bytes = item.get("size_bytes")
        sha256 = item.get("sha256")
        if (
            not isinstance(name, str)
            or _NAME.fullmatch(name) is None
            or name == "manifest.json"
            or not isinstance(media_type, str)
            or _MEDIA_TYPE.fullmatch(media_type) is None
            or media_type not in allowed_media_types
            or type(size_bytes) is not int
            or not 0 <= size_bytes <= maximum_file_bytes
            or not isinstance(sha256, str)
            or _DIGEST.fullmatch(sha256) is None
            or name in names
        ):
            raise ControlMalformedResponse("artifact result file metadata is invalid")
        names.add(name)
        total += size_bytes
        declared.append(item)
    if total > maximum_total_bytes:
        raise ControlMalformedResponse(
            "artifact result exceeds its declared byte limit"
        )
    deadline = time.monotonic() + timeout
    directory = _output_directory(args.output, create=True)
    transfer = _transfer_client(client)
    downloaded = cast(list[dict[str, object]], args.artifact_job_downloaded)
    args.artifact_job_download_total = len(declared)
    for item in declared:
        name = cast(str, item["name"])
        media_type = cast(str, item["media_type"])
        size_bytes = cast(int, item["size_bytes"])
        sha256 = cast(str, item["sha256"])
        destination = _download_destination(directory / name, size_bytes, sha256)
        if _existing_verified_file(destination, size_bytes, sha256):
            downloaded.append(
                {
                    "name": name,
                    "path": str(destination),
                    "size_bytes": size_bytes,
                    "sha256": sha256,
                    "state": "reused",
                }
            )
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ControlTransportError(
                "artifact download observation deadline reached"
            )
        response = transfer.download_file(
            f"/api/artifact-jobs/{quote(args.job_id)}/results/{quote(name)}/{sha256}",
            destination,
            media_type=media_type,
            expected_sha256=sha256,
            expected_size=size_bytes,
            overwrite=False,
            timeout_seconds=remaining,
        )
        downloaded.append(
            {
                "name": name,
                "path": response.get("destination", str(destination)),
                "size_bytes": size_bytes,
                "sha256": sha256,
                "state": "downloaded",
                **(
                    {"transfer": dict(response)}
                    if isinstance(response, Mapping)  # pyright: ignore[reportUnnecessaryIsInstance]  # a Controller response is checked before use
                    else {}
                ),
            }
        )
    return {
        "job_id": args.job_id,
        "state": result["state"],
        "output_manifest_sha256": digest,
        "files": downloaded,
        "total_bytes": total,
    }


def _read_binding(source: str) -> tuple[dict[str, object], dict[str, Path]]:
    raw = read_json_document(source)
    if not isinstance(raw, dict) or set(raw) != {"create", "input_paths"}:
        raise ValueError(
            "artifact input file must contain exactly create and input_paths"
        )
    create = raw.get("create")
    paths_value = raw.get("input_paths")
    if not isinstance(create, dict) or set(create) - _CREATE_FIELDS:
        raise ValueError("artifact input create object has unknown fields")
    if not isinstance(paths_value, dict):
        raise TypeError("artifact input_paths must map each input name to a local path")
    inputs = create.get("inputs", [])
    if not isinstance(inputs, list) or len(inputs) > 32:
        raise ValueError(
            "artifact job input declarations must be an array of at most 32 files"
        )
    names: set[str] = set()
    for item in inputs:
        if not isinstance(item, dict) or set(item) != _INPUT_FIELDS:
            raise ValueError(
                "each local input declaration must contain exactly slot and name"
            )
        name = item.get("name")
        if not isinstance(name, str) or _NAME.fullmatch(name) is None or name in names:
            raise ValueError("artifact input names must be unique safe file names")
        names.add(name)
        if not isinstance(item.get("slot"), str):
            raise TypeError("artifact input slot must be a string")
    if set(paths_value) != names:
        raise ValueError("input_paths must name every declared input exactly once")
    base = Path.cwd() if source == "-" else Path(source).absolute().parent
    paths: dict[str, Path] = {}
    for name, raw_path in paths_value.items():
        if not isinstance(raw_path, str) or not raw_path or len(raw_path) > 4096:
            raise ValueError(f"local path for {name} must be a non-empty path string")
        local = Path(raw_path)
        paths[name] = local if local.is_absolute() else base / local
    return create, paths


def _prepare_inputs(
    create: Mapping[str, object],
    paths: Mapping[str, Path],
    capabilities: Mapping[str, object],
) -> list[dict[str, object]]:
    transport = _mapping(capabilities.get("transport"), "artifact job capabilities")
    maximum_files = _integer(transport.get("max_input_files"), "input file limit")
    maximum_file_bytes = _integer(
        transport.get("max_input_file_bytes"), "input file byte limit"
    )
    maximum_total_bytes = _integer(
        transport.get("max_input_total_bytes"), "input total byte limit"
    )
    reserved_names_value = transport.get("reserved_input_names")
    if not isinstance(reserved_names_value, list) or any(
        not isinstance(name, str) for name in reserved_names_value
    ):
        raise ControlMalformedResponse("artifact job reserved input names are invalid")
    inputs = create.get("inputs", [])
    if not isinstance(inputs, list) or len(inputs) > maximum_files:
        raise ValueError(
            f"artifact input count exceeds the Controller limit of {maximum_files}"
        )
    declarations: list[dict[str, object]] = []
    total = 0
    for item in inputs:
        if not isinstance(item, dict):
            raise TypeError("artifact input declaration is invalid")
        name = cast(str, item["name"])
        if name in reserved_names_value:
            raise ValueError(
                f"artifact input name is reserved by the Controller: {name}"
            )
        size, digest = _digest_regular_file(paths[name], maximum_file_bytes, name)
        total += size
        declarations.append(
            {
                "slot": item["slot"],
                "name": name,
                "size_bytes": size,
                "sha256": digest,
            }
        )
    if total > maximum_total_bytes:
        raise ValueError(
            f"artifact input bytes {total} exceed the Controller limit of {maximum_total_bytes}"
        )
    return declarations


def _canonical_create(
    create: Mapping[str, object],
    declarations: list[dict[str, object]],
    capabilities: Mapping[str, object],
) -> dict[str, object]:
    body: dict[str, object] = {
        "interface": create.get("interface"),
        "parameters": create.get("parameters", {}),
        "inputs": declarations,
        "output_limits": create.get("output_limits"),
        "timeout_seconds": create.get("timeout_seconds"),
    }
    validated = validate_control_document("ArtifactJobCreate", body)
    transport = _mapping(capabilities.get("transport"), "artifact job capabilities")
    output_limits = _mapping(validated.get("output_limits"), "artifact output limits")
    output_limits = {
        **output_limits,
        "allowed_media_types": canonical_media_types(
            cast(list[str], output_limits["allowed_media_types"])
        ),
    }
    validated["output_limits"] = output_limits
    maximum_timeout = _integer(
        transport.get("max_timeout_seconds"), "job timeout limit"
    )
    timeout = _integer(validated.get("timeout_seconds"), "job timeout")
    if timeout > maximum_timeout:
        raise ValueError(
            f"job timeout {timeout} exceeds the Controller limit of {maximum_timeout}"
        )
    for field, capability in (
        ("max_files", "max_output_files"),
        ("max_file_bytes", "max_output_file_bytes"),
        ("max_total_bytes", "max_output_total_bytes"),
    ):
        value = _integer(output_limits.get(field), f"output {field}")
        maximum = _integer(transport.get(capability), f"{field} capability")
        if value > maximum:
            raise ValueError(
                f"output {field} {value} exceeds the Controller limit of {maximum}"
            )
    return validated


def _digest_regular_file(path: Path, maximum: int, name: str) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise LocalFilesystemError(
            "artifact inputs cannot be opened safely on this platform"
        )
    descriptor = -1
    try:
        descriptor = os.open(path, flags | no_follow)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(
                f"artifact input {name} must be a regular non-symlink file"
            )
        if before.st_size > maximum:
            raise ValueError(
                f"artifact input {name} is {before.st_size} bytes; the Controller limit is {maximum}"
            )
        digest = hashlib.sha256()
        observed = 0
        while observed <= maximum:
            chunk = os.read(descriptor, min(1024**2, maximum + 1 - observed))
            if not chunk:
                break
            observed += len(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_before != identity_after or observed != before.st_size:
            raise ControlTransportError(
                "artifact input observation changed while being read"
            )
        return observed, digest.hexdigest()
    except OSError as error:
        raise LocalFilesystemError(
            f"artifact input {name} must be a readable regular non-symlink file"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


#: Where a contract that cannot be given points next, per state the Controller
#: answers: `profile endpoint` shows whether a load is still in flight, and a
#: revision's contract needs no run.
_CONTRACT_NEXT_STEPS = {
    JobContractState.NOT_RUNNING: (
        "vonkctl profile endpoint",
        "vonkctl job contract --revision <recipe-revision-id>",
    ),
    JobContractState.NOT_A_JOB_RECIPE: ("vonkctl recipe detail <recipe-revision-id>",),
    JobContractState.CONTRACT_INVALID: ("vonkctl recipe library",),
}
_CONTRACT_NOT_FOUND_STEPS = ("vonkctl fleet", "vonkctl profile list")


def _contract(
    args: argparse.Namespace,
    client: ArtifactJobClient,
    quote: Callable[[str], str],
):
    """One bounded read; the Controller's typed answer is final."""
    by, key = ("run", args.run) if args.run else ("revision", args.revision)
    try:
        value = client.request("GET", f"/api/artifact-jobs/contract/{by}/{quote(key)}")
    except ControlNotFound as error:
        error.recovery = _CONTRACT_NOT_FOUND_STEPS
        raise
    answer = validate_control_document("ArtifactJobContractResponse", value)
    if answer.get("contract") is None:
        # The Controller answered that there is no contract to give; the usual
        # error document carries the state as its code, the cause and next steps.
        state = JobContractState(answer["state"])
        raise ControlHTTPError(
            409,
            str(answer.get("detail")),
            code="job_contract." + state.value,
            recovery=_CONTRACT_NEXT_STEPS[state],
        )
    return answer


def _capabilities(client: ArtifactJobClient) -> dict[str, object]:
    def read(timeout: float):
        value = client.request(
            "GET", "/api/artifact-jobs/capabilities", timeout_seconds=timeout
        )
        return validate_control_document("ArtifactJobCapabilitiesResponse", value)

    return _recover(client, read, lambda _timeout: None, retry_if=_transient_failure)


def _job(
    value: Mapping[str, object],
    *,
    expected_id: str | None = None,
    expected_run: str | None = None,
    expected_submit_request_id: str | None = None,
) -> dict[str, object]:
    job = validate_control_document("ArtifactJobResponse", dict(value))
    identifier = job.get("id")
    run_id = job.get("run_id")
    if not isinstance(identifier, str) or _UUID.fullmatch(identifier) is None:
        raise ControlMalformedResponse("artifact job response has an invalid identity")
    if expected_id is not None and identifier != expected_id:
        raise ControlMalformedResponse("artifact job response identifies another job")
    if expected_run is not None and run_id != expected_run:
        raise ControlMalformedResponse("artifact job response identifies another run")
    operation_id = job.get("operation_id")
    submit_request_id = job.get("submit_request_id")
    if (operation_id is None) != (submit_request_id is None):
        raise ControlMalformedResponse("artifact job submission identity is incomplete")
    if (
        expected_submit_request_id is not None
        and submit_request_id != expected_submit_request_id
    ):
        raise ControlMalformedResponse(
            "artifact job response identifies another submit request"
        )
    return job


def _same_job(value: Mapping[str, object], expected_id: str) -> None:
    _job(value, expected_id=expected_id)


def _match_input_binding(
    job: Mapping[str, object],
    create: Mapping[str, object] | None,
    declarations: list[dict[str, object]],
) -> None:
    actual = job.get("input_declarations")
    expected_by_name = {cast(str, item["name"]): item for item in declarations}
    if not isinstance(actual, (list, tuple)):
        raise ControlMalformedResponse("artifact job input declarations are invalid")
    actual_by_name = {
        name: {key: value for key, value in item.items() if key != "media_type"}
        for name, item in _declared_files(actual, "input declarations").items()
    }
    if actual_by_name != expected_by_name:
        raise ControlMalformedResponse(
            "artifact create receipt differs from the requested declarations"
        )
    if create is not None:
        for key, value in (
            ("interface", create.get("interface")),
            ("output_limits", create.get("output_limits")),
            ("timeout_seconds", create.get("timeout_seconds")),
        ):
            if job.get(key) != value:
                raise ControlMalformedResponse(
                    f"artifact create receipt differs from the requested {key}"
                )


def _declared_files(value: object, label: str) -> dict[str, dict[str, object]]:
    if not isinstance(value, (list, tuple)):
        raise ControlMalformedResponse(f"artifact job {label} are invalid")
    result: dict[str, dict[str, object]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            raise ControlMalformedResponse(f"artifact job {label} are invalid")
        name = item.get("name")
        if not isinstance(name, str) or name in result:
            raise ControlMalformedResponse(f"artifact job {label} have invalid names")
        result[name] = dict(item)
    return result


def _output_directory(path: Path, *, create: bool = False) -> Path:
    """The existing output directory; with ``create``, made first (``mkdir -p``).

    A local filesystem problem is the operator's to fix: it is named as such and
    never reported as the control API being unavailable or retried.
    """
    try:
        if create:
            path.mkdir(parents=True, exist_ok=True)
        metadata = path.lstat()
    except OSError as error:
        raise local_filesystem_error(path, error) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise LocalFilesystemError(
            "artifact output path must be an existing non-symlink directory"
        )
    try:
        return path.resolve(strict=True)
    except OSError as error:
        raise local_filesystem_error(path, error) from error


def _transfer_client(client: ArtifactJobClient) -> ArtifactJobClient:
    if not callable(getattr(client, "upload_file", None)) or not callable(
        getattr(client, "download_file", None)
    ):
        raise TransferUnavailable
    return client


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ControlMalformedResponse(f"{label} are invalid")
    return value


def _integer(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ControlMalformedResponse(f"{label} is invalid")
    return value


def _json_mode(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "global_json", False) or getattr(args, "json", False))


def _uuid_argument(value: str) -> str:
    try:
        normalized = str(uuid.UUID(value))
    except ValueError:
        raise argparse.ArgumentTypeError(
            "artifact job selector must be a UUID"
        ) from None
    if _UUID.fullmatch(normalized) is None:
        raise argparse.ArgumentTypeError(
            "artifact job selector must be a supported UUID"
        )
    return normalized


def _stable_request_key(
    args: argparse.Namespace,
    factory: Callable[[], str],
    resolver: Callable[[argparse.Namespace, Callable[[], str]], str],
) -> str:
    value = resolver(args, factory)
    if _UUID.fullmatch(value) is None:
        raise ValueError("request key must be a version 1–5 UUID")
    return value


def _transient_failure(error: BaseException) -> bool:
    """A pure read repeats only after a fault the next try can differ on."""
    if isinstance(error, ControlHTTPError):
        return error.status_code >= 500 or error.status_code in {408, 429}
    return _may_have_completed(error)


def _may_have_completed(error: BaseException) -> bool:
    if isinstance(error, ControlHTTPError):
        return error.status_code not in {401, 403}
    if isinstance(error, (ControlMalformedResponse, ControlResponseTooLarge)):
        status = error.context.http_status if error.context is not None else None
        return status not in {401, 403}
    return isinstance(error, (ControlClientError, OSError))


def _recover[T](
    client: ArtifactJobClient,
    send: Callable[[float], T],
    observe: Callable[[float], T | None],
    *,
    retry_if: Callable[[BaseException], bool] = _may_have_completed,
) -> T:
    """Reconcile an unknown effect, then replay only identical authorized intent.

    There is one total deadline and at most three sends and six observations.
    Unknown reads never become state-based admission decisions.
    """
    timeout = client.request_timeout_seconds
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("request timeout must be finite and positive")
    deadline = time.monotonic() + 3 * timeout
    last: BaseException = ControlTransportError("artifact request outcome is unknown")

    def remaining() -> float:
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise ControlTransportError(
                "artifact request observation deadline reached"
            ) from last
        return min(timeout, budget)

    for attempt in range(_RECOVERY_ATTEMPTS):
        try:
            return send(remaining())
        except (ControlClientError, OSError) as error:
            if not retry_if(error):
                raise
            last = error
        for _ in range(2):
            try:
                observed = observe(remaining())
                if observed is not None:
                    return observed
                break
            except ControlNotFound:
                break
            except (ControlClientError, OSError) as error:
                if not retry_if(error):
                    raise
                last = error
        delay = min(0.1 * 2**attempt, max(0.0, deadline - time.monotonic()))
        retry_after = getattr(last, "retry_after_seconds", None)
        if retry_after is not None:
            budget = max(0.0, deadline - time.monotonic())
            delay = max(delay, budget if retry_after >= budget else float(retry_after))
        if attempt + 1 < _RECOVERY_ATTEMPTS and delay > 0:
            time.sleep(delay)
    if isinstance(last, ControlHTTPError):
        raise last
    raise ControlTransportError(
        "artifact request outcome remains unknown after bounded reconciliation"
    ) from last


__all__ = ["add_artifact_job_commands", "run_artifact_job"]
