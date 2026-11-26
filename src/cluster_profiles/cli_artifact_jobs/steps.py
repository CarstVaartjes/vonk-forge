"""The step commands: create, upload, submit and cancel one artifact job."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import uuid
from collections.abc import Callable, Mapping
from typing import NamedTuple, cast

from cluster_profiles.cli_artifact_jobs.client import (
    ArtifactJobClient,
    _recover,
    _transient_failure,
)
from cluster_profiles.cli_artifact_jobs.contract import (
    _binding_from_flags,
    _capabilities,
)
from cluster_profiles.cli_artifact_jobs.inputs import (
    _canonical_create,
    _deliver_inputs,
    _prepare_inputs,
    _read_binding,
)
from cluster_profiles.cli_artifact_jobs.responses import _create_receipt, _job
from cluster_profiles.cli_artifact_jobs.values import _json_mode, _stable_request_key
from cluster_profiles.cli_select import SelectorError
from cluster_profiles.cli_states import ENDED_WITHOUT_RESULT, preparation
from cluster_profiles.cli_states_generated import RUN_RUNNING
from cluster_profiles.control_client.errors import (
    ControlMalformedResponse,
    ControlNotFound,
)

_RUN_KEY_NAMESPACE = uuid.UUID("7d0c9a64-3f0e-5b1a-8c52-1f0b6a9e2d41")


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
