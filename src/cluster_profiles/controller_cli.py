"""Command tree that mirrors the browser controller's operator workflows."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import stat
import urllib.parse
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Protocol

from .control_client import ControlClientError

PUBLIC_CAPABILITIES = (
    "chat",
    "reasoning",
    "vision",
    "image-generation",
    "image-editing",
    "video",
    "audio",
    "3d",
)
PUBLIC_READINESS = (
    "executable",
    "integration-required",
    "not-executable",
    "not-declared",
)
PUBLIC_QUALIFICATIONS = ("cataloged", "candidate")
PUBLIC_LOCAL_STATES = (
    "not-imported",
    "update-available",
    "current",
    "needs-review",
)
PUBLIC_MODEL_TYPES = ("language", "vision", "image", "video", "audio", "3d")
PUBLIC_SORTS = ("catalog", "model", "sparks", "download")
ARTIFACT_JOB_INTERFACES = (
    "audio-job",
    "video-job",
    "image-job",
    "mesh-job",
    "artifact-job",
)
FLEET_HEALTH = ("live", "delayed", "stale", "offline")
TELEMETRY_RANGES: dict[str, tuple[timedelta, str, int]] = {
    "1h": (timedelta(hours=1), "minute", 60),
    "24h": (timedelta(hours=24), "minute", 1_440),
    "7d": (timedelta(days=7), "fifteen-minute", 672),
    "31d": (timedelta(days=31), "fifteen-minute", 2_976),
}
ACTIVITY_STATUSES = (
    "recorded",
    "in_progress",
    "attention",
    "unsuccessful",
    "unknown",
)
ACTIVITY_SORTS = ("recent", "attention")
RECIPE_PRESETS = ("custom", "vllm", "diffusers")
_MAX_PAGES = 100
_MAX_ARTIFACT_JOB_INPUT_FILES = 32
_MAX_ARTIFACT_JOB_INPUT_FILE_BYTES = 512 * 1024**2
_MAX_ARTIFACT_JOB_INPUT_TOTAL_BYTES = 1024**3
_MAX_ARTIFACT_JOB_OUTPUT_FILE_BYTES = 1024**3
_MAX_ARTIFACT_JOB_OUTPUT_TOTAL_BYTES = 2 * 1024**3
_ARTIFACT_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ARTIFACT_INPUT_SLOT = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")
_ARTIFACT_MEDIA_TYPE = re.compile(
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/"
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}\Z"
)
_LOWER_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_RESERVED_ARTIFACT_INPUT_NAMES = frozenset({"manifest.json"})
_ACTION_LABELS = {
    "agent.enrollment.grant.create": "Created enrollment grant",
    "agent.enrollment.submit.approved": "Approved Spark enrollment",
    "agent.enrollment.submit.rejected": "Rejected Spark enrollment",
    "agent.enrollment.submit.uncertain": "Spark enrollment needs review",
    "agent.node.revoke": "Revoked Spark access",
    "authority.change.submit": "Submitted authority change",
    "auth.login.failed": "Sign-in failed",
    "auth.login.succeeded": "Signed in",
    "auth.login.throttled": "Sign-in rate limited",
    "auth.logout": "Signed out",
    "catalog.entity.create": "Created catalog item",
    "catalog.entity.resolve": "Resolved catalog item",
    "catalog.entity.revise": "Revised catalog item",
    "catalog.global.import": "Imported public catalog item",
    "catalog.publication.export": "Exported catalog publication",
    "catalog.recipe.create": "Created recipe",
    "catalog.recipe.fork": "Forked recipe",
    "catalog.recipe.resolve": "Resolved recipe",
    "catalog.recipe.update": "Updated recipe",
    "catalog.recipe_library.import": "Imported recipe library",
    "catalog.source_bundle.upload": "Uploaded source bundle",
    "catalog.test_report.attach": "Attached validation report",
    "catalog.workload_run.import": "Imported workload recipe",
    "catalog.workload_run.resolve": "Resolved workload recipe",
    "fleet.revoke": "Revoked Spark access",
    "job.resume": "Resumed operation",
    "recipe.build": "Built recipe image",
    "recipe.image.distribute": "Distributed recipe image",
    "recipe.install": "Installed recipe",
    "recipe.mapping.create": "Created recipe placement",
    "recipe.retry": "Retried recipe operation",
    "recipe.start": "Started recipe",
    "recipe.stop": "Stopped recipe",
    "recipe.uninstall": "Uninstalled recipe",
}
_CATEGORY_LABELS = {
    "agent": "Sparks",
    "auth": "Authentication",
    "authority": "Authority",
    "catalog": "Catalog",
    "fleet": "Fleet",
    "job": "Operations",
    "library": "Library",
    "recipe": "Recipes",
    "operation": "Operations",
}
_OPERATION_STATE_LABELS = {
    "cancelled": "Cancelled",
    "compensated": "Recovered",
    "compensating": "Recovering",
    "completed": "Completed",
    "failed": "Failed",
    "pending": "Pending",
    "planned": "Planned",
    "queued": "Queued",
    "running": "Running",
    "succeeded": "Completed",
    "uncertain": "Needs review",
    "waiting-for-operator": "Waiting for operator",
}
_OPERATION_ACTIVITY_STATES = {
    "cancelled": "unsuccessful",
    "canceled": "unsuccessful",
    "error": "unsuccessful",
    "expired": "unsuccessful",
    "failed": "unsuccessful",
    "uncertain": "attention",
    "waiting-for-operator": "attention",
    "compensating": "in_progress",
    "pending": "in_progress",
    "planned": "in_progress",
    "queued": "in_progress",
    "running": "in_progress",
    "starting": "in_progress",
    "stopping": "in_progress",
    "compensated": "recorded",
    "completed": "recorded",
    "succeeded": "recorded",
}


class ControllerClient(Protocol):
    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        extra_headers: Mapping[str, str] | None = None,
        query: Mapping[str, object] | None = None,
    ) -> dict[str, object]: ...

    def upload_file(
        self,
        path: str,
        source: Path,
        *,
        media_type: str,
        expected_sha256: str,
        expected_size: int,
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
    ) -> dict[str, object]: ...


def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit stable JSON output")


def _subcommands(parser: argparse.ArgumentParser, name: str):
    return parser.add_subparsers(dest=name, required=True, parser_class=type(parser))


def _apply(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the mutation; without this flag only the exact request is shown",
    )


def _paging(parser: argparse.ArgumentParser, *, default: int) -> None:
    parser.add_argument("--cursor")
    parser.add_argument("--limit", type=int, choices=range(1, 101), default=default)
    parser.add_argument(
        "--all",
        action="store_true",
        help="Follow bounded continuation cursors until all available pages are loaded",
    )


def _public_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--search", default="")
    parser.add_argument("--model-type", choices=PUBLIC_MODEL_TYPES)
    parser.add_argument("--model")
    parser.add_argument("--source-owner")
    parser.add_argument("--repository")
    parser.add_argument("--sparks", choices=("1", "2", "3", "4+"))
    parser.add_argument("--runtime")
    parser.add_argument("--precision")
    parser.add_argument("--topology")
    parser.add_argument("--qualification", choices=PUBLIC_QUALIFICATIONS)
    parser.add_argument("--readiness", choices=PUBLIC_READINESS)
    parser.add_argument("--local", choices=PUBLIC_LOCAL_STATES)
    parser.add_argument(
        "--capability", action="append", choices=PUBLIC_CAPABILITIES, default=[]
    )


def _request_key(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--request-key",
        help="Idempotency UUID (generated automatically when omitted)",
    )


def _action_pair(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    add_shared: Callable[[argparse.ArgumentParser], None],
    add_apply: Callable[[argparse.ArgumentParser], None],
) -> None:
    action = commands.add_parser(name)
    variants = _subcommands(action, f"{name}_command")
    preview = variants.add_parser("preview")
    add_shared(preview)
    _add_json(preview)
    apply = variants.add_parser("apply")
    add_shared(apply)
    add_apply(apply)
    _request_key(apply)
    _apply(apply)
    _add_json(apply)


def add_controller_commands(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Add the web-controller-equivalent command hierarchy."""
    fleet = commands.add_parser("fleet", help="Fleet nodes, health, and enrollment")
    fleet_commands = _subcommands(fleet, "fleet_command")

    fleet_list = fleet_commands.add_parser("list", help="List the visual Fleet")
    fleet_list.add_argument("--search", default="")
    fleet_list.add_argument(
        "--health", action="append", choices=FLEET_HEALTH, default=[]
    )
    fleet_list.add_argument("--warnings-only", action="store_true")
    fleet_list.add_argument(
        "--sort", choices=("attention", "name"), default="attention"
    )
    _add_json(fleet_list)

    fleet_node = fleet_commands.add_parser("show", help="Show one Fleet node")
    fleet_node.add_argument("node_id")
    _add_json(fleet_node)

    telemetry = fleet_commands.add_parser(
        "telemetry", help="Show node telemetry history"
    )
    telemetry.add_argument("node_id")
    telemetry_time = telemetry.add_mutually_exclusive_group()
    telemetry_time.add_argument(
        "--range", choices=tuple(TELEMETRY_RANGES), default="1h"
    )
    telemetry_time.add_argument("--start")
    telemetry.add_argument("--end")
    telemetry.add_argument("--resolution", choices=("raw", "minute", "fifteen-minute"))
    telemetry.add_argument("--maximum-points", type=int, choices=range(1, 3001))
    _add_json(telemetry)

    profile = fleet_commands.add_parser("profile", help="Rename a Fleet node")
    profile.add_argument("node_id")
    profile.add_argument("--display-name", required=True)
    _apply(profile)
    _add_json(profile)

    agents = fleet_commands.add_parser("agents", help="List registered agents")
    _add_json(agents)
    enrollments = fleet_commands.add_parser(
        "enrollments", help="List enrollment attempts"
    )
    _paging(enrollments, default=100)
    enrollments.add_argument("--state")
    _add_json(enrollments)
    enroll = fleet_commands.add_parser("enroll", help="Create a one-time Spark grant")
    enroll.add_argument("--ttl-seconds", type=int, default=900)
    _apply(enroll)
    _add_json(enroll)
    reenroll = fleet_commands.add_parser(
        "re-enroll", help="Create a one-time Spark certificate replacement grant"
    )
    reenroll.add_argument("node_id", nargs="?")
    reenroll.add_argument("--ttl-seconds", type=int, default=900)
    _apply(reenroll)
    _add_json(reenroll)
    revoke = fleet_commands.add_parser("revoke", help="Revoke a Spark identity")
    revoke.add_argument("node_id")
    _apply(revoke)
    _add_json(revoke)

    upgrade = fleet_commands.add_parser(
        "upgrade", help="Preview or apply a controller-managed Spark agent rollout"
    )
    upgrade_commands = _subcommands(upgrade, "upgrade_command")
    upgrade_candidate = upgrade_commands.add_parser(
        "candidate", help="Show the current signed agent package"
    )
    _add_json(upgrade_candidate)
    for variant in ("preview", "apply"):
        upgrade_variant = upgrade_commands.add_parser(variant)
        upgrade_variant.add_argument(
            "--node-id",
            action="append",
            default=[],
            help="Target one or more Sparks; omit to target every eligible Spark",
        )
        upgrade_variant.add_argument(
            "--strategy",
            choices=("one-at-a-time", "all-at-once"),
            default="one-at-a-time",
        )
        if variant == "apply":
            upgrade_variant.add_argument("--plan-digest", required=True)
            _apply(upgrade_variant)
        _add_json(upgrade_variant)

    library = commands.add_parser(
        "library", help="Browse and operate the recipe Library"
    )
    library_commands = _subcommands(library, "library_command")
    library_list = library_commands.add_parser(
        "list", help="List local models and recipes"
    )
    _paging(library_list, default=100)
    library_list.add_argument("--search", default="")
    _add_json(library_list)
    library_show = library_commands.add_parser("show", help="Show a local recipe")
    library_show.add_argument("recipe_id")
    _add_json(library_show)
    library_compare = library_commands.add_parser(
        "compare", help="Compare two or three local recipes"
    )
    library_compare.add_argument("recipe_id", nargs="+", metavar="RECIPE_ID")
    _add_json(library_compare)

    public = library_commands.add_parser(
        "public", help="Browse and import the public catalog"
    )
    public_commands = _subcommands(public, "public_command")
    public_list = public_commands.add_parser("list", help="List public recipes")
    _public_filters(public_list)
    public_list.add_argument("--sort", choices=PUBLIC_SORTS, default="catalog")
    _add_json(public_list)
    public_facets = public_commands.add_parser(
        "facets", help="List the currently available web catalog filter options"
    )
    _public_filters(public_facets)
    _add_json(public_facets)
    public_compare = public_commands.add_parser(
        "compare", help="Compare two or three public recipes"
    )
    public_compare.add_argument("uri", nargs="+", metavar="URI")
    _add_json(public_compare)
    public_preview = public_commands.add_parser(
        "preview", help="Review an immutable public recipe"
    )
    public_preview.add_argument("uri")
    _add_json(public_preview)
    public_import = public_commands.add_parser(
        "import", help="Import an immutable public recipe"
    )
    public_import.add_argument("uri")
    public_import.add_argument("--expected-content-sha256", required=True)
    _apply(public_import)
    _add_json(public_import)

    template = library_commands.add_parser(
        "template", help="Print the canonical document for a web builder preset"
    )
    template.add_argument("--preset", choices=RECIPE_PRESETS, default="custom")
    _add_json(template)

    create = library_commands.add_parser("create", help="Create a custom recipe draft")
    create.add_argument("--slug", required=True)
    create.add_argument("--document", type=Path, required=True)
    _apply(create)
    _add_json(create)
    update = library_commands.add_parser("update", help="Update a custom recipe draft")
    update.add_argument("recipe_id")
    update.add_argument("--expected-revision", type=int, required=True)
    update.add_argument("--document", type=Path, required=True)
    _apply(update)
    _add_json(update)
    resolve = library_commands.add_parser("resolve", help="Resolve a recipe draft")
    resolve.add_argument("recipe_id")
    resolve.add_argument("--expected-revision", type=int, required=True)
    _apply(resolve)
    _add_json(resolve)
    fork = library_commands.add_parser("fork", help="Fork an immutable recipe revision")
    fork.add_argument("recipe_id")
    fork.add_argument("--revision", type=int, required=True)
    fork.add_argument("--slug", required=True)
    _apply(fork)
    _add_json(fork)

    def mapping_shared(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--recipe-revision-id", required=True)
        parser.add_argument("--node-id", action="append", required=True)
        parser.add_argument("--parameters", type=Path)

    _action_pair(
        library_commands,
        "map",
        mapping_shared,
        lambda parser: parser.add_argument("--placement-digest", required=True),
    )

    def build_shared(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--recipe-revision-id", required=True)
        parser.add_argument("--builder-node-id", required=True)

    _action_pair(
        library_commands,
        "build",
        build_shared,
        lambda parser: parser.add_argument("--build-input-sha256", required=True),
    )

    def distribute_shared(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--recipe-build-id", required=True)
        parser.add_argument("--mapping-id", required=True)
        parser.add_argument("--mapping-generation", type=int, required=True)

    _action_pair(
        library_commands,
        "distribute",
        distribute_shared,
        lambda parser: parser.add_argument("--plan-digest", required=True),
    )

    def install_shared(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--mapping-id", required=True)
        parser.add_argument("--recipe-build-id", required=True)

    _action_pair(
        library_commands,
        "install",
        install_shared,
        lambda parser: parser.add_argument("--plan-digest", required=True),
    )

    def load_shared(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--installation-id", required=True)
        parser.add_argument("--alias", required=True)

    _action_pair(
        library_commands,
        "load",
        load_shared,
        lambda parser: parser.add_argument("--plan-digest", required=True),
    )

    def stop_shared(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("run_id")

    _action_pair(
        library_commands,
        "stop",
        stop_shared,
        lambda parser: parser.add_argument("--plan-digest", required=True),
    )

    def uninstall_shared(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("installation_id")

    _action_pair(
        library_commands,
        "uninstall",
        uninstall_shared,
        lambda parser: parser.add_argument("--plan-digest", required=True),
    )

    operation = library_commands.add_parser(
        "operation", help="Inspect or retry a recipe operation"
    )
    operation_commands = _subcommands(operation, "operation_command")
    operation_show = operation_commands.add_parser("show")
    operation_show.add_argument("operation_id")
    _add_json(operation_show)
    operation_retry = operation_commands.add_parser("retry")
    operation_retry.add_argument("operation_id")
    _request_key(operation_retry)
    _apply(operation_retry)
    _add_json(operation_retry)
    run = library_commands.add_parser("run", help="Show a recipe run")
    run.add_argument("run_id")
    _add_json(run)

    artifact_job = library_commands.add_parser(
        "job", help="Run and retrieve a bounded artifact-producing recipe job"
    )
    artifact_job_commands = _subcommands(artifact_job, "artifact_job_command")

    job_capabilities = artifact_job_commands.add_parser(
        "capabilities", help="Show controller transfer limits and storage headroom"
    )
    _add_json(job_capabilities)
    list_jobs = artifact_job_commands.add_parser(
        "list", help="List durable jobs for one logical recipe run"
    )
    list_jobs.add_argument("run_id")
    _add_json(list_jobs)

    def activate_job_shared(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--installation-id", required=True)
        parser.add_argument("--alias", required=True)

    _action_pair(
        artifact_job_commands,
        "activate",
        activate_job_shared,
        lambda parser: parser.add_argument("--plan-digest", required=True),
    )

    def add_job_inputs(parser: argparse.ArgumentParser, *, required: bool) -> None:
        parser.add_argument(
            "--input",
            action="append",
            nargs=4,
            default=[],
            required=required,
            metavar=("SLOT", "NAME", "MEDIA_TYPE", "PATH"),
            help="Declare a local input; repeat for up to 32 files",
        )

    def add_job_declaration(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("run_id")
        parser.add_argument(
            "--interface", choices=ARTIFACT_JOB_INTERFACES, required=True
        )
        parser.add_argument("--parameters", type=Path)
        add_job_inputs(parser, required=False)
        parser.add_argument(
            "--output-media-type",
            action="append",
            required=True,
            help="Allowed output MIME type; repeat to allow more than one",
        )
        parser.add_argument("--max-output-files", type=int, default=1)
        parser.add_argument(
            "--max-output-file-bytes",
            type=int,
            default=_MAX_ARTIFACT_JOB_OUTPUT_FILE_BYTES,
        )
        parser.add_argument(
            "--max-output-total-bytes",
            type=int,
            default=_MAX_ARTIFACT_JOB_OUTPUT_FILE_BYTES,
        )
        parser.add_argument("--timeout-seconds", type=int, default=3_600)
        _request_key(parser)
        _apply(parser)
        _add_json(parser)

    create_job = artifact_job_commands.add_parser(
        "create", help="Declare inputs and create a draft job"
    )
    add_job_declaration(create_job)
    launch_job = artifact_job_commands.add_parser(
        "launch", help="Create, upload, finalize, and submit one job"
    )
    add_job_declaration(launch_job)

    upload_job = artifact_job_commands.add_parser(
        "upload", help="Upload declared local inputs to a draft job"
    )
    upload_job.add_argument("job_id")
    add_job_inputs(upload_job, required=True)
    _apply(upload_job)
    _add_json(upload_job)

    for command_name, command_help in (
        ("finalize", "Seal a draft after all declared inputs are uploaded"),
        ("submit", "Submit a finalized job to its Spark"),
    ):
        job_mutation = artifact_job_commands.add_parser(command_name, help=command_help)
        job_mutation.add_argument("job_id")
        _apply(job_mutation)
        _add_json(job_mutation)

    status_job = artifact_job_commands.add_parser("status", help="Show job status")
    status_job.add_argument("job_id")
    _add_json(status_job)
    result_job = artifact_job_commands.add_parser(
        "result", help="Show result metadata for a successful job"
    )
    result_job.add_argument("job_id")
    _add_json(result_job)
    download_job = artifact_job_commands.add_parser(
        "download", help="Verify and atomically download successful outputs"
    )
    download_job.add_argument("job_id")
    download_job.add_argument("--output-directory", type=Path, required=True)
    download_job.add_argument(
        "--sha256",
        action="append",
        default=[],
        help="Download only an exact output digest; repeat to select more than one",
    )
    download_job.add_argument(
        "--overwrite",
        action="store_true",
        help="Atomically replace existing output files",
    )
    _apply(download_job)
    _add_json(download_job)
    cancel_job = artifact_job_commands.add_parser("cancel", help="Cancel a job")
    cancel_job.add_argument("job_id")
    cancel_job.add_argument("--reason", required=True)
    _apply(cancel_job)
    _add_json(cancel_job)

    activity = commands.add_parser("activity", help="Audit history and background jobs")
    activity_commands = _subcommands(activity, "activity_command")
    activity_list = activity_commands.add_parser(
        "list", help="List the combined audit and operation activity timeline"
    )
    activity_list.add_argument("--search", default="")
    activity_list.add_argument("--area")
    activity_list.add_argument("--operator")
    activity_list.add_argument("--status", choices=ACTIVITY_STATUSES)
    activity_list.add_argument("--sort", choices=ACTIVITY_SORTS, default="recent")
    activity_list.add_argument(
        "--all",
        action="store_true",
        help="Load every available operation page in addition to the audit window",
    )
    _add_json(activity_list)
    jobs = activity_commands.add_parser("jobs", help="List jobs")
    _paging(jobs, default=20)
    jobs.add_argument("--status")
    jobs.add_argument("--target")
    _add_json(jobs)
    job = activity_commands.add_parser("job", help="Show a job and its progress")
    job.add_argument("job_id")
    job.add_argument("--operation-cursor")
    job.add_argument("--target-cursor")
    job.add_argument("--limit", type=int, choices=range(1, 101), default=20)
    _add_json(job)
    resume = activity_commands.add_parser("resume", help="Resume a waiting job")
    resume.add_argument("job_id")
    _apply(resume)
    _add_json(resume)


def _quoted(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _read_object(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_048_576:
        raise ValueError("input must be a bounded regular non-symlink JSON file")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"input JSON contains duplicate key: {key}")
            value[key] = item
        return value

    value = json.loads(path.read_bytes(), object_pairs_hook=reject_duplicates)
    if not isinstance(value, dict):
        raise TypeError("input JSON must be an object")
    return value


def _inspect_artifact_input(
    slot: str, name: str, media_type: str, source: Path
) -> tuple[dict[str, object], Path]:
    if _ARTIFACT_INPUT_SLOT.fullmatch(slot) is None:
        raise ValueError(
            "artifact input slot must be 1-32 ASCII letters, digits, underscore, or hyphen and start with a letter"
        )
    if _ARTIFACT_FILE_NAME.fullmatch(name) is None:
        raise ValueError(
            "artifact input name must be 1-128 ASCII letters, digits, dot, underscore, or hyphen"
        )
    if name in _RESERVED_ARTIFACT_INPUT_NAMES:
        raise ValueError(f"artifact input name is reserved: {name}")
    if _ARTIFACT_MEDIA_TYPE.fullmatch(media_type) is None:
        raise ValueError("artifact input media type must be a lowercase MIME type")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOINHERIT", 0)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise ValueError("artifact input cannot be opened safely on this platform")
    descriptor = -1
    try:
        descriptor = os.open(source, flags | no_follow)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("artifact input must be a regular non-symlink file")
        if metadata.st_size > _MAX_ARTIFACT_JOB_INPUT_FILE_BYTES:
            raise ValueError("artifact input exceeds the 512 MiB per-file limit")
        digest = hashlib.sha256()
        observed_size = 0
        while observed_size <= _MAX_ARTIFACT_JOB_INPUT_FILE_BYTES:
            chunk = os.read(
                descriptor,
                min(1024**2, _MAX_ARTIFACT_JOB_INPUT_FILE_BYTES + 1 - observed_size),
            )
            if not chunk:
                break
            observed_size += len(chunk)
            digest.update(chunk)
        if observed_size > _MAX_ARTIFACT_JOB_INPUT_FILE_BYTES:
            raise ValueError("artifact input exceeds the 512 MiB per-file limit")
        if observed_size != metadata.st_size:
            raise ValueError("artifact input changed while it was being inspected")
    except ValueError:
        raise
    except OSError:
        raise ValueError(
            "artifact input must be a readable regular non-symlink file"
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return (
        {
            "slot": slot,
            "name": name,
            "media_type": media_type,
            "size_bytes": observed_size,
            "sha256": digest.hexdigest(),
        },
        source,
    )


def _artifact_inputs(
    values: list[list[str]],
) -> list[tuple[dict[str, object], Path]]:
    if len(values) > _MAX_ARTIFACT_JOB_INPUT_FILES:
        raise ValueError("artifact job accepts at most 32 input files")
    inspected = [
        _inspect_artifact_input(slot, name, media_type, Path(source))
        for slot, name, media_type, source in values
    ]
    names = [str(item[0]["name"]) for item in inspected]
    if len(set(names)) != len(names):
        raise ValueError("artifact input names must be unique")
    if (
        sum(int(item[0]["size_bytes"]) for item in inspected)
        > _MAX_ARTIFACT_JOB_INPUT_TOTAL_BYTES
    ):
        raise ValueError("artifact job inputs exceed the 1 GiB total limit")
    return sorted(inspected, key=lambda item: str(item[0]["name"]).encode())


def _artifact_job_create_payload(
    args: argparse.Namespace,
    inputs: list[tuple[dict[str, object], Path]],
) -> dict[str, object]:
    media_types = sorted(set(args.output_media_type))
    if (
        not media_types
        or len(media_types) > 16
        or any(_ARTIFACT_MEDIA_TYPE.fullmatch(value) is None for value in media_types)
    ):
        raise ValueError("output media types must be 1-16 unique lowercase MIME types")
    if not 1 <= args.max_output_files <= 32:
        raise ValueError("--max-output-files must be between 1 and 32")
    if not 1 <= args.max_output_file_bytes <= _MAX_ARTIFACT_JOB_OUTPUT_FILE_BYTES:
        raise ValueError("--max-output-file-bytes must be between 1 and 1 GiB")
    if not 1 <= args.max_output_total_bytes <= _MAX_ARTIFACT_JOB_OUTPUT_TOTAL_BYTES:
        raise ValueError("--max-output-total-bytes must be between 1 and 2 GiB")
    if args.max_output_file_bytes > args.max_output_total_bytes:
        raise ValueError("per-file output limit cannot exceed the total output limit")
    if not 1 <= args.timeout_seconds <= 3_600:
        raise ValueError("--timeout-seconds must be between 1 and 3600")
    parameters = _read_object(args.parameters) if args.parameters else {}
    if (
        len(json.dumps(parameters, sort_keys=True, separators=(",", ":")).encode())
        > 16 * 1024
    ):
        raise ValueError("artifact job parameters exceed the 16 KiB protocol limit")
    return {
        "interface": args.interface,
        "parameters": parameters,
        "inputs": [item[0] for item in inputs],
        "output_limits": {
            "max_files": args.max_output_files,
            "max_file_bytes": args.max_output_file_bytes,
            "max_total_bytes": args.max_output_total_bytes,
            "allowed_media_types": media_types,
        },
        "timeout_seconds": args.timeout_seconds,
    }


def _merge_patch(base: object, patch: object) -> object:
    if not isinstance(base, Mapping) or not isinstance(patch, Mapping):
        return copy.deepcopy(patch)
    result = copy.deepcopy(dict(base))
    for key, value in patch.items():
        result[str(key)] = (
            _merge_patch(result[key], value) if key in result else copy.deepcopy(value)
        )
    return result


def _preset_data() -> dict[str, object]:
    packaged = resources.files("cluster_profiles").joinpath(
        "resources/custom-recipe-presets.json"
    )
    try:
        value = json.loads(packaged.read_bytes())
    except FileNotFoundError:
        repository_copy = (
            Path(__file__).resolve().parents[2]
            / "control/web/src/pages/custom-recipe-presets.json"
        )
        value = json.loads(repository_copy.read_bytes())
    if not isinstance(value, dict):
        raise TypeError("custom recipe preset data must be a JSON object")
    return value


def _recipe_template(preset: str) -> dict[str, object]:
    data = _preset_data()
    base = data.get("base")
    presets = data.get("presets")
    if not isinstance(base, Mapping) or not isinstance(presets, Mapping):
        raise TypeError("custom recipe preset data is invalid")
    if preset not in presets:
        raise ValueError(f"unknown custom recipe preset: {preset}")
    patch = presets.get(preset)
    if not isinstance(patch, Mapping):
        raise TypeError(f"custom recipe preset is not an object: {preset}")
    merged = _merge_patch(base, patch)
    if not isinstance(merged, dict):
        raise TypeError("custom recipe preset did not produce a document")
    return merged


def _plan_or_request(
    args: argparse.Namespace,
    client: ControllerClient,
    method: str,
    path: str,
    payload: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if args.apply:
        return client.request(method, path, payload)
    return {
        "mode": "plan",
        "apply": False,
        "method": method,
        "path": path,
        "body": dict(payload or {}),
    }


def _query(**values: object) -> dict[str, object]:
    return {key: value for key, value in values.items() if value is not None}


def _node_health(node: Mapping[str, object], now: datetime) -> str:
    connection = node.get("connection")
    if (
        not isinstance(connection, Mapping)
        or connection.get("online_state") != "online"
    ):
        return "offline"
    telemetry = node.get("telemetry")
    sample = telemetry.get("sample") if isinstance(telemetry, Mapping) else None
    observed_at = sample.get("observed_at") if isinstance(sample, Mapping) else None
    if not isinstance(observed_at, str):
        return "stale"
    try:
        observed = datetime.fromisoformat(observed_at)
    except ValueError:
        return "stale"
    seconds = max(0.0, (now - observed.astimezone(UTC)).total_seconds())
    if seconds <= 6:
        return "live"
    if seconds <= 20:
        return "delayed"
    return "stale"


_TECHNICAL_SPARK = re.compile(r"^spk_[0-9a-f]{32}(?:\.|$)", re.IGNORECASE)
_TELEMETRY_WARNING_CODES = {
    "telemetry.missing",
    "telemetry.delayed",
    "telemetry.stale",
}


def _is_technical_spark(value: object) -> bool:
    return isinstance(value, str) and bool(_TECHNICAL_SPARK.match(value.strip()))


def _humanize_name(value: str) -> str:
    return " ".join(
        part[:1].upper() + part[1:]
        for part in re.split(r"[\s._-]+", value.strip())
        if part
    )


def _node_display_name(node: Mapping[str, object]) -> str:
    display = node.get("display_name")
    if (
        isinstance(display, str)
        and display.strip()
        and not _is_technical_spark(display)
    ):
        return display.strip()
    labels = node.get("labels")
    if isinstance(labels, Mapping):
        for key in ("display_name", "name", "spark_name"):
            candidate = labels.get(key)
            if (
                isinstance(candidate, str)
                and candidate.strip()
                and not _is_technical_spark(candidate)
            ):
                return _humanize_name(candidate)
    hostname = node.get("hostname")
    if (
        isinstance(hostname, str)
        and hostname.strip()
        and not _is_technical_spark(hostname)
    ):
        return _humanize_name(hostname.split(".", 1)[0])
    role = labels.get("role") if isinstance(labels, Mapping) else None
    return (
        f"{_humanize_name(role)} Spark"
        if isinstance(role, str) and role
        else "Unnamed Spark"
    )


def _node_secondary_name(node: Mapping[str, object]) -> str | None:
    hostname = node.get("hostname")
    if (
        not isinstance(hostname, str)
        or not hostname.strip()
        or _is_technical_spark(hostname)
    ):
        return None
    return (
        None if hostname.casefold() == _node_display_name(node).casefold() else hostname
    )


def _node_warnings(
    node: Mapping[str, object], now: datetime
) -> list[Mapping[str, object]]:
    raw = node.get("warnings")
    warnings = (
        [item for item in raw if isinstance(item, Mapping)]
        if isinstance(raw, list)
        else []
    )
    telemetry = node.get("telemetry")
    sample = telemetry.get("sample") if isinstance(telemetry, Mapping) else None
    if not isinstance(sample, Mapping):
        return warnings
    insertion = next(
        (
            index
            for index, warning in enumerate(warnings)
            if warning.get("code") in _TELEMETRY_WARNING_CODES
        ),
        len(warnings),
    )
    reconciled = [
        warning
        for warning in warnings
        if warning.get("code") not in _TELEMETRY_WARNING_CODES
    ]
    freshness = _node_health(node, now)
    if freshness in {"delayed", "stale"}:
        reconciled.insert(
            min(insertion, len(reconciled)),
            {
                "code": f"telemetry.{freshness}",
                "detail": f"Telemetry is {freshness}.",
                "severity": "warning",
            },
        )
    return reconciled


def _attention_rank(node: Mapping[str, object], now: datetime) -> int:
    health_rank = {"offline": 4, "stale": 3, "delayed": 2, "live": 0}
    warnings = _node_warnings(node, now)
    errors = sum(warning.get("severity") == "error" for warning in warnings)
    installed = node.get("installed")
    loaded = node.get("loaded")
    degraded = (
        sum(
            not bool(item.get("complete"))
            for item in installed
            if isinstance(item, Mapping)
        )
        if isinstance(installed, list)
        else 0
    )
    degraded += (
        sum(
            not bool(item.get("healthy"))
            for item in loaded
            if isinstance(item, Mapping)
        )
        if isinstance(loaded, list)
        else 0
    )
    return (
        health_rank[_node_health(node, now)] * 1_000
        + errors * 100
        + len(warnings) * 10
        + degraded
    )


def _natural_name_key(node: Mapping[str, object]) -> tuple[object, ...]:
    return tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", _node_display_name(node))
    )


def _filter_fleet(
    payload: dict[str, object], args: argparse.Namespace
) -> dict[str, object]:
    nodes = payload.get("nodes")
    if not isinstance(nodes, list):
        return payload
    now = datetime.now(UTC)
    query = args.search.strip().casefold()
    filtered: list[dict[str, object]] = []
    for item in nodes:
        if not isinstance(item, dict):
            continue
        health = _node_health(item, now)
        if args.health and health not in args.health:
            continue
        warnings = _node_warnings(item, now)
        if args.warnings_only and health == "live" and not warnings:
            continue
        searchable = " ".join(
            value
            for value in (_node_display_name(item), _node_secondary_name(item))
            if value
        ).casefold()
        if query and query not in searchable:
            continue
        item = {
            **item,
            "display_name": _node_display_name(item),
            "operational_state": health,
            "warnings": warnings,
        }
        filtered.append(item)
    if args.sort == "name":
        filtered.sort(key=_natural_name_key)
    else:
        filtered.sort(
            key=lambda node: (
                -_attention_rank(node, now),
                _natural_name_key(node),
            )
        )
    return {**payload, "nodes": filtered, "filtered_count": len(filtered)}


def _contains(value: object, query: str) -> bool:
    if isinstance(value, Mapping):
        return any(_contains(item, query) for item in value.values())
    if isinstance(value, list):
        return any(_contains(item, query) for item in value)
    return query in str(value).casefold()


def _searchable_model(model: Mapping[str, object]) -> str:
    identity = model.get("model")
    if not isinstance(identity, Mapping):
        return ""
    publisher = str(identity.get("publisher", ""))
    slug = str(identity.get("slug", ""))
    digest = str(identity.get("content_sha256", ""))
    return f"{_humanize_name(slug)} {publisher}/{slug}@sha256:{digest} {publisher} {slug}".casefold()


def _searchable_recipe(recipe: Mapping[str, object]) -> str:
    capabilities = recipe.get("capabilities")
    capability_text = (
        " ".join(str(value) for value in capabilities)
        if isinstance(capabilities, list)
        else ""
    )
    return " ".join(
        (
            str(recipe.get("title", "")),
            str(recipe.get("slug", "")),
            str(recipe.get("description", "")),
            str(recipe.get("topology_name", "")),
            capability_text,
        )
    ).casefold()


def _filter_library(payload: dict[str, object], query: str) -> dict[str, object]:
    normalized = query.strip().casefold()
    if not normalized:
        return payload
    models = payload.get("models")
    unlinked = payload.get("unlinked_recipes")
    filtered_models: list[object] = []
    if isinstance(models, list):
        for model in models:
            if not isinstance(model, dict):
                continue
            recipes = model.get("recipes")
            model_matches = normalized in _searchable_model(model)
            matching_recipes = (
                [
                    recipe
                    for recipe in recipes
                    if isinstance(recipe, Mapping)
                    and normalized in _searchable_recipe(recipe)
                ]
                if isinstance(recipes, list)
                else []
            )
            if model_matches:
                filtered_models.append(model)
            elif matching_recipes:
                filtered_models.append({**model, "recipes": matching_recipes})
    filtered_unlinked = (
        [
            recipe
            for recipe in unlinked
            if isinstance(recipe, Mapping) and normalized in _searchable_recipe(recipe)
        ]
        if isinstance(unlinked, list)
        else []
    )
    return {
        **payload,
        "models": filtered_models,
        "unlinked_recipes": filtered_unlinked,
        "filter": normalized,
    }


def _public_match(
    recipe: Mapping[str, object], args: argparse.Namespace, omitted: str | None = None
) -> bool:
    query = args.search.strip().casefold()
    fields = (
        "title",
        "slug",
        "description",
        "model_title",
        "model_slug",
        "source_owner",
        "source_repository",
        "runtime_distribution",
        "precision",
        "capabilities",
        "tags",
    )
    if query and not any(_contains(recipe.get(field), query) for field in fields):
        return False
    capabilities = recipe.get("capabilities")
    capability_set = set(capabilities) if isinstance(capabilities, list) else set()
    if omitted != "modelType":
        model_type = args.model_type
        if model_type == "language" and not capability_set.intersection(
            {"chat", "reasoning"}
        ):
            return False
        if model_type == "vision" and "vision" not in capability_set:
            return False
        if model_type == "image" and not capability_set.intersection(
            {"image-generation", "image-editing"}
        ):
            return False
        if model_type in {"video", "audio", "3d"} and model_type not in capability_set:
            return False
    exact = {
        "model": ("model", args.model),
        "source_owner": ("sourceOwner", args.source_owner),
        "source_repository": ("repository", args.repository),
        "runtime_distribution": ("runtime", args.runtime),
        "precision": ("precision", args.precision),
        "topology_mode": ("topology", args.topology),
        "qualification": ("qualification", args.qualification),
        "execution_readiness": ("readiness", args.readiness),
    }
    for field, (facet, expected) in exact.items():
        if omitted == facet or expected is None:
            continue
        actual = (
            f"{recipe.get('model_publisher')}/{recipe.get('model_slug')}"
            if field == "model"
            else recipe.get(field)
        )
        if actual != expected:
            return False
    node_count = recipe.get("node_count")
    if (
        omitted != "sparks"
        and args.sparks == "4+"
        and (not isinstance(node_count, int) or node_count < 4)
    ):
        return False
    if (
        omitted != "sparks"
        and args.sparks in {"1", "2", "3"}
        and node_count != int(args.sparks)
    ):
        return False
    local = recipe.get("local")
    local_state = local.get("status") if isinstance(local, Mapping) else None
    if (
        omitted != "local"
        and args.local == "needs-review"
        and local_state
        not in {
            "different-revision",
            "local-ahead",
            "conflict",
        }
    ):
        return False
    if (
        omitted != "local"
        and args.local
        and args.local != "needs-review"
        and local_state != args.local
    ):
        return False
    return omitted == "capability" or all(
        capability in capability_set for capability in args.capability
    )


def _filter_public(
    payload: dict[str, object], args: argparse.Namespace
) -> dict[str, object]:
    recipes = payload.get("recipes")
    if not isinstance(recipes, list):
        return payload
    filtered = [
        recipe
        for recipe in recipes
        if isinstance(recipe, dict) and _public_match(recipe, args)
    ]
    if args.sort == "model":
        filtered.sort(
            key=lambda recipe: (
                str(recipe.get("model_title", "")).casefold(),
                str(recipe.get("title", "")).casefold(),
            )
        )
    elif args.sort == "sparks":
        filtered.sort(
            key=lambda recipe: (
                int(recipe.get("node_count", 0)),
                str(recipe.get("title", "")).casefold(),
            )
        )
    elif args.sort == "download":
        filtered.sort(
            key=lambda recipe: (
                int(recipe.get("expected_download_bytes", 0)),
                str(recipe.get("title", "")).casefold(),
            )
        )
    return {**payload, "recipes": filtered, "filtered_count": len(filtered)}


def _model_type_matches(recipe: Mapping[str, object], model_type: str) -> bool:
    capabilities = recipe.get("capabilities")
    values = set(capabilities) if isinstance(capabilities, list) else set()
    if model_type == "language":
        return bool(values.intersection({"chat", "reasoning"}))
    if model_type == "image":
        return bool(values.intersection({"image-generation", "image-editing"}))
    return model_type in values


def _facet_values(
    recipes: list[Mapping[str, object]],
    args: argparse.Namespace,
    facet: str,
    values: list[str],
    predicate: Callable[[Mapping[str, object], str], bool],
) -> list[dict[str, object]]:
    return [
        {
            "value": value,
            "count": sum(
                _public_match(recipe, args, facet) and predicate(recipe, value)
                for recipe in recipes
            ),
        }
        for value in values
    ]


def _public_facets(
    payload: dict[str, object], args: argparse.Namespace
) -> dict[str, object]:
    raw = payload.get("recipes")
    recipes = (
        [recipe for recipe in raw if isinstance(recipe, Mapping)]
        if isinstance(raw, list)
        else []
    )

    def unique(field: str) -> list[str]:
        return sorted(
            {
                value
                for recipe in recipes
                if isinstance((value := recipe.get(field)), str) and value
            },
            key=str.casefold,
        )

    models = sorted(
        {
            f"{recipe.get('model_publisher')}/{recipe.get('model_slug')}"
            for recipe in recipes
            if recipe.get("model_publisher")
            and recipe.get("model_slug")
            and (
                args.model_type is None or _model_type_matches(recipe, args.model_type)
            )
        },
        key=str.casefold,
    )
    capabilities = []
    for capability in PUBLIC_CAPABILITIES:
        selected_others = [value for value in args.capability if value != capability]
        capabilities.append(
            {
                "value": capability,
                "count": sum(
                    _public_match(recipe, args, "capability")
                    and all(
                        selected in set(recipe.get("capabilities", []))
                        for selected in selected_others
                    )
                    and capability in set(recipe.get("capabilities", []))
                    for recipe in recipes
                ),
            }
        )
    model_type_args = argparse.Namespace(**{**vars(args), "model": None})
    facets = {
        "model_type": _facet_values(
            recipes,
            model_type_args,
            "modelType",
            list(PUBLIC_MODEL_TYPES),
            _model_type_matches,
        ),
        "model": _facet_values(
            recipes,
            args,
            "model",
            models,
            lambda recipe, value: (
                f"{recipe.get('model_publisher')}/{recipe.get('model_slug')}" == value
            ),
        ),
        "source_owner": _facet_values(
            recipes,
            args,
            "sourceOwner",
            unique("source_owner"),
            lambda recipe, value: recipe.get("source_owner") == value,
        ),
        "repository": _facet_values(
            recipes,
            args,
            "repository",
            unique("source_repository"),
            lambda recipe, value: recipe.get("source_repository") == value,
        ),
        "sparks": _facet_values(
            recipes,
            args,
            "sparks",
            ["1", "2", "3", "4+"],
            lambda recipe, value: (
                isinstance(recipe.get("node_count"), int)
                and (
                    recipe["node_count"] >= 4
                    if value == "4+"
                    else recipe["node_count"] == int(value)
                )
            ),
        ),
        "runtime": _facet_values(
            recipes,
            args,
            "runtime",
            unique("runtime_distribution"),
            lambda recipe, value: recipe.get("runtime_distribution") == value,
        ),
        "precision": _facet_values(
            recipes,
            args,
            "precision",
            unique("precision"),
            lambda recipe, value: recipe.get("precision") == value,
        ),
        "topology": _facet_values(
            recipes,
            args,
            "topology",
            unique("topology_mode"),
            lambda recipe, value: recipe.get("topology_mode") == value,
        ),
        "qualification": _facet_values(
            recipes,
            args,
            "qualification",
            list(PUBLIC_QUALIFICATIONS),
            lambda recipe, value: recipe.get("qualification") == value,
        ),
        "readiness": _facet_values(
            recipes,
            args,
            "readiness",
            list(PUBLIC_READINESS),
            lambda recipe, value: recipe.get("execution_readiness") == value,
        ),
        "local": _facet_values(
            recipes,
            args,
            "local",
            list(PUBLIC_LOCAL_STATES),
            lambda recipe, value: (
                isinstance(recipe.get("local"), Mapping)
                and (
                    recipe["local"].get("status")
                    in {"different-revision", "local-ahead", "conflict"}
                    if value == "needs-review"
                    else recipe["local"].get("status") == value
                )
            ),
        ),
        "capability": capabilities,
    }
    return {
        "repository": payload.get("repository"),
        "commit": payload.get("commit"),
        "matching_count": sum(_public_match(recipe, args) for recipe in recipes),
        "facets": facets,
    }


def _telemetry_query(args: argparse.Namespace) -> dict[str, object]:
    end = datetime.fromisoformat(args.end) if args.end else datetime.now(UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    if args.start:
        start = datetime.fromisoformat(args.start)
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        resolution = args.resolution or "minute"
        maximum = args.maximum_points or 1_500
    else:
        duration, default_resolution, default_maximum = TELEMETRY_RANGES[args.range]
        start = end - duration
        resolution = args.resolution or default_resolution
        maximum = args.maximum_points or default_maximum
    return {
        "start": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "end": end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "resolution": resolution,
        "maximum_points": maximum,
    }


def _request_key_value(args: argparse.Namespace, factory: Callable[[], str]) -> str:
    return args.request_key or factory()


def _compare_values(values: list[str], label: str) -> list[str]:
    unique = list(dict.fromkeys(values))
    if len(unique) != len(values):
        raise ValueError(f"{label} comparison values must be unique")
    if not 2 <= len(unique) <= 3:
        raise ValueError(f"compare requires two or three {label} values")
    return unique


def _model_identity_key(model: Mapping[str, object]) -> tuple[object, object, object]:
    identity = model.get("model")
    if not isinstance(identity, Mapping):
        return (None, None, None)
    return (
        identity.get("publisher"),
        identity.get("slug"),
        identity.get("content_sha256"),
    )


def _load_library_snapshot(
    client: ControllerClient, args: argparse.Namespace
) -> dict[str, object]:
    cursor = args.cursor
    seen: set[str] = set()
    pages: list[dict[str, object]] = []
    for _page_number in range(_MAX_PAGES):
        page = client.request(
            "GET",
            "/api/v1/library",
            query=_query(cursor=cursor, limit=args.limit),
        )
        pages.append(page)
        next_cursor = page.get("next_cursor")
        if not args.all or not isinstance(next_cursor, str) or not next_cursor:
            break
        if next_cursor in seen:
            raise ValueError("Library continuation cursor repeated")
        seen.add(next_cursor)
        cursor = next_cursor
    else:
        raise ValueError("Library pagination exceeded the safety limit")

    result = dict(pages[0])
    merged_models: dict[tuple[object, object, object], dict[str, object]] = {}
    unlinked: dict[object, Mapping[str, object]] = {}
    for page in pages:
        models = page.get("models")
        if isinstance(models, list):
            for model in models:
                if not isinstance(model, Mapping):
                    continue
                key = _model_identity_key(model)
                current = merged_models.setdefault(key, dict(model))
                current_recipes = current.setdefault("recipes", [])
                page_recipes = model.get("recipes")
                if isinstance(current_recipes, list) and isinstance(page_recipes, list):
                    known = {
                        recipe.get("recipe_id")
                        for recipe in current_recipes
                        if isinstance(recipe, Mapping)
                    }
                    current_recipes.extend(
                        recipe
                        for recipe in page_recipes
                        if isinstance(recipe, Mapping)
                        and recipe.get("recipe_id") not in known
                    )
        page_unlinked = page.get("unlinked_recipes")
        if isinstance(page_unlinked, list):
            for recipe in page_unlinked:
                if isinstance(recipe, Mapping):
                    unlinked.setdefault(recipe.get("recipe_id"), recipe)
    result["models"] = list(merged_models.values())
    result["unlinked_recipes"] = list(unlinked.values())
    result["next_cursor"] = pages[-1].get("next_cursor")
    result["loaded_pages"] = len(pages)
    return result


def _run_fleet(args: argparse.Namespace, client: ControllerClient) -> dict[str, object]:
    command = args.fleet_command
    if command in {"list", "show"}:
        payload = client.request("GET", "/api/v1/fleet")
        if command == "list":
            return _filter_fleet(payload, args)
        nodes = payload.get("nodes")
        if isinstance(nodes, list):
            for node in nodes:
                if isinstance(node, dict) and node.get("id") == args.node_id:
                    return node
        raise ValueError(f"Fleet node not found: {args.node_id}")
    if command == "telemetry":
        return client.request(
            "GET",
            f"/api/v1/nodes/{_quoted(args.node_id)}/telemetry",
            query=_telemetry_query(args),
        )
    if command == "profile":
        display_name = args.display_name.strip()
        if (
            not display_name
            or len(display_name) > 80
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in display_name
            )
        ):
            raise ValueError("--display-name must be 1-80 printable characters")
        return _plan_or_request(
            args,
            client,
            "PATCH",
            f"/api/v1/nodes/{_quoted(args.node_id)}/profile",
            {"display_name": display_name},
        )
    if command == "agents":
        return client.request("GET", "/api/v1/agents")
    if command == "enrollments":
        return _load_enrollments(client, args)
    if command in {"enroll", "re-enroll"}:
        if args.ttl_seconds < 1 or args.ttl_seconds > 900:
            raise ValueError("--ttl-seconds must be between 1 and 900")
        if (
            command == "re-enroll"
            and args.node_id
            and not re.fullmatch(r"spk_[0-9a-f]{32}", args.node_id)
        ):
            raise ValueError("re-enrollment node ID must match spk_<32 lowercase hex>")
        payload: dict[str, object] = {
            "ttl_seconds": args.ttl_seconds,
            "purpose": "re-enroll" if command == "re-enroll" else "new-node",
        }
        if command == "re-enroll" and args.node_id:
            payload["node_id"] = args.node_id
        return _plan_or_request(
            args,
            client,
            "POST",
            "/api/v1/agents/enrollments/grants",
            payload,
        )
    if command == "upgrade":
        if args.upgrade_command == "candidate":
            return client.request("GET", "/api/v1/agents/upgrades/candidate")
        for node_id in args.node_id:
            if not re.fullmatch(r"spk_[0-9a-f]{32}", node_id):
                raise ValueError("upgrade node ID must match spk_<32 lowercase hex>")
        payload = {"strategy": args.strategy}
        if args.node_id:
            payload["node_ids"] = args.node_id
        if args.upgrade_command == "preview":
            return client.request("POST", "/api/v1/agents/upgrades/preview", payload)
        payload["plan_digest"] = args.plan_digest
        return _plan_or_request(
            args,
            client,
            "POST",
            "/api/v1/agents/upgrades",
            payload,
        )
    return _plan_or_request(
        args, client, "POST", f"/api/v1/agents/nodes/{_quoted(args.node_id)}/revoke"
    )


def _artifact_job_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            value,
        )
        is None
    ):
        raise ValueError("controller returned an invalid artifact job ID")
    return value


def _artifact_capabilities(value: Mapping[str, object]) -> dict[str, object]:
    transport = value.get("transport")
    storage = value.get("storage")
    expected_transport = {
        "max_input_files": _MAX_ARTIFACT_JOB_INPUT_FILES,
        "max_input_file_bytes": _MAX_ARTIFACT_JOB_INPUT_FILE_BYTES,
        "max_input_total_bytes": _MAX_ARTIFACT_JOB_INPUT_TOTAL_BYTES,
        "max_output_files": 32,
        "max_output_file_bytes": _MAX_ARTIFACT_JOB_OUTPUT_FILE_BYTES,
        "max_output_total_bytes": _MAX_ARTIFACT_JOB_OUTPUT_TOTAL_BYTES,
        "max_timeout_seconds": 3_600,
        "reserved_input_names": sorted(_RESERVED_ARTIFACT_INPUT_NAMES),
    }
    if value.get("schema_version") != 1 or not isinstance(transport, Mapping):
        raise ValueError("controller returned invalid artifact-job capabilities")
    if any(
        transport.get(key) != expected for key, expected in expected_transport.items()
    ):
        raise ValueError("controller artifact-job transfer contract is incompatible")
    if not isinstance(storage, Mapping):
        raise TypeError("controller returned invalid artifact storage capabilities")
    maximum = storage.get("max_stored_bytes")
    used = storage.get("used_bytes")
    remaining = storage.get("remaining_bytes")
    if (
        not isinstance(maximum, int)
        or isinstance(maximum, bool)
        or maximum < 1
        or not isinstance(used, int)
        or isinstance(used, bool)
        or not 0 <= used <= maximum
        or not isinstance(remaining, int)
        or isinstance(remaining, bool)
        or remaining != maximum - used
    ):
        raise ValueError("controller returned invalid artifact storage capabilities")
    return dict(value)


def _artifact_storage_preflight(
    capabilities: Mapping[str, object],
    inputs: list[tuple[dict[str, object], Path]],
) -> dict[str, object]:
    storage = capabilities["storage"]
    assert isinstance(storage, Mapping)
    distinct: dict[str, int] = {}
    for declaration, _source in inputs:
        distinct.setdefault(str(declaration["sha256"]), int(declaration["size_bytes"]))
    required = sum(distinct.values())
    remaining = int(storage["remaining_bytes"])
    return {
        "distinct_input_bytes": required,
        "storage_remaining_bytes": remaining,
        "fits_without_server_reuse": required <= remaining,
        "note": (
            "Capacity is sufficient without server-side blob reuse."
            if required <= remaining
            else "Capacity is insufficient unless every excess input blob already exists in the controller CAS."
        ),
    }


def _artifact_output_files(result: Mapping[str, object]) -> list[dict[str, object]]:
    raw = result.get("output_files")
    if not isinstance(raw, list) or not raw:
        raise ValueError("artifact job result contains no downloadable outputs")
    outputs: list[dict[str, object]] = []
    names: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            raise TypeError("artifact job result contains invalid output metadata")
        name = item.get("name")
        media_type = item.get("media_type")
        size = item.get("size_bytes")
        digest = item.get("sha256")
        if (
            not isinstance(name, str)
            or _ARTIFACT_FILE_NAME.fullmatch(name) is None
            or name in names
            or not isinstance(media_type, str)
            or _ARTIFACT_MEDIA_TYPE.fullmatch(media_type) is None
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 <= size <= _MAX_ARTIFACT_JOB_OUTPUT_FILE_BYTES
            or not isinstance(digest, str)
            or _LOWER_SHA256.fullmatch(digest) is None
        ):
            raise ValueError("artifact job result contains invalid output metadata")
        names.add(name)
        outputs.append(dict(item))
    if (
        sum(int(item["size_bytes"]) for item in outputs)
        > _MAX_ARTIFACT_JOB_OUTPUT_TOTAL_BYTES
    ):
        raise ValueError("artifact job result exceeds the CLI output safety limit")
    return outputs


def _run_artifact_job(
    args: argparse.Namespace,
    client: ControllerClient,
    request_id_factory: Callable[[], str],
) -> dict[str, object]:
    command = args.artifact_job_command
    if command == "capabilities":
        return _artifact_capabilities(
            client.request("GET", "/api/v1/artifact-jobs/capabilities")
        )
    if command == "list":
        return client.request(
            "GET",
            f"/api/v1/recipes/runs/{_quoted(args.run_id)}/artifact-jobs",
        )
    if command == "activate":
        apply = args.activate_command == "apply"
        payload: dict[str, object] = {
            "installation_id": args.installation_id,
            "alias": args.alias,
        }
        if not apply:
            return client.request("POST", "/api/v1/recipes/run-plans/preview", payload)
        payload.update(
            plan_digest=args.plan_digest,
            request_key=_request_key_value(args, request_id_factory),
        )
        return _plan_or_request(
            args, client, "POST", "/api/v1/recipes/job-runs", payload
        )
    if command in {"create", "launch"}:
        inputs = _artifact_inputs(args.input)
        payload = _artifact_job_create_payload(args, inputs)
        request_key = _request_key_value(args, request_id_factory)
        if (
            args.request_key
            and re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                request_key,
            )
            is None
        ):
            raise ValueError("--request-key must be a lowercase UUID")
        create_path = f"/api/v1/recipes/runs/{_quoted(args.run_id)}/artifact-jobs"
        if not args.apply:
            steps: list[dict[str, object]] = [
                {
                    "method": "POST",
                    "path": create_path,
                    "request_key": request_key,
                    "body": payload,
                }
            ]
            if command == "launch":
                steps.extend(
                    {
                        "method": "PUT",
                        "path": f"/api/v1/artifact-jobs/<created-job-id>/inputs/{_quoted(str(declaration['name']))}",
                        "source": str(source),
                        "sha256": declaration["sha256"],
                        "size_bytes": declaration["size_bytes"],
                    }
                    for declaration, source in inputs
                )
                steps.extend(
                    [
                        {
                            "method": "POST",
                            "path": "/api/v1/artifact-jobs/<created-job-id>/finalize",
                        },
                        {
                            "method": "POST",
                            "path": "/api/v1/artifact-jobs/<created-job-id>/submit",
                        },
                    ]
                )
            return {"mode": "plan", "steps": steps}
        capabilities = _artifact_capabilities(
            client.request("GET", "/api/v1/artifact-jobs/capabilities")
        )
        storage_preflight = _artifact_storage_preflight(capabilities, inputs)
        created = client.request(
            "POST",
            create_path,
            payload,
            extra_headers={"X-Request-ID": request_key},
        )
        if command == "create":
            return {**created, "storage_preflight": storage_preflight}
        job_id = _artifact_job_id(created.get("id"))
        uploaded: list[dict[str, object]] = []
        for declaration, source in inputs:
            client.upload_file(
                f"/api/v1/artifact-jobs/{_quoted(job_id)}/inputs/{_quoted(str(declaration['name']))}",
                source,
                media_type=str(declaration["media_type"]),
                expected_sha256=str(declaration["sha256"]),
                expected_size=int(declaration["size_bytes"]),
            )
            uploaded.append(declaration)
        client.request("POST", f"/api/v1/artifact-jobs/{_quoted(job_id)}/finalize")
        submitted = client.request(
            "POST", f"/api/v1/artifact-jobs/{_quoted(job_id)}/submit"
        )
        return {
            "job": submitted,
            "uploaded_inputs": uploaded,
            "storage_preflight": storage_preflight,
            "steps_completed": 3 + len(uploaded),
        }
    job_id = _quoted(args.job_id)
    base = f"/api/v1/artifact-jobs/{job_id}"
    if command == "upload":
        inputs = _artifact_inputs(args.input)
        if not args.apply:
            return {
                "mode": "plan",
                "steps": [
                    {
                        "method": "PUT",
                        "path": f"{base}/inputs/{_quoted(str(declaration['name']))}",
                        "source": str(source),
                        **declaration,
                    }
                    for declaration, source in inputs
                ],
            }
        responses = [
            client.upload_file(
                f"{base}/inputs/{_quoted(str(declaration['name']))}",
                source,
                media_type=str(declaration["media_type"]),
                expected_sha256=str(declaration["sha256"]),
                expected_size=int(declaration["size_bytes"]),
            )
            for declaration, source in inputs
        ]
        return {
            "job": responses[-1],
            "uploaded_inputs": [item[0] for item in inputs],
        }
    if command in {"finalize", "submit"}:
        return _plan_or_request(args, client, "POST", f"{base}/{command}")
    if command == "status":
        return client.request("GET", base)
    if command == "result":
        return client.request("GET", f"{base}/result")
    if command == "cancel":
        reason = " ".join(args.reason.split())
        if not reason or len(reason) > 512:
            raise ValueError("--reason must be 1-512 non-whitespace characters")
        return _plan_or_request(
            args, client, "POST", f"{base}/cancel", {"reason": reason}
        )
    result = client.request("GET", f"{base}/result")
    outputs = _artifact_output_files(result)
    requested = list(dict.fromkeys(args.sha256))
    if any(_LOWER_SHA256.fullmatch(value) is None for value in requested):
        raise ValueError("--sha256 must be exactly 64 lowercase hexadecimal characters")
    if requested:
        available = {str(item["sha256"]) for item in outputs}
        missing = [digest for digest in requested if digest not in available]
        if missing:
            raise ValueError(f"artifact output digest not found: {missing[0]}")
        selected = set(requested)
        outputs = [item for item in outputs if item["sha256"] in selected]
    directory = args.output_directory
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("--output-directory must be an existing non-symlink directory")
    plans = [
        {
            "path": f"{base}/results/{item['sha256']}",
            "destination": str(directory / str(item["name"])),
            "media_type": item["media_type"],
            "size_bytes": item["size_bytes"],
            "sha256": item["sha256"],
        }
        for item in outputs
    ]
    if not args.apply:
        return {"mode": "plan", "downloads": plans, "overwrite": args.overwrite}
    downloaded = [
        client.download_file(
            str(plan["path"]),
            Path(str(plan["destination"])),
            media_type=str(plan["media_type"]),
            expected_sha256=str(plan["sha256"]),
            expected_size=int(plan["size_bytes"]),
            overwrite=args.overwrite,
        )
        for plan in plans
    ]
    return {"job_id": args.job_id, "downloads": downloaded}


def _run_library(
    args: argparse.Namespace,
    client: ControllerClient,
    request_id_factory: Callable[[], str],
) -> dict[str, object]:
    command = args.library_command
    if command == "list":
        result = _load_library_snapshot(client, args)
        return _filter_library(result, args.search)
    if command == "show":
        return client.request(
            "GET", f"/api/v1/library/recipes/{_quoted(args.recipe_id)}"
        )
    if command == "compare":
        recipe_ids = _compare_values(args.recipe_id, "recipe")
        return {
            "recipes": [
                client.request("GET", f"/api/v1/library/recipes/{_quoted(recipe_id)}")
                for recipe_id in recipe_ids
            ],
            "compared_count": len(recipe_ids),
        }
    if command == "public":
        if args.public_command in {"list", "facets", "compare"}:
            catalog = client.request("GET", "/api/v1/catalog/public-recipes")
            if args.public_command == "list":
                return _filter_public(catalog, args)
            if args.public_command == "facets":
                return _public_facets(catalog, args)
            uris = _compare_values(args.uri, "recipe URI")
            recipes = catalog.get("recipes")
            indexed = (
                {
                    recipe.get("uri"): recipe
                    for recipe in recipes
                    if isinstance(recipe, Mapping)
                    and isinstance(recipe.get("uri"), str)
                }
                if isinstance(recipes, list)
                else {}
            )
            missing = [uri for uri in uris if uri not in indexed]
            if missing:
                raise ValueError(f"public recipe not found: {missing[0]}")
            return {
                "repository": catalog.get("repository"),
                "commit": catalog.get("commit"),
                "recipes": [indexed[uri] for uri in uris],
                "compared_count": len(uris),
            }
        payload = {"uri": args.uri}
        if args.public_command == "preview":
            return client.request(
                "POST", "/api/v1/catalog/imports/public/preview", payload
            )
        payload["expected_content_sha256"] = args.expected_content_sha256
        return _plan_or_request(
            args, client, "POST", "/api/v1/catalog/imports/public", payload
        )
    if command == "template":
        return _recipe_template(args.preset)
    if command == "create":
        return _plan_or_request(
            args,
            client,
            "POST",
            "/api/v1/catalog/recipes",
            {"slug": args.slug, "document": _read_object(args.document)},
        )
    if command == "update":
        return _plan_or_request(
            args,
            client,
            "PUT",
            f"/api/v1/catalog/recipes/{_quoted(args.recipe_id)}/draft",
            {
                "expected_revision": args.expected_revision,
                "document": _read_object(args.document),
            },
        )
    if command == "resolve":
        return _plan_or_request(
            args,
            client,
            "POST",
            f"/api/v1/catalog/recipes/{_quoted(args.recipe_id)}/resolve",
            {"expected_revision": args.expected_revision},
        )
    if command == "fork":
        return _plan_or_request(
            args,
            client,
            "POST",
            f"/api/v1/catalog/recipes/{_quoted(args.recipe_id)}/fork",
            {"revision": args.revision, "slug": args.slug},
        )
    if command in {
        "map",
        "build",
        "distribute",
        "install",
        "load",
        "stop",
        "uninstall",
    }:
        variant = getattr(args, f"{command}_command")
        apply = variant == "apply"
        if command == "map":
            payload: dict[str, object] = {
                "recipe_revision_id": args.recipe_revision_id,
                "node_ids": args.node_id,
                "parameters": _read_object(args.parameters) if args.parameters else {},
            }
            path = (
                "/api/v1/recipes/mappings"
                if apply
                else "/api/v1/recipes/mapping-plans/preview"
            )
            if apply:
                payload.update(
                    placement_digest=args.placement_digest,
                    request_key=_request_key_value(args, request_id_factory),
                )
        elif command == "build":
            payload = {
                "recipe_revision_id": args.recipe_revision_id,
                "builder_node_id": args.builder_node_id,
            }
            path = (
                "/api/v1/recipes/builds"
                if apply
                else "/api/v1/recipes/build-plans/preview"
            )
            if apply:
                payload.update(
                    build_input_sha256=args.build_input_sha256,
                    request_key=_request_key_value(args, request_id_factory),
                )
        elif command == "distribute":
            payload = {
                "recipe_build_id": args.recipe_build_id,
                "mapping_id": args.mapping_id,
                "mapping_generation": args.mapping_generation,
            }
            path = (
                "/api/v1/recipes/image-distributions"
                if apply
                else "/api/v1/recipes/image-distribution-plans/preview"
            )
            if apply:
                payload.update(
                    plan_digest=args.plan_digest,
                    request_key=_request_key_value(args, request_id_factory),
                )
        elif command == "install":
            payload = {
                "mapping_id": args.mapping_id,
                "recipe_build_id": args.recipe_build_id,
            }
            path = (
                "/api/v1/recipes/installations"
                if apply
                else "/api/v1/recipes/install-plans/preview"
            )
            if apply:
                payload.update(
                    plan_digest=args.plan_digest,
                    request_key=_request_key_value(args, request_id_factory),
                )
        elif command == "load":
            payload = {"installation_id": args.installation_id, "alias": args.alias}
            path = (
                "/api/v1/recipes/runs" if apply else "/api/v1/recipes/run-plans/preview"
            )
            if apply:
                payload.update(
                    plan_digest=args.plan_digest,
                    request_key=_request_key_value(args, request_id_factory),
                )
        elif command == "stop":
            payload = (
                {"run_id": args.run_id}
                if not apply
                else {
                    "plan_digest": args.plan_digest,
                    "request_key": _request_key_value(args, request_id_factory),
                }
            )
            path = (
                "/api/v1/recipes/stop-plans/preview"
                if not apply
                else f"/api/v1/recipes/runs/{_quoted(args.run_id)}/stop"
            )
        else:
            payload = (
                {"installation_id": args.installation_id}
                if not apply
                else {
                    "plan_digest": args.plan_digest,
                    "request_key": _request_key_value(args, request_id_factory),
                }
            )
            path = (
                "/api/v1/recipes/uninstall-plans/preview"
                if not apply
                else f"/api/v1/recipes/installations/{_quoted(args.installation_id)}/uninstall"
            )
        if apply:
            return _plan_or_request(args, client, "POST", path, payload)
        return client.request("POST", path, payload)
    if command == "operation":
        path = f"/api/v1/recipes/operations/{_quoted(args.operation_id)}"
        if args.operation_command == "show":
            return client.request("GET", path)
        return _plan_or_request(
            args,
            client,
            "POST",
            f"{path}/retry",
            {"request_key": _request_key_value(args, request_id_factory)},
        )
    if command == "job":
        return _run_artifact_job(args, client, request_id_factory)
    return client.request("GET", f"/api/v1/recipes/runs/{_quoted(args.run_id)}")


def _activity_title_case(value: str) -> str:
    return " ".join(part.capitalize() for part in re.split(r"[_-]+", value) if part)


def _activity_label(action: str) -> str:
    if action.startswith("operation."):
        parts = action.split(".")[1:]
        state = parts.pop() if parts else "unknown"
        kind = _activity_title_case(" ".join(parts)) or "Operation"
        return f"{kind} · {_OPERATION_STATE_LABELS.get(state, _activity_title_case(state))}"
    if action in _ACTION_LABELS:
        return _ACTION_LABELS[action]
    parts = [part for part in action.split(".") if part]
    useful = parts[1:] if len(parts) > 1 else parts
    label = _activity_title_case(" ".join(useful))
    return label[:1].upper() + label[1:].lower() if label else "Recorded activity"


def _activity_category(event: Mapping[str, object]) -> str:
    action = str(event.get("action", ""))
    category = action.split(".", 1)[0] or "other"
    return _CATEGORY_LABELS.get(category, _activity_title_case(category))


def _activity_status(event: Mapping[str, object]) -> str:
    action = str(event.get("action", ""))
    if action.startswith("operation."):
        return _OPERATION_ACTIVITY_STATES.get(action.rsplit(".", 1)[-1], "unknown")
    if re.search(r"(?:^|\.)(?:failed|rejected|throttled|denied|error)(?:\.|$)", action):
        return "unsuccessful"
    if re.search(r"(?:^|\.)(?:uncertain|warning|conflict|stale)(?:\.|$)", action):
        return "attention"
    return "recorded"


def _event_timestamp(event: Mapping[str, object]) -> float:
    value = event.get("occurred_at")
    if not isinstance(value, str):
        return 0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0


def _load_jobs(
    client: ControllerClient,
    *,
    cursor: str | None,
    limit: int,
    status: str | None,
    target: str | None,
    load_all: bool,
) -> dict[str, object]:
    seen_cursors: set[str] = set()
    seen_jobs: set[object] = set()
    jobs: list[Mapping[str, object]] = []
    last: dict[str, object] = {}
    for _page_number in range(_MAX_PAGES):
        last = client.request(
            "GET",
            "/api/v1/jobs",
            query=_query(cursor=cursor, limit=limit, status=status, target=target),
        )
        raw_jobs = last.get("jobs")
        if isinstance(raw_jobs, list):
            for job in raw_jobs:
                if not isinstance(job, Mapping) or job.get("id") in seen_jobs:
                    continue
                seen_jobs.add(job.get("id"))
                jobs.append(job)
        next_cursor = last.get("next_cursor")
        if not load_all or not isinstance(next_cursor, str) or not next_cursor:
            break
        if next_cursor in seen_cursors:
            raise ValueError("job continuation cursor repeated")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        raise ValueError("job pagination exceeded the safety limit")
    return {
        **last,
        "jobs": jobs,
        "loaded_count": len(jobs),
        "next_cursor": last.get("next_cursor"),
    }


def _load_enrollments(
    client: ControllerClient, args: argparse.Namespace
) -> dict[str, object]:
    cursor = args.cursor
    seen_cursors: set[str] = set()
    seen_enrollments: set[object] = set()
    enrollments: list[Mapping[str, object]] = []
    last: dict[str, object] = {}
    for _page_number in range(_MAX_PAGES):
        last = client.request(
            "GET",
            "/api/v1/agents/enrollments",
            query=_query(cursor=cursor, limit=args.limit, state=args.state),
        )
        raw = last.get("enrollments")
        if isinstance(raw, list):
            for enrollment in raw:
                if (
                    not isinstance(enrollment, Mapping)
                    or enrollment.get("id") in seen_enrollments
                ):
                    continue
                seen_enrollments.add(enrollment.get("id"))
                enrollments.append(enrollment)
        next_cursor = last.get("next_cursor")
        if not args.all or not isinstance(next_cursor, str) or not next_cursor:
            break
        if next_cursor in seen_cursors:
            raise ValueError("enrollment continuation cursor repeated")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        raise ValueError("enrollment pagination exceeded the safety limit")
    return {
        **last,
        "enrollments": enrollments,
        "loaded_count": len(enrollments),
        "next_cursor": last.get("next_cursor"),
    }


def _target_name_lookup(
    fleet: Mapping[str, object] | None, library: Mapping[str, object] | None
) -> dict[object, str]:
    names: dict[object, str] = {}
    nodes = fleet.get("nodes") if isinstance(fleet, Mapping) else None
    if isinstance(nodes, list):
        for node in nodes:
            if isinstance(node, Mapping):
                names[node.get("id")] = _node_display_name(node)
    models = library.get("models") if isinstance(library, Mapping) else None
    unlinked = library.get("unlinked_recipes") if isinstance(library, Mapping) else None
    recipes: list[Mapping[str, object]] = []
    if isinstance(models, list):
        for model in models:
            model_recipes = model.get("recipes") if isinstance(model, Mapping) else None
            if isinstance(model_recipes, list):
                recipes.extend(
                    recipe for recipe in model_recipes if isinstance(recipe, Mapping)
                )
    if isinstance(unlinked, list):
        recipes.extend(recipe for recipe in unlinked if isinstance(recipe, Mapping))
    for recipe in recipes:
        recipe_id = recipe.get("recipe_id")
        raw_title = str(recipe.get("title", "")).strip()
        title = raw_title if raw_title and raw_title != recipe_id else "Unnamed recipe"
        names[recipe_id] = title
        revision = recipe.get("selected_revision")
        if isinstance(revision, Mapping):
            names[revision.get("id")] = (
                f"{title} revision {revision.get('revision_number')}"
            )
        installations = recipe.get("installations")
        if isinstance(installations, list):
            for index, installation in enumerate(installations, 1):
                if isinstance(installation, Mapping):
                    names[installation.get("installation_id")] = (
                        f"{title} installation {index}"
                    )
        runs = recipe.get("runs")
        if isinstance(runs, list):
            for index, run in enumerate(runs, 1):
                if isinstance(run, Mapping):
                    names[run.get("run_id")] = f"{title} run {index}"
    names.pop(None, None)
    return names


def _combined_activity(
    args: argparse.Namespace, client: ControllerClient
) -> dict[str, object]:
    source_errors: dict[str, str] = {}
    audit: dict[str, object] | None = None
    jobs: dict[str, object] | None = None
    try:
        audit = client.request("GET", "/api/v1/audit")
    except (ControlClientError, OSError, TypeError, ValueError) as error:
        source_errors["audit"] = str(error)
    try:
        jobs = _load_jobs(
            client,
            cursor=None,
            limit=20,
            status=None,
            target=None,
            load_all=args.all,
        )
    except (ControlClientError, OSError, TypeError, ValueError) as error:
        source_errors["jobs"] = str(error)
    if audit is None and jobs is None:
        raise ValueError(
            "Unable to load audit history or operations. "
            + " ".join(source_errors.values())
        )
    fleet: dict[str, object] | None = None
    library: dict[str, object] | None = None
    try:
        fleet = client.request("GET", "/api/v1/fleet")
    except (ControlClientError, OSError, TypeError, ValueError) as error:
        source_errors["fleet_names"] = str(error)
    try:
        library = client.request("GET", "/api/v1/library", query={"limit": 100})
    except (ControlClientError, OSError, TypeError, ValueError) as error:
        source_errors["library_names"] = str(error)
    names = _target_name_lookup(fleet, library)

    events: list[dict[str, object]] = []
    audit_events = audit.get("events") if isinstance(audit, Mapping) else None
    if isinstance(audit_events, list):
        for event in audit_events:
            if not isinstance(event, Mapping):
                continue
            targets = event.get("targets")
            target_values = targets if isinstance(targets, list) else []
            events.append(
                {
                    **event,
                    "source": "audit",
                    "target_names": [names.get(target, "") for target in target_values],
                }
            )
    raw_jobs = jobs.get("jobs") if isinstance(jobs, Mapping) else None
    if isinstance(raw_jobs, list):
        for job in raw_jobs:
            if not isinstance(job, Mapping):
                continue
            events.append(
                {
                    "request_id": job.get("id"),
                    "actor": "Vonk Forge",
                    "action": f"operation.{job.get('kind')}.{job.get('state')}",
                    "occurred_at": job.get("created_at"),
                    "targets": [],
                    "source": "operation",
                    "target_names": [],
                }
            )
    for event in events:
        event["label"] = _activity_label(str(event.get("action", "")))
        event["area"] = _activity_category(event)
        event["status"] = _activity_status(event)
    available_areas = sorted({_activity_category(event) for event in events})
    available_operators = sorted(
        {str(event.get("actor")) for event in events if event.get("actor")}
    )
    normalized = args.search.strip().casefold()
    filtered = []
    for event in events:
        if args.area and event["area"] != args.area:
            continue
        if args.operator and event.get("actor") != args.operator:
            continue
        if args.status and event["status"] != args.status:
            continue
        searchable = [
            event.get("label"),
            event.get("area"),
            event.get("action"),
            event.get("actor"),
            event.get("request_id"),
            event.get("authority_revision"),
            *(event.get("targets") if isinstance(event.get("targets"), list) else []),
            *(
                event.get("target_names")
                if isinstance(event.get("target_names"), list)
                else []
            ),
        ]
        if normalized and not any(
            normalized in str(value or "").casefold() for value in searchable
        ):
            continue
        filtered.append(event)
    status_order = {
        "attention": 0,
        "unsuccessful": 1,
        "in_progress": 2,
        "unknown": 2,
        "recorded": 2,
    }
    if args.sort == "attention":
        filtered.sort(
            key=lambda event: (
                status_order[str(event["status"])],
                -_event_timestamp(event),
            )
        )
    else:
        filtered.sort(key=_event_timestamp, reverse=True)
    summary = {status: 0 for status in ACTIVITY_STATUSES}
    for event in filtered:
        summary[str(event["status"])] += 1
    return {
        "events": filtered,
        "filtered_count": len(filtered),
        "loaded_count": len(events),
        "summary": summary,
        "available_areas": available_areas,
        "available_operators": available_operators,
        "audit_count": len(audit_events) if isinstance(audit_events, list) else 0,
        "operation_count": len(raw_jobs) if isinstance(raw_jobs, list) else 0,
        "operation_total": jobs.get("total", 0) if isinstance(jobs, Mapping) else 0,
        "next_cursor": jobs.get("next_cursor") if isinstance(jobs, Mapping) else None,
        "source_errors": source_errors,
    }


def _run_activity(
    args: argparse.Namespace, client: ControllerClient
) -> dict[str, object]:
    command = args.activity_command
    if command == "list":
        return _combined_activity(args, client)
    if command == "jobs":
        return _load_jobs(
            client,
            cursor=args.cursor,
            limit=args.limit,
            status=args.status,
            target=args.target,
            load_all=args.all,
        )
    path = f"/api/v1/jobs/{_quoted(args.job_id)}"
    if command == "job":
        return client.request(
            "GET",
            path,
            query=_query(
                operation_cursor=args.operation_cursor,
                target_cursor=args.target_cursor,
                limit=args.limit,
            ),
        )
    return _plan_or_request(args, client, "POST", f"{path}/resume")


def run_controller(
    args: argparse.Namespace,
    client: ControllerClient,
    request_id_factory: Callable[[], str],
) -> dict[str, object]:
    if args.command == "fleet":
        return _run_fleet(args, client)
    if args.command == "library":
        return _run_library(args, client, request_id_factory)
    if args.command == "activity":
        return _run_activity(args, client)
    raise ValueError("unsupported controller command")
