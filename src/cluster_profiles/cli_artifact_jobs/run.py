"""Dispatch of the ``job`` commands, and the one-command ``job run``."""

from __future__ import annotations

import argparse
import shlex
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from cluster_profiles.cli_artifact_jobs.client import (
    ArtifactJobClient,
    _recover,
    _transient_failure,
)
from cluster_profiles.cli_artifact_jobs.contract import _contract
from cluster_profiles.cli_artifact_jobs.outputs import _download
from cluster_profiles.cli_artifact_jobs.responses import _job, _same_job
from cluster_profiles.cli_artifact_jobs.steps import (
    _RUN_KEY_NAMESPACE,
    _cancel,
    _create,
    _resolve_run,
    _submit,
    _upload_existing,
)
from cluster_profiles.cli_artifact_jobs.values import _integer, _json_mode
from cluster_profiles.cli_json_contract import Word
from cluster_profiles.cli_states_generated import (
    NEEDS_OPERATOR,
    SUCCEEDED,
    TERMINAL_LIFECYCLE_STATES,
)
from cluster_profiles.control_client.errors import ControlMalformedResponse

_JOB_TERMINAL_STATES = TERMINAL_LIFECYCLE_STATES | {NEEDS_OPERATOR}


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
