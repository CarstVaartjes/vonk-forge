"""The ``job`` command group's argument parser."""

from __future__ import annotations

import argparse
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cluster_profiles.cli_artifact_jobs.values import _UUID
from cluster_profiles.cli_json_contract import Word


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
