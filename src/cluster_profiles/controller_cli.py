"""The current four-noun operator CLI.

The CLI deliberately speaks the same singular operator namespaces as the
Controller. It does not retain the former plural/library/admin trees: a
selector is either the exact stable selector printed by a list or an exact
friendly name accepted by the Controller.
"""

from __future__ import annotations

import argparse
import copy
import math
import re
import shlex
import sys
import time
import urllib.parse
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from .cli_artifact_jobs import (
    ArtifactJobClient,
    add_artifact_job_commands,
    run_artifact_job,
)
from .cli_files import PrivateOutput, read_json_document, write_private_document
from .cli_outcome import (
    EnrollmentDeliveryError,
    Observation,
    Submission,
    operation_state,
)
from .cli_render import render_payload, terminal_text
from .cli_select import SelectorError
from .control_client import (
    ControlClientError,
    ControlConflict,
    ControlForbidden,
    ControlHTTPError,
    ControlMalformedResponse,
    ControlNotFound,
    ControlResponseTooLarge,
    ControlTransportError,
    ControlUnauthorized,
    ControlUnavailable,
    validate_control_document,
)
from .error_reporting import ErrorContext, protocol_context, transport_context
from .generated_control.models.fleet_profile_endpoints_view import (
    FleetProfileEndpointsView,
)

FLEET_HEALTH = ("live", "delayed", "stale", "offline")
TELEMETRY_RANGES = ("1h", "24h", "7d", "31d")
MAX_PAGE_LIMIT = 512
MAX_LOG_LINES = 1000


class ControllerClient(Protocol):
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

    def profile_endpoints(
        self, number: int, alias: str | None = None
    ) -> FleetProfileEndpointsView: ...


@runtime_checkable
class _WatchCallback(Protocol):
    """The progress callback the terminal renderer stores on the namespace."""

    def __call__(self, observed: Mapping[str, object]) -> None: ...


def _quoted(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _log_since(value: str) -> str:
    relative = re.fullmatch(r"([0-9]+)([smhd])", value)
    try:
        if relative is not None:
            seconds = (
                int(relative[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[relative[2]]
            )
            return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is not None:
            return value
    except (ValueError, OverflowError):
        pass
    raise ValueError(
        "--since requires a duration such as 15m or a timestamp with a timezone"
    )


def _add_output(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--wide", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument(
        "--no-input",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Never prompt; this does not grant consent",
    )


def _query(**values: object) -> dict[str, object]:
    return {
        key: value for key, value in values.items() if value not in (None, "", [], ())
    }


def _request_key(args: argparse.Namespace, factory: Callable[[], str]) -> str:
    """Resolve and retain the logical request key for this invocation.

    The key has to exist before the mutation is submitted and stay on the
    parsed arguments afterwards.  When the Controller accepts a submission and
    the response is lost, the error path can only name the durable operation by
    a key it can still read, and a following manual retry has to reconcile that
    same request instead of submitting the same intent under a fresh one.
    """

    value = getattr(args, "request_key", None) or factory()
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ValueError("--request-key must be a UUID") from None
    resolved = str(parsed)
    args.request_key = resolved
    return resolved


def _bounded_int(value: str, *, label: str, minimum: int, maximum: int) -> int:
    """Accept an integer within a closed range.

    The bound belongs in the conversion rather than in ``choices``: a range of
    choices turns every ``--help`` that lists the option into a wall of numbers
    that hides the real options.
    """

    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid {label}: {value!r} is not an integer"
        ) from None
    if not minimum <= number <= maximum:
        raise argparse.ArgumentTypeError(
            f"{label} must be between {minimum} and {maximum}"
        )
    return number


def _page_limit(value: str) -> int:
    """Accept a page size between 1 and 512."""

    return _bounded_int(value, label="page limit", minimum=1, maximum=MAX_PAGE_LIMIT)


def _line_limit(value: str) -> int:
    """Accept a log tail line count between 1 and 1000."""

    return _bounded_int(value, label="line count", minimum=1, maximum=MAX_LOG_LINES)


def _sha256_digest(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise argparse.ArgumentTypeError(
            "review digest must be the complete lowercase SHA-256 value"
        )
    return value


def _activity_limit(value: str) -> int:
    """Accept the Controller's bounded Activity page size."""

    return _bounded_int(value, label="activity page size", minimum=1, maximum=100)


def _activity_state(value: str) -> str:
    if re.fullmatch(r"[a-z][a-z0-9-]{0,31}", value) is None:
        raise argparse.ArgumentTypeError("activity state is invalid")
    return value


def _activity_target(value: str) -> str:
    if re.fullmatch(r"spk_[0-9a-f]{32}", value) is None:
        raise argparse.ArgumentTypeError("target must be an exact Spark ID")
    return value


def _activity_cursor(value: str) -> str:
    if not value or len(value) > 512:
        raise argparse.ArgumentTypeError(
            "activity cursor must contain 1 to 512 characters"
        )
    return value


def _selector(parser: argparse.ArgumentParser, name: str, *, help: str) -> None:
    parser.add_argument(name, metavar=name.upper(), help=help)


def _filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--search", default="")
    for name in (
        "usage",
        "family",
        "version",
        "quantization",
        "publisher",
        "alignment",
    ):
        parser.add_argument(f"--{name}", action="append", default=[])
    parser.add_argument("--updated-since")
    parser.add_argument("--sort", choices=("updated", "name"), default="updated")
    parser.add_argument("--cursor")
    parser.add_argument(
        "--limit",
        type=_page_limit,
        default=100,
        metavar="1-512",
        help=f"Page size, 1 to {MAX_PAGE_LIMIT} (default: 100)",
    )


def _detail_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--metrics", choices=("glance", "all"), default="glance")
    parser.add_argument("--range", choices=TELEMETRY_RANGES, default="1h")
    parser.add_argument("--device")
    parser.add_argument("--interface")
    parser.add_argument("--run")
    parser.add_argument("--capabilities", action="store_true")


def _watch_controls(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--timeout-seconds",
        type=_timeout_seconds,
        default=30,
        help="Observation deadline, 0–300 seconds (default: 30); remote work continues",
    )
    parser.add_argument(
        "--interval-seconds",
        type=_interval_seconds,
        default=1.0,
        help="Poll interval, 0.01–30 seconds (default: 1)",
    )


def _selection_controls(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--timeout-seconds",
        type=_timeout_seconds,
        default=30,
        help="Selection deadline across all pages, 0–300 seconds (default: 30)",
    )


def _finite_seconds(value: str, *, label: str, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{label} requires a number of seconds"
        ) from None
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise argparse.ArgumentTypeError(
            f"{label} must be {minimum:g}–{maximum:g} seconds; received {value}"
        )
    return number


def _timeout_seconds(value: str) -> float:
    return _finite_seconds(value, label="observation timeout", minimum=0, maximum=300)


def _interval_seconds(value: str) -> float:
    return _finite_seconds(value, label="poll interval", minimum=0.01, maximum=30)


def _action_flags(
    parser: argparse.ArgumentParser,
    *,
    destructive: bool = False,
    recipe_remove: bool = False,
    followable: bool = False,
) -> None:
    parser.set_defaults(outcome_context="mutation")
    parser.add_argument(
        "--request-key",
        help="Original request UUID; supply and retain it to reconnect after process death",
    )
    if followable:
        parser.add_argument("--detach", action="store_true")
        _watch_controls(parser)
    if destructive:
        parser.add_argument("--yes", action="store_true")
    if recipe_remove:
        model_choice = parser.add_mutually_exclusive_group()
        model_choice.add_argument("--with-model", action="store_true")
        model_choice.add_argument("--keep-model", action="store_true")
    _add_output(parser)


def _profile_edit_flags(
    parser: argparse.ArgumentParser, *, require_revision: bool = False
) -> None:
    parser.set_defaults(outcome_context="mutation", requires_profile=True)
    parser.add_argument(
        "--expected-revision", type=_revision, required=require_revision
    )
    _add_output(parser)


def _revision(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "revision must be a nonnegative integer"
        ) from None
    if number < 0:
        raise argparse.ArgumentTypeError(
            "revision must be nonnegative; zero means create only"
        )
    return number


def _uuid_argument(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise argparse.ArgumentTypeError("grant identity must be a UUID4") from None
    if parsed.version != 4 or str(parsed) != value:
        raise argparse.ArgumentTypeError("grant identity must be a canonical UUID4")
    return value


def add_controller_commands[ControllerParserT: argparse.ArgumentParser](
    commands: argparse._SubParsersAction[ControllerParserT],
) -> None:
    """Register only Fleet, Model, Recipe and Profile command namespaces."""
    fleet = commands.add_parser("fleet", help="Show and operate enrolled Sparks")
    fleet.add_argument("--watch", action="store_true")
    _watch_controls(fleet)
    fleet.add_argument("--search", default="")
    fleet.add_argument("--health", action="append", choices=FLEET_HEALTH, default=[])
    fleet.add_argument("--warnings-only", action="store_true")
    fleet.add_argument("--sort", choices=("attention", "name"), default="attention")
    _add_output(fleet)
    fleet_actions = fleet.add_subparsers(dest="fleet_action", parser_class=type(fleet))

    detail = fleet_actions.add_parser("detail", help="Show one Spark")
    _selector(detail, "selector", help="Exact Spark selector or friendly name")
    _detail_filters(detail)
    detail.add_argument("--watch", action="store_true")
    _watch_controls(detail)
    detail.add_argument("--technical", action="store_true")
    _add_output(detail)
    node_profile = fleet_actions.add_parser(
        "node-profile", help="Show one Spark's identity, labels, and lifecycle"
    )
    _selector(node_profile, "selector", help="Exact Spark ID or friendly name")
    _selection_controls(node_profile)
    _add_output(node_profile)
    rename = fleet_actions.add_parser("rename", help="Change a Spark friendly name")
    _selector(rename, "selector", help="Exact Spark selector or friendly name")
    rename.add_argument("new_name")
    _action_flags(rename)
    enroll = fleet_actions.add_parser(
        "enroll", help="Create a one-time enrollment grant"
    )
    enroll.set_defaults(outcome_context="mutation")
    enroll.add_argument("name")
    enroll.add_argument("--ttl-seconds", type=int, default=900)
    enroll.add_argument("--output", type=Path, required=True)
    enroll.add_argument("--request-key", type=_uuid_argument)
    _add_output(enroll)
    for action_name, help_text in (
        ("re-enroll", "Replace a Spark certificate"),
        ("remove", "Revoke and remove a Spark"),
    ):
        action = fleet_actions.add_parser(action_name, help=help_text)
        action.set_defaults(outcome_context="mutation")
        _selector(action, "selector", help="Exact Spark selector or friendly name")
        _add_output(action)
        if action_name == "remove":
            action.add_argument("--yes", action="store_true")
        else:
            action.add_argument("--output", type=Path, required=True)
            action.add_argument("--request-key", type=_uuid_argument)
            action.add_argument("--yes", action="store_true")
    enrollment = fleet_actions.add_parser(
        "enrollment", help="Inspect or revoke a bootstrap grant"
    )
    enrollment_actions = enrollment.add_subparsers(
        dest="enrollment_action", required=True, parser_class=type(fleet)
    )
    for action_name in ("status", "revoke"):
        action = enrollment_actions.add_parser(action_name)
        action.add_argument("grant_id", type=_uuid_argument)
        _add_output(action)
        if action_name == "revoke":
            action.set_defaults(outcome_context="mutation")
            action.add_argument("--yes", action="store_true")
    upgrade = fleet_actions.add_parser(
        "upgrade", help="Roll out the signed Spark agent package through Controller"
    )
    upgrade.set_defaults(outcome_context="mutation")
    upgrade.add_argument("selector", nargs="?")
    upgrade.add_argument("--all", action="store_true")
    upgrade.add_argument(
        "--strategy", choices=("one-at-a-time",), default="one-at-a-time"
    )
    upgrade.add_argument("--request-key", type=_uuid_argument)
    upgrade.add_argument("--detach", action="store_true")
    upgrade.add_argument("--yes", action="store_true")
    _watch_controls(upgrade)
    _add_output(upgrade)
    loginfo = fleet_actions.add_parser(
        "loginfo", help="Read bounded Controller-collected logs"
    )
    _selector(loginfo, "selector", help="Exact Spark selector or friendly name")
    loginfo.add_argument("--since", default="15m")
    loginfo.add_argument(
        "--lines",
        type=_line_limit,
        default=100,
        metavar="1-1000",
        help=f"Tail lines, 1 to {MAX_LOG_LINES} (default: 100)",
    )
    loginfo.add_argument("--recipe")
    loginfo.add_argument("--source", choices=("client", "monitor", "runtime", "job"))
    loginfo.add_argument("--follow", action="store_true")
    _watch_controls(loginfo)
    _add_output(loginfo)

    fleet_progress = fleet_actions.add_parser(
        "progress", help="Inspect or follow one fleet job"
    )
    fleet_progress.add_argument("job_id")
    fleet_progress.add_argument("--follow", action="store_true")
    _watch_controls(fleet_progress)
    _add_output(fleet_progress)

    activity = fleet_actions.add_parser(
        "activity", help="List durable operation history across owners"
    )
    activity.add_argument("--limit", type=_activity_limit, default=20)
    activity.add_argument("--cursor", type=_activity_cursor)
    activity.add_argument("--state", type=_activity_state)
    activity.add_argument("--target", type=_activity_target)
    activity.add_argument("--request-id", type=_uuid_argument)
    _add_output(activity)

    resume = fleet_actions.add_parser(
        "resume", help="Resume one job only when its owner advertises permission"
    )
    resume.set_defaults(outcome_context="mutation")
    resume.add_argument("job_id")
    resume.add_argument("--yes", action="store_true", required=True)
    _add_output(resume)

    model = commands.add_parser("model", help="Browse and manage model cache")
    model.add_argument("--watch", action="store_true")
    _watch_controls(model)
    _add_output(model)
    model_actions = model.add_subparsers(dest="model_action", parser_class=type(model))
    model_library = model_actions.add_parser(
        "library", help="List published model variants"
    )
    _filters(model_library)
    _add_output(model_library)
    model_detail = model_actions.add_parser(
        "detail", help="Show an exact model variant"
    )
    _selector(model_detail, "selector", help="Exact model selector or friendly name")
    model_detail.add_argument("--watch", action="store_true")
    _watch_controls(model_detail)
    model_detail.add_argument("--technical", action="store_true")
    _add_output(model_detail)
    model_download = model_actions.add_parser("download", help="Cache a model variant")
    _selector(model_download, "selector", help="Exact model selector or friendly name")
    _action_flags(model_download, followable=True)
    model_remove = model_actions.add_parser(
        "remove", help="Review and remove Controller model cache assets"
    )
    _selector(model_remove, "selector", help="Exact model selector or friendly name")
    _action_flags(model_remove, destructive=True, followable=True)
    model_remove.add_argument(
        "--review",
        action="store_true",
        help="Show the Controller-owned removal impact without submitting it",
    )
    model_remove.add_argument(
        "--review-digest",
        type=_sha256_digest,
        metavar="SHA256",
        help="Exact removal review digest to accept with --yes",
    )
    model_cancel = model_actions.add_parser(
        "cancel", help="Cancel one model download while preserving resumable files"
    )
    model_cancel.add_argument("operation_id", type=_uuid_selector)
    _action_flags(model_cancel, destructive=True, followable=True)
    model_cancel.add_argument(
        "--reason",
        default="operator requested cancellation",
        help="Short reason retained with the durable cancellation request",
    )

    recipe = commands.add_parser("recipe", help="Browse and manage runnable recipes")
    recipe.add_argument("--watch", action="store_true")
    _watch_controls(recipe)
    _add_output(recipe)
    recipe_actions = recipe.add_subparsers(
        dest="recipe_action", parser_class=type(recipe)
    )
    recipe_library = recipe_actions.add_parser(
        "library", help="List compatible recipes"
    )
    _filters(recipe_library)
    recipe_library.add_argument("--model", action="append", default=[])
    recipe_library.add_argument("--all-models", action="store_true")
    recipe_library.add_argument(
        "--ready",
        action="store_true",
        default=None,
        help="Require exact usable NAS assets and an admissible placement",
    )
    recipe_library.add_argument(
        "--fits-fleet",
        action="store_true",
        default=None,
        help="Require a placement fitting fresh current fleet capacity",
    )
    recipe_library.add_argument(
        "--sparks",
        action="append",
        type=int,
        default=[],
        help="Exact topology node count",
    )
    _add_output(recipe_library)
    recipe_detail = recipe_actions.add_parser("detail", help="Show an exact recipe")
    _selector(recipe_detail, "selector", help="Exact recipe selector or friendly name")
    recipe_detail.add_argument("--watch", action="store_true")
    _watch_controls(recipe_detail)
    recipe_detail.add_argument("--technical", action="store_true")
    _add_output(recipe_detail)
    recipe_download = recipe_actions.add_parser(
        "download", help="Cache a recipe and missing model"
    )
    _selector(
        recipe_download, "selector", help="Exact recipe selector or friendly name"
    )
    _action_flags(recipe_download, followable=True)
    recipe_update = recipe_actions.add_parser(
        "update", help="Refresh an exact recipe or all currently cached recipes"
    )
    recipe_update.add_argument("selector", nargs="?")
    recipe_update.add_argument("--all", action="store_true")
    _action_flags(recipe_update, followable=True)
    recipe_remove = recipe_actions.add_parser(
        "remove", help="Review and remove Controller recipe cache assets"
    )
    _selector(recipe_remove, "selector", help="Exact recipe selector or friendly name")
    _action_flags(recipe_remove, destructive=True, recipe_remove=True, followable=True)
    recipe_remove.add_argument(
        "--review",
        action="store_true",
        help="Show the Controller-owned removal impact without submitting it",
    )
    recipe_remove.add_argument(
        "--review-digest",
        type=_sha256_digest,
        metavar="SHA256",
        help="Exact removal review digest to accept with --yes",
    )
    recipe_cancel = recipe_actions.add_parser(
        "cancel", help="Cancel one accepted recipe preparation"
    )
    recipe_cancel.add_argument("operation_id")
    _action_flags(recipe_cancel, destructive=True, followable=True)
    recipe_cancel.add_argument("--reason", default="operator requested cancellation")

    recipe_installation = recipe_actions.add_parser(
        "installation",
        help="Inspect and reconcile one installed recipe identity",
    )
    installation_actions = recipe_installation.add_subparsers(
        dest="installation_action", required=True, parser_class=type(recipe)
    )
    installation_reconcile = installation_actions.add_parser(
        "reconcile",
        help="Review or reconcile one invalid stopped installation",
    )
    installation_reconcile.add_argument(
        "installation_id", type=_uuid_argument, help="Exact installation UUID"
    )
    _action_flags(installation_reconcile, destructive=True, followable=True)
    installation_reconcile.add_argument(
        "--review",
        action="store_true",
        help="Show the exact reconciliation plan without submitting it",
    )
    installation_reconcile.add_argument(
        "--review-digest",
        type=_sha256_digest,
        metavar="SHA256",
        help="Exact reconciliation plan digest to accept with --yes",
    )

    add_artifact_job_commands(
        recipe_actions, add_output=_add_output, watch_controls=_watch_controls
    )

    for noun, actions in (("model", model_actions), ("recipe", recipe_actions)):
        progress = actions.add_parser(
            "progress", help=f"Inspect or follow one {noun} operation"
        )
        target = progress.add_mutually_exclusive_group(required=True)
        target.add_argument("operation_id", nargs="?")
        target.add_argument(
            "--request-key",
            help="Original request UUID; resolves once before following",
        )
        progress.add_argument("--follow", action="store_true")
        _watch_controls(progress)
        _add_output(progress)

    profile = commands.add_parser("profile", help="Edit and load a whole-fleet profile")
    _add_output(profile)
    profile_actions = profile.add_subparsers(
        dest="profile_action", parser_class=type(profile)
    )
    profile_list = profile_actions.add_parser(
        "list", help="List stable numbered profiles"
    )
    _add_output(profile_list)
    profile_name = profile_actions.add_parser("name", help="Name the selected profile")
    profile_name.add_argument("name")
    _profile_edit_flags(profile_name)
    profile_add = profile_actions.add_parser(
        "add", help="Assign a recipe to Sparks and autosave"
    )
    profile_add.add_argument("recipe_selector")
    profile_add.add_argument("--spark", action="append", required=True)
    profile_add.add_argument("--as", dest="assignment_name")
    profile_add.add_argument("--model-variant")
    profile_add.add_argument(
        "--state", dest="desired_state", choices=("installed", "running")
    )
    _profile_edit_flags(profile_add)
    _selection_controls(profile_add)
    profile_remove = profile_actions.add_parser(
        "remove", help="Remove an assignment or Spark members"
    )
    profile_remove.add_argument("assignment")
    profile_remove.add_argument("--spark", action="append", default=[])
    _profile_edit_flags(profile_remove)
    _selection_controls(profile_remove)
    profile_configure = profile_actions.add_parser(
        "configure", help="Edit saved metadata without loading"
    )
    profile_configure.add_argument("--description")
    profile_configure.add_argument("--retention", choices=("keep-cached", "exact"))
    profile_configure.add_argument("--favorite", choices=("true", "false"))
    profile_configure.add_argument(
        "--label", action="append", default=[], metavar="KEY=VALUE"
    )
    profile_configure.add_argument(
        "--remove-label", action="append", default=[], metavar="KEY"
    )
    _profile_edit_flags(profile_configure)
    profile_export = profile_actions.add_parser(
        "export", help="Export the canonical saved definition as JSON"
    )
    profile_export.add_argument("--output", type=Path)
    _add_output(profile_export)
    profile_import = profile_actions.add_parser(
        "import", help="Save a definition; never load it"
    )
    profile_import.add_argument(
        "--file", required=True, help="JSON file or - for stdin"
    )
    _profile_edit_flags(profile_import, require_revision=True)
    profile_load = profile_actions.add_parser(
        "load", help="Apply the entire profile to the fleet"
    )
    profile_load.set_defaults(outcome_context="mutation", requires_profile=True)
    profile_load.add_argument("--dry-run", action="store_true")
    profile_load.add_argument(
        "--expected-plan", help="The exact plan digest reviewed before this load"
    )
    profile_load.add_argument("--yes", action="store_true")
    profile_load.add_argument("--request-key")
    profile_load.add_argument("--detach", action="store_true")
    _watch_controls(profile_load)
    _add_output(profile_load)
    profile_cancel = profile_actions.add_parser(
        "cancel", help="Cancel one profile application and reconcile its effects"
    )
    profile_cancel.set_defaults(outcome_context="mutation", requires_profile=True)
    profile_cancel.add_argument("application_id", type=_uuid_selector)
    profile_cancel.add_argument("--yes", action="store_true")
    profile_cancel.add_argument("--request-key", type=_uuid_selector)
    profile_cancel.add_argument("--detach", action="store_true")
    _watch_controls(profile_cancel)
    _add_output(profile_cancel)
    profile_progress = profile_actions.add_parser(
        "progress", help="Show the latest profile load"
    )
    profile_progress.add_argument("--follow", action="store_true")
    profile_progress_selectors = profile_progress.add_mutually_exclusive_group()
    profile_progress_selectors.add_argument("--application", type=_uuid_selector)
    profile_progress_selectors.add_argument("--request-key", type=_uuid_selector)
    _watch_controls(profile_progress)
    _add_output(profile_progress)
    profile_endpoint = profile_actions.add_parser(
        "endpoint",
        help="Discover current published endpoints from the loaded profile",
    )
    profile_endpoint.add_argument(
        "alias", nargs="?", help="Only show this endpoint alias from the profile"
    )
    _add_output(profile_endpoint)


def _uuid_selector(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise argparse.ArgumentTypeError("operation selector must be a UUID") from None


def _profile_number(args: argparse.Namespace) -> int:
    number = getattr(args, "profile_number", None)
    if number is None:
        return 1
    if type(number) is not int or number < 1:
        raise ValueError("--profile must be a positive stable profile number")
    return number


_TERMINAL_STATES = {
    "succeeded",
    "completed",
    "failed",
    "partial",
    "cancelled",
    "blocked",
    "rejected",
}


def _state(value: Mapping[str, object]) -> str:
    return operation_state(value)


def _watch_callback(args: argparse.Namespace) -> _WatchCallback | None:
    callback = getattr(args, "_watch_callback", None)
    return callback if isinstance(callback, _WatchCallback) else None


def _bounded_timeout(args: argparse.Namespace) -> float:
    return _timeout_seconds(str(getattr(args, "timeout_seconds", 30)))


def _bounded_interval(args: argparse.Namespace) -> float:
    return _interval_seconds(str(getattr(args, "interval_seconds", 1.0)))


def _observation_delay(
    error: BaseException, fallback: float, remaining: float
) -> float:
    """Return the delay before the next observation attempt.

    Bound waiting by the observation's remaining time. A server delay beyond
    that budget ends observation at its deadline, without polling prematurely.
    """

    retry_after = getattr(error, "retry_after_seconds", None)
    if type(retry_after) is int and retry_after >= 0:
        return max(0.01, float(min(retry_after, max(0.0, remaining))))
    return fallback


def _observation_reason(error: BaseException) -> str:
    if isinstance(error, ControlUnavailable):
        return "control API reported unavailable"
    if isinstance(error, (ControlTransportError, OSError)):
        return "control API connection was lost"
    return type(error).__name__


def _reconnect_command(args: argparse.Namespace, path: str) -> str:
    parts = path.split("/")
    if path.startswith("/api/profile/applications/"):
        command = [
            "vonkctl",
            "--profile",
            str(_profile_number(args)),
            "profile",
            "progress",
            "--application",
            urllib.parse.unquote(parts[-1]),
            "--follow",
        ]
    elif len(parts) == 5 and parts[1:3] == ["api", "fleet"] and parts[4] == "loginfo":
        # Log observation has already resolved any friendly selector. Retain
        # the caller's complete scope, but reconnect by the immutable node ID.
        invocation: list[str] = list(getattr(args, "invocation", ()))
        replaced = False
        for index in range(len(invocation) - 2):
            if invocation[index : index + 2] == ["fleet", "loginfo"]:
                invocation[index + 2] = urllib.parse.unquote(parts[3])
                replaced = True
                break
        if replaced:
            command = ["vonkctl", *invocation]
        else:
            command = [
                "vonkctl",
                "fleet",
                "loginfo",
                urllib.parse.unquote(parts[3]),
            ]
            for name in ("since", "lines", "recipe", "source"):
                value = getattr(args, name, None)
                if value is not None:
                    command.extend((f"--{name}", str(value)))
            if getattr(args, "follow", False):
                command.append("--follow")
            command.extend(
                (
                    "--timeout-seconds",
                    str(_bounded_timeout(args)),
                    "--interval-seconds",
                    str(_bounded_interval(args)),
                )
            )
    elif (
        len(parts) == 5 and parts[2] in {"model", "recipe"} and parts[3] == "operations"
    ):
        command = [
            "vonkctl",
            parts[2],
            "progress",
            urllib.parse.unquote(parts[-1]),
            "--follow",
        ]
    elif path.startswith("/api/jobs/"):
        command = [
            "vonkctl",
            "fleet",
            "progress",
            urllib.parse.unquote(parts[-1]),
            "--follow",
        ]
    else:
        command = ["vonkctl", *getattr(args, "invocation", ())]
    return shlex.join(command)


def _poll_path(
    client: ControllerClient,
    path: str,
    initial: dict[str, object],
    args: argparse.Namespace,
    *,
    query: Mapping[str, object] | None = None,
    terminal: Callable[[Mapping[str, object]], bool] | None = None,
    validate: Callable[[Mapping[str, object]], None] | None = None,
) -> dict[str, object]:
    """Observe a bounded durable snapshot, retaining the last truthful value.

    A temporary loss of the Controller must not discard the observation.  The
    last confirmed snapshot stays authoritative and polling continues to the
    bounded deadline, reporting why it was reconnecting.  Authorization,
    contract and not-found answers stay immediate errors: retrying them would
    only delay the operator's decision.  The durable operation's own outcome is
    never rewritten by an observation failure.
    """

    callback = _watch_callback(args)
    is_terminal = terminal or (lambda observed: _state(observed) in _TERMINAL_STATES)
    current = initial
    if validate is not None:
        validate(current)
    started = time.monotonic()
    timeout = _bounded_timeout(args)
    observation = Observation(
        path,
        _reconnect_command(args, path),
        current,
        timeout,
        started,
        started,
        datetime.now(UTC),
    )
    args.observation = observation
    deadline = started + timeout
    interval = _bounded_interval(args)
    while True:
        if callback is not None:
            callback(current)
        if is_terminal(current):
            observation.status = "complete"
            return current
        if time.monotonic() >= deadline:
            observation.status = "timed_out"
            return current
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            observation.status = "timed_out"
            return current
        try:
            current = client.request(
                "GET", path, query=query, timeout_seconds=remaining
            )
            if validate is not None:
                validate(current)
        except (ControlUnavailable, ControlTransportError, OSError) as error:
            if isinstance(error, BrokenPipeError):
                raise
            observation.error = _observation_reason(error)
            interval = _observation_delay(error, interval, deadline - time.monotonic())
            continue
        observation.update(current)
        interval = _bounded_interval(args)


def _submit_profile_load(
    client: ControllerClient,
    number: int,
    expected_digest: str,
    args: argparse.Namespace,
    factory: Callable[[], str],
) -> dict[str, object]:
    key = _request_key(args, factory)
    path = f"/api/profile/{number}/load"
    lookup = f"/api/profile/{number}/requests/{key}"
    body: dict[str, object] = {
        "request_key": key,
        "plan_digest": expected_digest,
    }

    def validate(result: Mapping[str, object]) -> str:
        progress = result.get("progress")
        intended = (
            progress.get("intended_profile") if isinstance(progress, Mapping) else None
        )
        operation_id = result.get("id")
        if (
            result.get("request_key") != key
            or not isinstance(intended, Mapping)
            or intended.get("reviewed_plan_digest") != expected_digest
            or not isinstance(operation_id, str)
            or not operation_id
        ):
            raise ControlMalformedResponse(
                "profile load receipt identifies another request or review"
            )
        return operation_id

    return _submit_idempotent_request(
        client,
        args,
        key=key,
        path=path,
        lookup=lookup,
        body=body,
        noun="profile",
        action="load",
        validate=validate,
        reconnect=shlex.join(
            [
                "vonkctl",
                "--profile",
                str(number),
                "profile",
                "progress",
                "--request-key",
                key,
                "--follow",
            ]
        ),
    )


def _submit_fleet_upgrade(
    args: argparse.Namespace,
    client: ControllerClient,
    factory: Callable[[], str],
) -> dict[str, object]:
    """Submit or replay one exact Controller-authorized upgrade request."""

    key = _request_key(args, factory)
    body: dict[str, object] = {
        "all": args.all,
        "request_key": key,
        "strategy": args.strategy,
    }
    if args.selector:
        body["selectors"] = [args.selector]

    def validate(result: Mapping[str, object]) -> str:
        operation_id = result.get("operation_id")
        plan_digest = result.get("plan_digest")
        targets = result.get("targets")
        if (
            result.get("action") != "upgrade"
            or result.get("request_key") != key
            or not isinstance(operation_id, str)
            or not operation_id
            or not isinstance(plan_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", plan_digest) is None
            or not isinstance(targets, list)
            or len(targets) > 64
            or not all(
                isinstance(target, str)
                and re.fullmatch(r"spk_[0-9a-f]{32}", target) is not None
                for target in targets
            )
            or len(targets) != len(set(targets))
        ):
            raise ControlMalformedResponse(
                "fleet upgrade receipt does not identify this request and plan"
            )
        return operation_id

    scope = ["--all"] if args.all else [cast(str, args.selector)]
    reconnect = shlex.join(
        [
            "vonkctl",
            "fleet",
            "upgrade",
            *scope,
            "--request-key",
            key,
            "--strategy",
            args.strategy,
            "--yes",
        ]
    )
    path = "/api/fleet/upgrade"
    return _submit_idempotent_request(
        client,
        args,
        key=key,
        path=path,
        # The upgrade route is itself the request-key lookup. Reposting the
        # identical body returns the original durable job.
        lookup=path,
        body=body,
        noun="fleet",
        action="upgrade",
        validate=validate,
        reconnect=reconnect,
    )


def _cache_operation_id(noun: str, result: Mapping[str, object]) -> str:
    if noun == "model":
        field = "operation_id"
    elif result.get("kind") in {
        "recipe.image.availability.v2",
        "recipe.cache.update.v2",
    }:
        field = "id"
    elif result.get("action") == "remove":
        field = "operation_id"
    else:
        raise ControlMalformedResponse("recipe response does not identify an operation")
    operation_id = result.get(field)
    if not isinstance(operation_id, str) or not operation_id:
        raise ControlMalformedResponse("cache response does not identify an operation")
    return operation_id


def _validate_cache_removal_receipt(
    noun: str,
    selector: str,
    request_key: str,
    result: Mapping[str, object],
    *,
    expected_with_model: bool | None,
    expected_model_content_sha256: str | None = None,
    expected_review_digest: str | None = None,
) -> str:
    """Bind a removal receipt to its submitted target and durable identity."""

    contract = (
        "ModelCacheOperatorResponse" if noun == "model" else "RecipeOperatorResponse"
    )
    try:
        receipt = validate_control_document(contract, dict(result))
    except ControlClientError:
        raise ControlMalformedResponse(
            f"{noun} removal receipt does not match its canonical contract"
        ) from None
    receipt_selector = receipt.get("selector")
    selector_matches = (
        isinstance(receipt_selector, str)
        and receipt_selector.strip().casefold() == selector.strip().casefold()
    )
    receipt_review_digest = receipt.get("review_digest")
    review_matches = (
        isinstance(receipt_review_digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", receipt_review_digest) is not None
        and (
            expected_review_digest is None
            or receipt_review_digest == expected_review_digest
        )
    )
    identity_matches = (
        receipt.get("action") == "remove"
        and selector_matches
        and receipt.get("request_key") == request_key
        and review_matches
    )
    if noun == "recipe":
        identity_matches = (
            identity_matches
            and expected_model_content_sha256 is None
            and expected_with_model is not None
            and receipt.get("with_model") is expected_with_model
        )
    else:
        digest = receipt.get("model_content_sha256")
        identity_matches = (
            identity_matches
            and expected_with_model is None
            and isinstance(digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
            and (
                expected_model_content_sha256 is None
                or digest == expected_model_content_sha256
            )
        )
    if not identity_matches:
        raise ControlMalformedResponse(
            f"{noun} removal receipt identifies another request, selector, digest, or retention choice"
        )
    return _cache_operation_id(noun, receipt)


def _removal_is_interactive(args: argparse.Namespace) -> bool:
    return (
        not (
            getattr(args, "global_json", False)
            or getattr(args, "json", False)
            or getattr(args, "no_input", False)
        )
        and sys.stdin.isatty()
        and sys.stderr.isatty()
    )


def _cache_removal_review(
    client: ControllerClient,
    noun: str,
    selector: str,
    *,
    with_model: bool | None,
) -> dict[str, object]:
    path = f"/api/{noun}/{_quoted(selector)}/remove-review"
    query = {"with_model": with_model} if noun == "recipe" else None
    document = client.request("GET", path, query=query)
    try:
        review = validate_control_document("CacheRemovalReview", document)
    except ControlClientError:
        raise ControlMalformedResponse(
            f"{noun} removal review does not match its canonical contract"
        ) from None
    target_identity = review.get("target_identity")
    review_selector = review.get("selector")
    if (
        review.get("action") != "remove"
        or review.get("resource_kind") != noun
        or not isinstance(review_selector, str)
        or review_selector.strip().casefold() != selector.strip().casefold()
        or not isinstance(target_identity, str)
        or not target_identity
    ):
        raise ControlMalformedResponse(
            f"{noun} removal review identifies another selector or resource"
        )
    if noun == "model" and re.fullmatch(r"[0-9a-f]{64}", target_identity) is None:
        raise ControlMalformedResponse("model removal review has no content identity")
    if noun == "model" and (
        "with_model" not in review or review.get("with_model") is not None
    ):
        raise ControlMalformedResponse(
            "model removal review has an invalid model-retention choice"
        )
    if noun == "recipe" and review.get("with_model") is not with_model:
        raise ControlMalformedResponse(
            "recipe removal review identifies another model-retention choice"
        )
    return review


def _review_digest_for_acceptance(
    client: ControllerClient,
    noun: str,
    selector: str,
    args: argparse.Namespace,
    *,
    with_model: bool | None,
) -> tuple[str, str | None]:
    supplied_digest = getattr(args, "review_digest", None)
    interactive = _removal_is_interactive(args)
    if supplied_digest is None and (args.yes or not interactive):
        raise ValueError(
            f"{noun} remove requires --review-digest SHA256 --yes for scripted consent; "
            f"review with {noun} remove {selector} --review first or omit --yes in a terminal"
        )
    if supplied_digest is not None and not args.yes and not interactive:
        raise ValueError(
            f"{noun} remove requires --yes with --review-digest in noninteractive mode"
        )

    review = _cache_removal_review(client, noun, selector, with_model=with_model)
    current_digest = cast(str, review["review_digest"])
    blockers = cast(list[Mapping[str, object]], review["blockers"])
    if blockers:
        if not (getattr(args, "global_json", False) or getattr(args, "json", False)):
            with redirect_stdout(sys.stderr):
                render_payload(review, noun, action="preview")
        details = "; ".join(
            f"{cast(str, blocker['code'])}: {cast(str, blocker['detail'])}"
            for blocker in blockers
        )
        raise ControlConflict(
            409,
            f"{noun} removal is blocked by the Controller review: {details}. "
            f"Inspect the read-only review with {noun} remove {selector} --review.",
        )
    if supplied_digest is not None and current_digest != supplied_digest:
        if not (getattr(args, "global_json", False) or getattr(args, "json", False)):
            with redirect_stdout(sys.stderr):
                render_payload(review, noun, action="preview")
        raise ControlConflict(
            409,
            f"{noun} removal review changed; inspect the current impact and "
            "rerun with the new review digest",
        )
    if not (args.yes and supplied_digest is not None):
        with redirect_stdout(sys.stderr):
            render_payload(review, noun, action="preview")
        _confirm_action(
            args,
            f"Remove {noun} selector {selector} with reviewed impact {current_digest}?",
        )
    return current_digest, cast(str, review["target_identity"])


def _existing_cache_removal(
    client: ControllerClient,
    args: argparse.Namespace,
    factory: Callable[[], str],
    *,
    noun: str,
    selector: str,
    with_model: bool | None,
) -> dict[str, object] | None:
    """Reconcile a caller-supplied request before consulting mutable selectors."""
    if getattr(args, "request_key", None) is None:
        return None
    key = _request_key(args, factory)
    path = f"/api/{noun}/{_quoted(selector)}/remove"
    lookup = f"/api/{noun}/requests/{_quoted(key)}"
    timeout = client.request_timeout_seconds
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("request timeout must be finite and positive")
    submission = Submission(
        key,
        path,
        lookup,
        3 * timeout,
        action="remove",
        acceptance="not_submitted",
    )
    args.submission = submission
    try:
        existing = client.request("GET", lookup)
    except ControlNotFound:
        return None
    submission.operation_id = _validate_cache_removal_receipt(
        noun,
        selector,
        key,
        existing,
        expected_with_model=with_model,
        expected_review_digest=getattr(args, "review_digest", None),
    )
    submission.acceptance = "accepted"
    return existing


def _submit_model_removal(
    client: ControllerClient,
    args: argparse.Namespace,
    factory: Callable[[], str],
    *,
    review_digest: str,
    model_content_sha256: str,
) -> dict[str, object]:
    """Submit one removal bound to the Controller's displayed review."""
    selector = cast(str, args.selector)
    key = _request_key(args, factory)
    path = f"/api/model/{_quoted(selector)}/remove"
    lookup = f"/api/model/requests/{_quoted(key)}"

    def validate(result: Mapping[str, object]) -> str:
        return _validate_cache_removal_receipt(
            "model",
            selector,
            key,
            result,
            expected_with_model=None,
            expected_model_content_sha256=model_content_sha256,
            expected_review_digest=review_digest,
        )

    return _submit_idempotent_request(
        client,
        args,
        key=key,
        path=path,
        lookup=lookup,
        body={
            "schema_version": 2,
            "request_key": key,
            "model_content_sha256": model_content_sha256,
            "review_digest": review_digest,
        },
        noun="model",
        action="remove",
        validate=validate,
        lookup_validate=validate,
        reconnect=shlex.join(
            [
                "vonkctl",
                "model",
                "remove",
                selector,
                "--review-digest",
                review_digest,
                "--yes",
                "--request-key",
                key,
            ]
        ),
    )


def _submit_recipe_removal(
    client: ControllerClient,
    args: argparse.Namespace,
    factory: Callable[[], str],
    *,
    review_digest: str,
    with_model: bool,
) -> dict[str, object]:
    """Submit one recipe removal bound to its reviewed retention choice."""
    selector = cast(str, args.selector)
    key = _request_key(args, factory)
    path = f"/api/recipe/{_quoted(selector)}/remove"
    lookup = f"/api/recipe/requests/{_quoted(key)}"

    def validate(result: Mapping[str, object]) -> str:
        return _validate_cache_removal_receipt(
            "recipe",
            selector,
            key,
            result,
            expected_with_model=with_model,
            expected_review_digest=review_digest,
        )

    choice = "--with-model" if with_model else "--keep-model"
    return _submit_idempotent_request(
        client,
        args,
        key=key,
        path=path,
        lookup=lookup,
        body={
            "schema_version": 2,
            "request_key": key,
            "with_model": with_model,
            "review_digest": review_digest,
        },
        noun="recipe",
        action="remove",
        validate=validate,
        lookup_validate=validate,
        reconnect=shlex.join(
            [
                "vonkctl",
                "recipe",
                "remove",
                selector,
                choice,
                "--review-digest",
                review_digest,
                "--yes",
                "--request-key",
                key,
            ]
        ),
    )


def _remove_model(
    client: ControllerClient,
    args: argparse.Namespace,
    factory: Callable[[], str],
) -> dict[str, object]:
    selector = args.selector.strip()
    if not selector:
        raise ValueError("model remove requires a non-empty selector")
    args.selector = selector
    if args.review:
        if args.yes or args.review_digest is not None or args.request_key is not None:
            raise ValueError(
                "model remove --review cannot be combined with consent or request flags"
            )
        if args.detach:
            raise ValueError("model remove --review cannot be detached")
        args.outcome_context = "read"
        args.model_action = "preview"
        return _cache_removal_review(client, "model", selector, with_model=None)

    existing = _existing_cache_removal(
        client,
        args,
        factory,
        noun="model",
        selector=selector,
        with_model=None,
    )
    if existing is not None:
        return existing
    digest, target_identity = _review_digest_for_acceptance(
        client, "model", selector, args, with_model=None
    )
    if target_identity is None:
        raise ControlMalformedResponse("model removal review has no target identity")
    return _submit_model_removal(
        client,
        args,
        factory,
        review_digest=digest,
        model_content_sha256=target_identity,
    )


def _remove_recipe(
    client: ControllerClient,
    args: argparse.Namespace,
    factory: Callable[[], str],
) -> dict[str, object]:
    selector = args.selector.strip()
    if not selector:
        raise ValueError("recipe remove requires a non-empty selector")
    args.selector = selector
    if not (args.with_model or args.keep_model):
        raise ValueError("recipe remove requires --with-model or --keep-model")
    with_model = args.with_model and not args.keep_model
    if args.review:
        if args.yes or args.review_digest is not None or args.request_key is not None:
            raise ValueError(
                "recipe remove --review cannot be combined with consent or request flags"
            )
        if args.detach:
            raise ValueError("recipe remove --review cannot be detached")
        args.outcome_context = "read"
        args.recipe_action = "preview"
        return _cache_removal_review(client, "recipe", selector, with_model=with_model)

    existing = _existing_cache_removal(
        client,
        args,
        factory,
        noun="recipe",
        selector=selector,
        with_model=with_model,
    )
    if existing is not None:
        return existing
    digest, _target_identity = _review_digest_for_acceptance(
        client, "recipe", selector, args, with_model=with_model
    )
    return _submit_recipe_removal(
        client,
        args,
        factory,
        review_digest=digest,
        with_model=with_model,
    )


def _submit_cache_request(
    client: ControllerClient,
    noun: str,
    args: argparse.Namespace,
    factory: Callable[[], str],
) -> dict[str, object]:
    """One POST, exact request reconciliation, and at most one identical replay."""

    key = _request_key(args, factory)
    action = getattr(args, f"{noun}_action")
    if action == "update":
        path = "/api/recipe/update"
    else:
        path = f"/api/{noun}/{_quoted(args.selector)}/download"
    lookup = f"/api/{noun}/requests/{key}"
    body: dict[str, object] = {"schema_version": 2, "request_key": key}
    if action == "update":
        body.update(all=args.all, selectors=[args.selector] if args.selector else [])

    def validate(result: Mapping[str, object]) -> str:
        if action == "update":
            intent = result.get("request")
            matches = (
                result.get("kind") == "recipe.cache.update.v2"
                and result.get("request_id") == key
                and isinstance(intent, Mapping)
                and intent.get("all") is args.all
                and intent.get("selectors") == body["selectors"]
            )
        elif noun == "model":
            returned_selector = result.get("selector")
            matches = (
                result.get("request_key") == key
                and result.get("action") == "download"
                and isinstance(returned_selector, str)
                and returned_selector.strip() == args.selector.strip()
            )
        else:
            intent = result.get("request")
            matches = (
                result.get("kind") == "recipe.image.availability.v2"
                and result.get("request_id") == key
                and isinstance(intent, Mapping)
                and intent.get("kind") == "selector"
                and intent.get("selector") == args.selector
                and intent.get("force") is False
            )
        if not matches:
            raise ControlMalformedResponse(
                f"{action} receipt identifies another request or intent"
            )
        return _cache_operation_id(noun, result)

    return _submit_idempotent_request(
        client,
        args,
        key=key,
        path=path,
        lookup=lookup,
        body=body,
        noun=noun,
        action=action,
        validate=validate,
        reconnect=shlex.join(
            ["vonkctl", noun, "progress", "--request-key", key, "--follow"]
        ),
    )


def _submit_model_cancellation(
    client: ControllerClient,
    args: argparse.Namespace,
    factory: Callable[[], str],
) -> dict[str, object]:
    """Cancel one operation through its durable identity and one safe replay."""

    if not args.yes:
        raise ValueError("model cancel requires --yes in noninteractive mode")
    operation_id = str(uuid.UUID(args.operation_id))
    key = _request_key(args, factory)
    reason = args.reason.strip()
    if not reason or len(reason) > 512:
        raise ValueError("--reason must contain between 1 and 512 characters")
    args.reason = reason
    path = f"/api/model/operations/{_quoted(operation_id)}/cancel"
    lookup = f"/api/model/operations/{_quoted(operation_id)}"
    body: dict[str, object] = {
        "schema_version": 2,
        "request_key": key,
        "reason": reason,
    }

    def validate(result: Mapping[str, object]) -> str:
        if _cache_operation_id("model", result) != operation_id:
            raise ControlMalformedResponse(
                "model cancellation receipt identifies another operation"
            )
        cancellation = result.get("cancellation")
        if (
            not isinstance(cancellation, Mapping)
            or cancellation.get("request_key") != key
            or cancellation.get("reason") != reason
            or result.get("state") not in {"cancelling", "cancelled"}
        ):
            raise ControlMalformedResponse(
                "model cancellation receipt identifies another cancellation"
            )
        return operation_id

    def validate_existing_cancellation(observed: Mapping[str, object]) -> str:
        if _cache_operation_id("model", observed) != operation_id:
            raise ControlMalformedResponse(
                "model cancellation lookup identifies another operation"
            )
        cancellation = observed.get("cancellation")
        if cancellation is None:
            raise ControlNotFound(404, "no cancellation is recorded for this operation")
        if not isinstance(cancellation, Mapping):
            raise ControlMalformedResponse("model cancellation lookup is malformed")
        if (
            cancellation.get("request_key") != key
            or cancellation.get("reason") != reason
        ):
            raise ControlConflict(
                409, "operation already has a different cancellation request"
            )
        return validate(observed)

    return _submit_idempotent_request(
        client,
        args,
        key=key,
        path=path,
        lookup=lookup,
        body=body,
        noun="model",
        action="cancel",
        validate=validate,
        lookup_validate=validate_existing_cancellation,
        reconnect=shlex.join(
            [
                "vonkctl",
                "model",
                "cancel",
                operation_id,
                "--yes",
                "--request-key",
                key,
                "--reason",
                reason,
            ]
        ),
    )


def _submit_recipe_cancellation(
    client: ControllerClient,
    args: argparse.Namespace,
    factory: Callable[[], str],
) -> dict[str, object]:
    """Cancel one operation through its durable identity and one safe replay."""

    if not args.yes:
        raise ValueError("recipe cancel requires --yes in noninteractive mode")
    operation_id = args.operation_id
    key = _request_key(args, factory)
    reason = " ".join(args.reason.split())
    if not reason or len(reason) > 512:
        raise ValueError("--reason must contain between 1 and 512 characters")
    args.reason = reason
    path = f"/api/recipe/operations/{_quoted(operation_id)}/cancel"
    lookup = f"/api/recipe/operations/{_quoted(operation_id)}"
    body: dict[str, object] = {
        "schema_version": 2,
        "request_key": key,
        "reason": reason,
    }

    def validate(result: Mapping[str, object]) -> str:
        if _cache_operation_id("recipe", result) != operation_id:
            raise ControlMalformedResponse(
                "recipe cancellation receipt identifies another operation"
            )
        cancellation = result.get("cancellation")
        if (
            not isinstance(cancellation, Mapping)
            or cancellation.get("cancel_request_id") != key
            or cancellation.get("reason") != reason
            or result.get("state") not in {"cancelling", "cancelled"}
        ):
            raise ControlMalformedResponse(
                "recipe cancellation receipt identifies another cancellation"
            )
        return operation_id

    def validate_existing_cancellation(observed: Mapping[str, object]) -> str:
        if _cache_operation_id("recipe", observed) != operation_id:
            raise ControlMalformedResponse(
                "recipe cancellation lookup identifies another operation"
            )
        cancellation = observed.get("cancellation")
        if cancellation is None:
            raise ControlNotFound(404, "no cancellation is recorded for this operation")
        if not isinstance(cancellation, Mapping):
            raise ControlMalformedResponse("recipe cancellation lookup is malformed")
        if (
            cancellation.get("cancel_request_id") != key
            or cancellation.get("reason") != reason
        ):
            raise ControlConflict(
                409, "operation already has a different cancellation request"
            )
        return validate(observed)

    return _submit_idempotent_request(
        client,
        args,
        key=key,
        path=path,
        lookup=lookup,
        body=body,
        noun="recipe",
        action="cancel",
        validate=validate,
        lookup_validate=validate_existing_cancellation,
        reconnect=shlex.join(
            [
                "vonkctl",
                "recipe",
                "cancel",
                operation_id,
                "--yes",
                "--request-key",
                key,
                "--reason",
                reason,
            ]
        ),
    )


def _known_http_refusal_status(error: ControlClientError) -> int | None:
    status = (
        error.status_code
        if isinstance(error, ControlHTTPError)
        else (error.context.http_status if error.context else None)
    )
    return status if status is not None and 400 <= status <= 499 else None


def _submit_idempotent_request(
    client: ControllerClient,
    args: argparse.Namespace,
    *,
    key: str,
    path: str,
    lookup: str,
    body: dict[str, object],
    noun: str,
    action: str,
    validate: Callable[[Mapping[str, object]], str],
    lookup_validate: Callable[[Mapping[str, object]], str] | None = None,
    reconnect: str,
) -> dict[str, object]:
    """Bounded submission, exact-request reconciliation, and one identical replay."""
    normal_timeout = client.request_timeout_seconds
    if not math.isfinite(normal_timeout) or normal_timeout <= 0:
        raise ValueError("request timeout must be finite and positive")
    submission = Submission(key, path, lookup, 3 * normal_timeout, action=action)
    args.submission = submission
    deadline = time.monotonic() + submission.timeout_seconds
    if not (args.global_json or getattr(args, "json", False)):
        print(
            f"Request key: {key}\nReconnect: {reconnect}", file=sys.stderr, flush=True
        )

    def request(
        method: str,
        target: str,
        stage: str,
        *,
        receipt_validator: Callable[[Mapping[str, object]], str] | None = None,
    ) -> dict[str, object]:
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ControlTransportError(
                    f"{action} submission deadline reached",
                    context=ErrorContext(
                        operation=f"{noun}.{action}.{stage}",
                        endpoint=target,
                        code="control.submission_timeout",
                        source="transport",
                        transport="timeout",
                        decision="exit",
                    ),
                )
            result = client.request(
                method,
                target,
                body if method == "POST" else None,
                timeout_seconds=min(normal_timeout, remaining),
            )
            operation_id = (receipt_validator or validate)(result)
        except BrokenPipeError:
            raise
        except (ControlClientError, OSError) as error:
            context = error.context if isinstance(error, ControlClientError) else None
            if context is None:
                context = (
                    transport_context(
                        operation=f"{noun}.{action}.{stage}",
                        endpoint=target,
                        error=error,
                    )
                    if isinstance(error, (ControlTransportError, OSError))
                    else protocol_context(
                        operation=f"{noun}.{action}.{stage}", endpoint=target
                    )
                )
            submission.failures.append({"stage": stage, **context.as_dict()})
            raise
        submission.acceptance = "accepted"
        submission.operation_id = operation_id
        return result

    retry_error: ControlHTTPError | ControlTransportError | None = None
    submission_error: ControlClientError | None = None
    retry_not_before = 0.0
    submission.acceptance = "unknown"
    try:
        return request("POST", path, "submit")
    except BrokenPipeError:
        raise
    except OSError:
        may_replay = True
    except ControlClientError as error:
        submission_error = error
        status = _known_http_refusal_status(error)
        if status is not None:
            submission.acceptance = "refused"
            raise
        if isinstance(error, (ControlMalformedResponse, ControlResponseTooLarge)):
            # Diagnose by read only. An invalid receipt never licenses a replay.
            may_replay = False
        elif isinstance(error, ControlTransportError) or (
            isinstance(error, ControlHTTPError) and 500 <= error.status_code <= 599
        ):
            may_replay = True
            if (
                isinstance(error, (ControlHTTPError, ControlTransportError))
                and error.retry_after_seconds is not None
            ):
                retry_error = error
                now = time.monotonic()
                # A delay beyond this invocation's budget needs no timer and
                # must not overflow when represented as floating-point time.
                retry_not_before = (
                    deadline
                    if error.retry_after_seconds >= deadline - now
                    else now + error.retry_after_seconds
                )
        else:
            raise

    if lookup == path:
        if not may_replay:
            if submission_error is not None:
                raise submission_error
            raise ControlMalformedResponse(f"{action} receipt could not be confirmed")
    else:
        try:
            return request("GET", lookup, "lookup", receipt_validator=lookup_validate)
        except ControlNotFound:
            if not may_replay:
                raise
    if retry_error is not None:
        now = time.monotonic()
        delay = max(0.0, retry_not_before - now)
        if delay >= deadline - now:
            raise retry_error
        if delay:
            time.sleep(delay)
    # A separate lookup's errors propagate with the original key and both safe
    # failure contexts. Neither a failed lookup nor a second lost POST loops.
    return request("POST", path, "replay")


def _follow_cache_operation(
    client: ControllerClient,
    noun: str,
    operation_id: str,
    result: dict[str, object],
    args: argparse.Namespace,
) -> dict[str, object]:
    def same_operation(observed: Mapping[str, object]) -> None:
        if _cache_operation_id(noun, observed) != operation_id:
            raise ControlMalformedResponse(
                "cache observation identifies another operation"
            )

    return _poll_path(
        client,
        f"/api/{noun}/operations/{_quoted(operation_id)}",
        result,
        args,
        validate=same_operation,
    )


def _cache_progress(
    client: ControllerClient,
    noun: str,
    args: argparse.Namespace,
    factory: Callable[[], str],
) -> dict[str, object]:
    key = getattr(args, "request_key", None)
    if key is not None:
        key = _request_key(args, factory)
        result = client.request("GET", f"/api/{noun}/requests/{_quoted(key)}")
        key_field = (
            "request_id" if noun == "recipe" and "kind" in result else "request_key"
        )
        if result.get(key_field) != key:
            raise ControlMalformedResponse("cache lookup identifies another request")
        operation_id = _cache_operation_id(noun, result)
    else:
        operation_id = args.operation_id
        result = client.request(
            "GET", f"/api/{noun}/operations/{_quoted(operation_id)}"
        )
        if _cache_operation_id(noun, result) != operation_id:
            raise ControlMalformedResponse("cache lookup identifies another operation")
    return (
        _follow_cache_operation(client, noun, operation_id, result, args)
        if args.follow
        else result
    )


def _follow_mutation(
    client: ControllerClient,
    noun: str,
    result: dict[str, object],
    args: argparse.Namespace,
) -> dict[str, object]:
    """Follow a model/recipe mutation through its noun-owned operation view."""
    operation_id = _cache_operation_id(noun, result)
    if getattr(args, "detach", False):
        return result
    return _follow_cache_operation(client, noun, operation_id, result, args)


def _watch_resource(
    client: ControllerClient,
    path: str,
    result: dict[str, object],
    args: argparse.Namespace,
    *,
    query: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if not getattr(args, "watch", False):
        return result
    return _poll_path(client, path, result, args, query=query)


def _log_follow_complete(observed: Mapping[str, object]) -> bool:
    return (
        observed.get("follow") is False
        or observed.get("complete") is True
        or observed.get("closed") is True
        or _state(observed) in _TERMINAL_STATES
    )


def _follow_loginfo(
    client: ControllerClient,
    path: str,
    result: dict[str, object],
    args: argparse.Namespace,
    query: Mapping[str, object],
    node_id: str,
) -> dict[str, object]:
    def same_node(observed: Mapping[str, object]) -> None:
        if observed.get("node_id") != node_id:
            raise ControlMalformedResponse(
                "fleet log observation identifies another Spark"
            )

    same_node(result)
    if not getattr(args, "follow", False):
        return result
    # The owner says this is retained evidence rather than a live stream. Keep
    # that snapshot as evidence without spending the caller's follow budget or
    # presenting a local timeout as a remote log-follow failure.
    if result.get("follow") is False:
        return result
    # The log tail is the same bounded observation as every other follow path;
    # keeping one loop means a dropped connection is tolerated identically here
    # instead of being a second, weaker implementation.
    return _poll_path(
        client,
        path,
        result,
        args,
        query=query,
        terminal=_log_follow_complete,
        validate=same_node,
    )


def _overview(
    client: ControllerClient, noun: str, args: argparse.Namespace
) -> dict[str, object]:
    if noun == "fleet":
        return client.request(
            "GET",
            "/api/fleet",
            query=_fleet_query(args),
        )
    if noun == "model":
        return client.request("GET", "/api/model")
    if noun == "recipe":
        return client.request("GET", "/api/recipe")
    return client.request("GET", f"/api/profile/{_profile_number(args)}")


def _fleet_query(args: argparse.Namespace) -> Mapping[str, object] | None:
    return (
        _query(
            search=args.search,
            health=args.health,
            warnings_only=args.warnings_only,
            sort=args.sort,
        )
        or None
    )


def _fleet_selector(args: argparse.Namespace) -> str:
    """Return the validated Spark selector of a fleet action that requires one."""

    selector = getattr(args, "selector", None)
    if not isinstance(selector, str):
        raise TypeError("fleet action requires a Spark selector")
    return selector


def _confirm_action(args: argparse.Namespace, message: str) -> None:
    if args.yes:
        if not (getattr(args, "global_json", False) or getattr(args, "json", False)):
            print(terminal_text(message), file=sys.stderr)
        return
    if (
        getattr(args, "no_input", False)
        or getattr(args, "global_json", False)
        or getattr(args, "json", False)
        or not sys.stdin.isatty()
        or not sys.stderr.isatty()
    ):
        raise ValueError(f"{message} Pass --yes to confirm in noninteractive mode")
    print(terminal_text(message) + " [y/N] ", end="", file=sys.stderr, flush=True)
    if sys.stdin.readline(1024).strip().casefold() not in {"y", "yes"}:
        raise ValueError("action was not confirmed")


def _deliver_enrollment(
    args: argparse.Namespace, client: ControllerClient, factory: Callable[[], str]
) -> dict[str, object]:
    identity = _request_key(args, factory)
    payload: dict[str, object] = {"request_key": identity}
    target: dict[str, object]
    if args.fleet_action == "enroll":
        payload.update(name=args.name, ttl_seconds=args.ttl_seconds)
        validate_control_document("FleetEnrollRequest", payload)
        path = "/api/fleet/enroll"
        target = {"display_name": args.name}
    else:
        validate_control_document("FleetReenrollRequest", payload)
        node = client.request("GET", f"/api/fleet/{_quoted(_fleet_selector(args))}")
        node_id = node.get("id")
        if (
            not isinstance(node_id, str)
            or re.fullmatch(r"spk_[0-9a-f]{32}", node_id) is None
        ):
            raise ValueError("Spark response has no canonical identity")
        target = {"node_id": node_id, "display_name": node.get("display_name", node_id)}
        _confirm_action(
            args,
            f"Authorize a replacement certificate for {node.get('display_name', node_id)} ({node_id})? "
            "The one-time grant permits replacing this Spark identity when consumed.",
        )
        path = f"/api/fleet/{node_id}/re-enroll"
    status_path = f"/api/fleet/enrollments/{identity}"
    receipt: dict[str, object] = {
        "id": identity,
        **target,
        "delivery": {"status": "pending"},
        "output": str(args.output.absolute()),
        "recovery": [
            f"vonkctl fleet enrollment status {identity}",
            f"vonkctl fleet enrollment revoke {identity} --yes",
        ],
    }
    issued = False
    submission_started = False
    try:
        with PrivateOutput(args.output) as destination:
            # This durable, nonsecret receipt identifies a request even if the
            # process dies before the Controller's response reaches it.
            destination.write(receipt)
            destination.retain_on_failure = True
            try:
                submission_started = True
                response = validate_control_document(
                    "FleetActionResponse", client.request("POST", path, payload)
                )
                grant = validate_control_document(
                    "EnrollmentGrantResponse", response.get("grant")
                )
                if grant["id"] != identity:
                    raise ValueError("issued grant identity does not match the request")
                issued = True
                destination.write({"id": identity, **grant})
                # Closing is part of delivery: a failed flush is reconciled here too.
                destination.stream.close()
                return {
                    **receipt,
                    "delivery": {"status": "delivered"},
                    "expires_at": grant["expires_at"],
                    "purpose": grant["purpose"],
                    "recovery": [f"vonkctl fleet enrollment status {identity}"],
                }
            except (
                ControlClientError,
                OSError,
                TypeError,
                ValueError,
                KeyboardInterrupt,
            ) as error:
                receipt["delivery"] = {
                    "status": "interrupted"
                    if isinstance(error, KeyboardInterrupt)
                    else "failed"
                }
                receipt["error"] = (
                    "Enrollment grant delivery was not confirmed; inspect the original grant before creating another."
                )
                receipt["error_type"] = "enrollment_delivery"
                receipt["cause"] = type(error).__name__
                refusal_status = (
                    _known_http_refusal_status(error)
                    if isinstance(error, ControlClientError)
                    else None
                )
                if not issued and refusal_status is not None:
                    if isinstance(error, (ControlForbidden, ControlUnauthorized)):
                        receipt["error"] = "Controller authorization denied enrollment."
                        receipt["reconciliation"] = "issuance denied"
                    else:
                        receipt["error"] = (
                            f"Controller refused enrollment (HTTP {refusal_status})."
                        )
                        receipt["reconciliation"] = "issuance refused"
                else:
                    try:
                        observed = validate_control_document(
                            "EnrollmentGrantStatus",
                            client.request("GET", status_path, timeout_seconds=5),
                        )
                        receipt["grant_status"] = observed
                        if issued and observed["state"] == "pending":
                            receipt["grant_status"] = validate_control_document(
                                "EnrollmentGrantStatus",
                                client.request(
                                    "POST", status_path + "/revoke", timeout_seconds=5
                                ),
                            )
                    except (
                        ControlClientError,
                        OSError,
                        TypeError,
                        ValueError,
                        KeyboardInterrupt,
                    ):
                        receipt["reconciliation"] = "unconfirmed"
                try:
                    destination.write(receipt)
                except (OSError, TypeError, ValueError):
                    receipt["output_status"] = "unusable; inspect grant status"
                raise EnrollmentDeliveryError(
                    receipt, isinstance(error, KeyboardInterrupt)
                ) from None
    except (OSError, KeyboardInterrupt) as error:
        receipt["delivery"] = {
            "status": "unconfirmed" if submission_started else "not_issued"
        }
        receipt["error_type"] = "enrollment_delivery"
        receipt["cause"] = type(error).__name__
        if submission_started:
            receipt["error"] = (
                "Enrollment delivery was interrupted; inspect the original grant."
            )
            receipt["reconciliation"] = "unconfirmed"
        else:
            receipt["error"] = (
                "Could not prepare the private output file. Choose a new writable "
                "destination; enrollment was not attempted."
            )
            receipt["reconciliation"] = "issuance not attempted"
            receipt["recovery"] = []
        raise EnrollmentDeliveryError(
            receipt, isinstance(error, KeyboardInterrupt)
        ) from None


def _fleet(
    args: argparse.Namespace,
    client: ControllerClient,
    factory: Callable[[], str],
) -> dict[str, object]:
    action = getattr(args, "fleet_action", None)
    if action == "enrollment":
        path = f"/api/fleet/enrollments/{args.grant_id}"
        if args.enrollment_action == "revoke":
            _confirm_action(args, f"Revoke unused enrollment grant {args.grant_id}?")
            return client.request("POST", path + "/revoke")
        return client.request("GET", path)
    if action == "progress":
        path = f"/api/jobs/{_quoted(args.job_id)}"
        result = client.request("GET", path)

        def same_job(observed: Mapping[str, object]) -> None:
            if observed.get("id") != args.job_id:
                raise ControlMalformedResponse(
                    "fleet progress observation identifies another job"
                )

        same_job(result)
        return (
            _poll_path(client, path, result, args, validate=same_job)
            if args.follow
            else result
        )
    if action == "activity":
        query = _query(
            cursor=args.cursor,
            limit=args.limit,
            state=args.state,
            node_id=args.target,
            request_id=args.request_id,
        )
        return client.request("GET", "/api/operations", query=query or None)
    if action == "resume":
        job_id = args.job_id
        path = f"/api/jobs/{_quoted(job_id)}"
        current = client.request("GET", path)
        recovery = current.get("recovery")
        actions = recovery.get("actions") if isinstance(recovery, Mapping) else None
        if (
            current.get("id") != job_id
            or current.get("state") != "waiting-for-operator"
            or not isinstance(actions, list)
            or "resume" not in actions
        ):
            raise ValueError(
                f"job {job_id} has no currently advertised authorized resume action"
            )
        _confirm_action(
            args, f"Resume job {job_id} using its advertised recovery action?"
        )
        accepted = client.request(
            "POST",
            f"{path}/resume",
            {"disposition": "resume"},
        )
        if accepted.get("id") != job_id:
            raise ControlMalformedResponse(
                "fleet resume receipt identifies another job"
            )
        return accepted
    if action is None:
        return _watch_resource(
            client,
            "/api/fleet",
            _overview(client, "fleet", args),
            args,
            query=_fleet_query(args),
        )
    if action == "node-profile":
        deadline = time.monotonic() + args.timeout_seconds
        node_id = _resolve_spark_selectors(client, [args.selector], deadline=deadline)[
            0
        ]
        result = client.request(
            "GET",
            f"/api/fleet/{_quoted(node_id)}",
            timeout_seconds=_selection_remaining(deadline),
        )
        _selection_remaining(deadline)
        if result.get("id") != node_id:
            raise ValueError("fleet detail response does not match the selected Spark")
        return result
    if action == "detail":
        selector = _fleet_selector(args)
        query = (
            _query(
                metrics=args.metrics,
                range=args.range,
                device=args.device,
                interface=args.interface,
                run=args.run,
                capabilities=args.capabilities,
                technical=args.technical,
            )
            or None
        )
        result = client.request(
            "GET",
            f"/api/fleet/{_quoted(selector)}",
            query=query,
        )
        return _watch_resource(
            client, f"/api/fleet/{_quoted(selector)}", result, args, query=query
        )
    if action == "rename":
        return client.request(
            "POST",
            f"/api/fleet/{_quoted(_fleet_selector(args))}/rename",
            {"display_name": args.new_name},
        )
    if action in {"enroll", "re-enroll"}:
        return _deliver_enrollment(args, client, factory)
    if action == "remove":
        deadline = time.monotonic() + 30
        node_id = _resolve_spark_selectors(
            client, [_fleet_selector(args)], deadline=deadline
        )[0]
        if re.fullmatch(r"spk_[0-9a-f]{32}", node_id) is None:
            raise ControlMalformedResponse(
                "fleet removal target has no canonical Spark identity"
            )
        node = client.request(
            "GET",
            f"/api/fleet/{_quoted(node_id)}",
            timeout_seconds=_selection_remaining(deadline),
        )
        _selection_remaining(deadline)
        if node.get("id") != node_id:
            raise ControlMalformedResponse(
                "fleet removal detail identifies another Spark"
            )
        display_name = node.get("display_name")
        if not isinstance(display_name, str) or not display_name.strip():
            display_name = node_id
        _confirm_action(
            args,
            f"Revoke and remove Spark {display_name} ({node_id}) from the fleet?",
        )
        result = client.request(
            "POST",
            f"/api/fleet/{_quoted(node_id)}/remove",
            None,
        )
        if result.get("action") != "remove" or result.get("node_id") != node_id:
            raise ControlMalformedResponse(
                "fleet removal receipt identifies another Spark"
            )
        return result
    if action == "upgrade":
        if args.all == bool(args.selector):
            raise ValueError("fleet upgrade requires either a Spark selector or --all")
        if args.all:
            scope = "all Sparks in the current fleet"
        else:
            resolved = _resolve_spark_selectors(
                client,
                [cast(str, args.selector)],
                deadline=time.monotonic() + _bounded_timeout(args),
            )
            if len(resolved) != 1:
                raise SelectorError(
                    "fleet upgrade selector did not resolve to one Spark"
                )
            args.selector = resolved[0]
            scope = f"Spark {args.selector}"
        _confirm_action(args, f"Upgrade {scope} one at a time?")
        result = _submit_fleet_upgrade(args, client, factory)
        if args.detach:
            return result
        job_id = result.get("operation_id")
        if not isinstance(job_id, str) or not job_id:
            raise ControlMalformedResponse("fleet upgrade has no durable job identity")

        def same_job(observed: Mapping[str, object]) -> None:
            if observed.get("action") == "upgrade":
                if (
                    observed.get("operation_id") != job_id
                    or observed.get("request_key") != args.request_key
                ):
                    raise ControlMalformedResponse(
                        "fleet upgrade receipt identifies another request"
                    )
                return
            if (
                observed.get("id") != job_id
                or observed.get("kind") != "agent-upgrade"
                or observed.get("targets") != result.get("targets")
            ):
                raise ControlMalformedResponse(
                    "fleet upgrade observation identifies another job"
                )
            operations = observed.get("operations")
            if isinstance(operations, list):
                active = [
                    item
                    for item in operations
                    if isinstance(item, Mapping)
                    and item.get("state")
                    in {"queued", "running", "waiting-for-operator"}
                ]
                if len(active) > 1:
                    raise ControlMalformedResponse(
                        "fleet upgrade job has more than one active Spark"
                    )
            progress = observed.get("progress")
            if (
                isinstance(progress, Mapping)
                and type(progress.get("running")) is int
                and progress["running"] > 1
            ):
                raise ControlMalformedResponse(
                    "fleet upgrade job reports concurrent Spark upgrades"
                )

        args.follow = True
        args.fleet_action = "progress"
        return _poll_path(
            client,
            f"/api/jobs/{_quoted(job_id)}",
            result,
            args,
            query={"limit": 100},
            terminal=lambda observed: (
                _state(observed) in _TERMINAL_STATES
                or _state(observed) == "waiting-for-operator"
            ),
            validate=same_job,
        )
    if action == "loginfo":
        selector = _fleet_selector(args)
        query = _query(
            since=_log_since(args.since),
            lines=args.lines,
            recipe=args.recipe,
            source=args.source,
            follow=args.follow,
        )
        node_id = (
            selector
            if re.fullmatch(r"spk_[0-9a-f]{32}", selector) is not None
            else _resolve_spark_selectors(client, [selector])[0]
        )
        path = f"/api/fleet/{_quoted(node_id)}/loginfo"
        result = client.request("GET", path, query=query or None)
        return _follow_loginfo(client, path, result, args, query, node_id)
    raise ValueError(f"unsupported fleet action: {action}")


def _library_query(args: argparse.Namespace, *, recipe: bool) -> dict[str, object]:
    values: dict[str, object] = {
        name: getattr(args, name)
        for name in (
            "search",
            "usage",
            "family",
            "version",
            "quantization",
            "publisher",
            "alignment",
            "updated_since",
            "sort",
            "limit",
            "cursor",
        )
    }
    if recipe:
        values.update(
            model=getattr(args, "model", []),
            all_models=args.all_models,
            ready=getattr(args, "ready", None),
            fits_fleet=getattr(args, "fits_fleet", None),
            sparks=getattr(args, "sparks", []),
        )
    return _query(**values)


def _model(
    args: argparse.Namespace,
    client: ControllerClient,
    factory: Callable[[], str],
) -> dict[str, object]:
    action = getattr(args, "model_action", None)
    if action == "progress":
        return _cache_progress(client, "model", args, factory)
    if action == "cancel":
        result = _submit_model_cancellation(client, args, factory)
        return _follow_mutation(client, "model", result, args)
    if action is None:
        return _watch_resource(
            client, "/api/model", _overview(client, "model", args), args
        )
    if action == "library":
        return client.request(
            "GET",
            "/api/model/library",
            query=_library_query(args, recipe=False) or None,
        )
    if action == "detail":
        result = client.request(
            "GET",
            f"/api/model/{_quoted(args.selector)}",
            query=_query(technical=args.technical) or None,
        )
        return _watch_resource(
            client,
            f"/api/model/{_quoted(args.selector)}",
            result,
            args,
            query=_query(technical=args.technical) or None,
        )
    if action == "download":
        result = _submit_cache_request(client, "model", args, factory)
        return _follow_mutation(client, "model", result, args)
    if action == "remove":
        result = _remove_model(client, args, factory)
        if args.review:
            return result
        return _follow_mutation(client, "model", result, args)
    raise ValueError(f"unsupported model action: {action}")


def _run_switch_request_operation(
    send_request: Callable[..., dict[str, object]],
    request_key: str,
    *,
    installation_id: str,
    expected_plan_digest: str | None,
) -> dict[str, object] | None:
    """Resolve one accepted Run/Switch request before considering a resubmit."""
    page = validate_control_document(
        "OperationsResponse",
        send_request(
            "GET",
            "/api/operations",
            query={"request_id": request_key, "limit": 100},
        ),
    )
    operations = page.get("operations")
    total = page.get("total")
    next_cursor = page.get("next_cursor")
    if (
        not isinstance(operations, list)
        or type(total) is not int
        or next_cursor is not None
        or total != len(operations)
    ):
        raise ControlMalformedResponse(
            "request lookup returned an invalid operation page"
        )
    if total == 0 and not operations:
        return None
    summaries = [item for item in operations if isinstance(item, Mapping)]
    if len(summaries) != len(operations):
        raise ControlMalformedResponse("request lookup contains an invalid operation")
    owners = [item.get("owner") for item in summaries]
    if any(
        not isinstance(owner, Mapping)
        or owner.get("kind") != "job"
        or owner.get("request_id") != request_key
        or not isinstance(owner.get("id"), str)
        for owner in owners
    ):
        raise ControlMalformedResponse("request lookup has no exact durable owner")
    owner_ids = {cast(Mapping[str, object], owner).get("id") for owner in owners}
    owner_id = next(iter(owner_ids)) if len(owner_ids) == 1 else None
    parents = [
        item
        for item in summaries
        if item.get("kind") == "recipe.cleanup.v2" and item.get("id") == owner_id
    ]
    if not isinstance(owner_id, str) or len(parents) != 1:
        raise ControlConflict(409, "request UUID is already owned by another operation")
    operation_id = owner_id
    observed = validate_control_document(
        "RunSwitchOperation",
        send_request("GET", f"/api/run-switch/operations/{_quoted(operation_id)}"),
    )
    _validate_installation_reconcile_operation(
        observed,
        operation_id=operation_id,
        request_key=request_key,
        plan_digest=expected_plan_digest,
        installation_id=installation_id,
    )
    return observed


def _validate_installation_reconcile_operation(
    value: Mapping[str, object],
    *,
    operation_id: str | None,
    request_key: str,
    plan_digest: str | None,
    installation_id: str,
) -> str:
    if (
        (operation_id is not None and value.get("operation_id") != operation_id)
        or value.get("request_key") != request_key
        or value.get("kind") != "recipe.cleanup.v2"
        or value.get("action") != "cleanup"
        or value.get("cleanup_mode") != "reconcile"
        or value.get("installation_id") != installation_id
        or (plan_digest is not None and value.get("plan_digest") != plan_digest)
    ):
        raise ControlMalformedResponse(
            "reconciliation status identifies another request or reviewed plan"
        )
    returned_id = value.get("operation_id")
    if not isinstance(returned_id, str) or not returned_id:
        raise ControlMalformedResponse(
            "reconciliation status has no operation identity"
        )
    return returned_id


def _follow_installation_reconciliation(
    client: ControllerClient,
    operation: dict[str, object],
    args: argparse.Namespace,
    *,
    request_key: str,
    plan_digest: str,
) -> dict[str, object]:
    operation_id = _validate_installation_reconcile_operation(
        operation,
        operation_id=None,
        request_key=request_key,
        plan_digest=plan_digest,
        installation_id=cast(str, args.installation_id),
    )
    if getattr(args, "detach", False):
        return operation

    def same_operation(observed: Mapping[str, object]) -> None:
        _validate_installation_reconcile_operation(
            observed,
            operation_id=operation_id,
            request_key=request_key,
            plan_digest=plan_digest,
            installation_id=cast(str, args.installation_id),
        )

    return _poll_path(
        client,
        f"/api/run-switch/operations/{_quoted(operation_id)}",
        operation,
        args,
        validate=same_operation,
    )


def _recipe_installation_reconcile(
    client: ControllerClient,
    args: argparse.Namespace,
    factory: Callable[[], str],
) -> dict[str, object]:
    installation_id = cast(str, args.installation_id)
    preview_path = (
        f"/api/recipe/installations/{_quoted(installation_id)}/reconcile/preview"
    )
    apply_path = f"/api/recipe/installations/{_quoted(installation_id)}/reconcile"
    preview_body = {
        "schema_version": 2,
        "installation_id": installation_id,
        "cleanup_mode": "reconcile",
    }
    if getattr(args, "review", False):
        if (
            getattr(args, "yes", False)
            or getattr(args, "review_digest", None) is not None
            or getattr(args, "request_key", None) is not None
            or getattr(args, "detach", False)
        ):
            raise ValueError(
                "installation reconcile --review cannot be combined with consent or request flags"
            )
        args.outcome_context = "read"
        return validate_control_document(
            "RunSwitchPlan", client.request("POST", preview_path, preview_body)
        )
    if not getattr(args, "yes", False):
        raise ValueError(
            "installation reconcile requires --yes; review with --review first"
        )
    reviewed_digest = getattr(args, "review_digest", None)
    if not isinstance(reviewed_digest, str):
        raise TypeError(
            "installation reconcile requires --review-digest SHA256 and --yes"
        )

    key = _request_key(args, factory)
    request_timeout = client.request_timeout_seconds
    if not math.isfinite(request_timeout) or request_timeout <= 0:
        raise ValueError("request timeout must be finite and positive")
    submission = Submission(
        key,
        apply_path,
        f"/api/operations?request_id={key}",
        3 * request_timeout,
        action="reconcile",
    )
    args.submission = submission
    submission_deadline = time.monotonic() + submission.timeout_seconds
    reconnect = shlex.join(
        [
            "vonkctl",
            "recipe",
            "installation",
            "reconcile",
            installation_id,
            "--review-digest",
            reviewed_digest,
            "--request-key",
            key,
            "--yes",
        ]
    )
    if not (args.global_json or getattr(args, "json", False)):
        print(
            f"Request key: {key}\nReconnect: {reconnect}", file=sys.stderr, flush=True
        )

    def request(
        method: str,
        path: str,
        body: dict[str, object] | None = None,
        *,
        query: Mapping[str, object] | None = None,
    ):
        remaining = submission_deadline - time.monotonic()
        if remaining <= 0:
            raise ControlTransportError("installation reconciliation deadline reached")
        return client.request(
            method,
            path,
            body,
            query=query,
            timeout_seconds=min(request_timeout, remaining),
        )

    # A supplied UUID may already own a completed or in-flight cleanup. Resolve
    # that exact owner before consulting mutable current evidence.
    existing = _run_switch_request_operation(
        request,
        key,
        installation_id=installation_id,
        expected_plan_digest=reviewed_digest,
    )
    if existing is not None:
        submission.acceptance = "accepted"
        submission.operation_id = cast(str, existing["operation_id"])
        return _follow_installation_reconciliation(
            client,
            existing,
            args,
            request_key=key,
            plan_digest=reviewed_digest,
        )

    preview = validate_control_document(
        "RunSwitchPlan", request("POST", preview_path, preview_body)
    )
    if (
        preview.get("action") != "cleanup"
        or preview.get("cleanup_mode") != "reconcile"
        or preview.get("installation_id") != installation_id
        or preview.get("plan_digest") != reviewed_digest
    ):
        raise ControlConflict(
            409,
            "installation reconciliation plan changed; review the current plan again",
        )
    if preview.get("allowed") is not True:
        reasons = preview.get("blockers")
        codes: list[str] = []
        if isinstance(reasons, list):
            for item in reasons:
                if isinstance(item, Mapping):
                    code = item.get("code")
                    if isinstance(code, str):
                        codes.append(code)
        detail = "installation reconciliation is blocked" + (
            f": {', '.join(codes[:6])}" if codes else "; review the current plan"
        )
        raise ControlConflict(409, detail)
    body = {
        **preview_body,
        "plan_digest": reviewed_digest,
        "request_key": key,
    }

    submission.acceptance = "unknown"
    try:
        raw = request("POST", apply_path, body)
        operation = validate_control_document("RunSwitchOperation", raw)
        operation_id = _validate_installation_reconcile_operation(
            operation,
            operation_id=None,
            request_key=key,
            plan_digest=reviewed_digest,
            installation_id=installation_id,
        )
    except (ControlTransportError, ControlUnavailable, OSError) as error:
        submission.failures.append({"stage": "submit", "error": type(error).__name__})
        # A successful, fresh lookup is the only basis for either reconnecting
        # to the old operation or replaying these exact bytes once.
        existing = _run_switch_request_operation(
            request,
            key,
            installation_id=installation_id,
            expected_plan_digest=reviewed_digest,
        )
        if existing is not None:
            operation = existing
            operation_id = cast(str, existing["operation_id"])
        else:
            raw = request("POST", apply_path, body)
            operation = validate_control_document("RunSwitchOperation", raw)
            operation_id = _validate_installation_reconcile_operation(
                operation,
                operation_id=None,
                request_key=key,
                plan_digest=reviewed_digest,
                installation_id=installation_id,
            )
    except ControlHTTPError as error:
        if error.status_code < 500:
            raise
        submission.failures.append({"stage": "submit", "error": type(error).__name__})
        existing = _run_switch_request_operation(
            request,
            key,
            installation_id=installation_id,
            expected_plan_digest=reviewed_digest,
        )
        if existing is not None:
            operation = existing
            operation_id = cast(str, existing["operation_id"])
        else:
            raw = request("POST", apply_path, body)
            operation = validate_control_document("RunSwitchOperation", raw)
            operation_id = _validate_installation_reconcile_operation(
                operation,
                operation_id=None,
                request_key=key,
                plan_digest=reviewed_digest,
                installation_id=installation_id,
            )
    except (ControlMalformedResponse, ControlResponseTooLarge):
        # Inspect only. A malformed receipt never licenses a resubmission.
        existing = _run_switch_request_operation(
            request,
            key,
            installation_id=installation_id,
            expected_plan_digest=reviewed_digest,
        )
        if existing is None:
            raise
        operation = existing
        operation_id = cast(str, existing["operation_id"])

    submission.acceptance = "accepted"
    submission.operation_id = operation_id
    return _follow_installation_reconciliation(
        client,
        operation,
        args,
        request_key=key,
        plan_digest=reviewed_digest,
    )


def _recipe(
    args: argparse.Namespace,
    client: ControllerClient,
    factory: Callable[[], str],
) -> dict[str, object]:
    action = getattr(args, "recipe_action", None)
    if action == "progress":
        return _cache_progress(client, "recipe", args, factory)
    if action == "job":
        if not isinstance(client, ArtifactJobClient):
            raise ControlClientError(
                "artifact byte transfer is unavailable in this CLI client"
            )
        return run_artifact_job(
            args,
            client,
            factory,
            request_key=_request_key,
            quote=_quoted,
            poll=_poll_path,
        )
    if action is None:
        return _watch_resource(
            client, "/api/recipe", _overview(client, "recipe", args), args
        )
    if action == "library":
        return client.request(
            "GET",
            "/api/recipe/library",
            query=_library_query(args, recipe=True) or None,
        )
    if action == "detail":
        result = client.request(
            "GET",
            f"/api/recipe/{_quoted(args.selector)}",
            query=_query(technical=args.technical) or None,
        )
        return _watch_resource(
            client,
            f"/api/recipe/{_quoted(args.selector)}",
            result,
            args,
            query=_query(technical=args.technical) or None,
        )
    if action == "download":
        result = _submit_cache_request(client, "recipe", args, factory)
        return _follow_mutation(client, "recipe", result, args)
    if action == "update":
        if args.all == bool(args.selector):
            raise ValueError("recipe update requires a selector or --all, but not both")
        result = _submit_cache_request(client, "recipe", args, factory)
        return _follow_mutation(client, "recipe", result, args)
    if action == "remove":
        result = _remove_recipe(client, args, factory)
        if args.review:
            return result
        return _follow_mutation(client, "recipe", result, args)
    if action == "cancel":
        if not args.yes:
            raise ValueError("recipe cancel requires --yes in noninteractive mode")
        result = _submit_recipe_cancellation(client, args, factory)
        return _follow_mutation(client, "recipe", result, args)
    if action == "installation":
        if getattr(args, "installation_action", None) != "reconcile":
            raise ValueError("unsupported recipe installation action")
        return _recipe_installation_reconcile(client, args, factory)
    raise ValueError(f"unsupported recipe action: {action}")


def _spark_id_list(value: object) -> list[str]:
    """Validate a decoded assignment ``spark_ids`` field without coercion."""

    if not isinstance(value, list):
        raise TypeError("profile assignment spark_ids must be an array of Spark IDs")
    identifiers: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise TypeError(
                "profile assignment spark_ids must be an array of Spark IDs"
            )
        identifiers.append(item)
    return identifiers


def _profile_save(
    args: argparse.Namespace,
    client: ControllerClient,
    definition: Mapping[str, object],
    *,
    current_revision: int,
) -> dict[str, object]:
    expected = (
        args.expected_revision
        if args.expected_revision is not None
        else current_revision
    )
    if expected != current_revision:
        raise ValueError(
            f"profile revision conflict: expected {expected}, read {current_revision}"
        )
    body = validate_control_document(
        "FleetProfileInput", {**definition, "expected_revision": expected}
    )
    result = client.request("PUT", f"/api/profile/{_profile_number(args)}", body)
    args.profile_saved = True
    return result


def _profile_authoring(
    args: argparse.Namespace, client: ControllerClient
) -> dict[str, object]:
    action = args.profile_action
    selection_deadline = (
        time.monotonic() + args.timeout_seconds if action in {"add", "remove"} else None
    )
    if action == "import":
        definition = validate_control_document(
            "FleetProfileDefinition", read_json_document(args.file)
        )
        return _profile_save(
            args, client, definition, current_revision=args.expected_revision
        )
    current = validate_control_document(
        "FleetProfileDefinitionView",
        client.request(
            "GET",
            f"/api/profile/{_profile_number(args)}/definition",
            timeout_seconds=(
                _selection_remaining(selection_deadline)
                if selection_deadline is not None
                else None
            ),
        ),
    )
    current_revision = current["revision"]
    if type(current_revision) is not int:
        raise TypeError("profile revision is invalid")
    definition = copy.deepcopy(
        validate_control_document("FleetProfileDefinition", current["definition"])
    )
    if action == "export":
        if args.output is not None:
            write_private_document(args.output, definition)
            return {
                "profile": _profile_number(args),
                "revision": current_revision,
                "output": str(args.output),
            }
        args.document_output = True
        return definition
    assignments = cast(list[dict[str, object]], definition.get("assignments", []))
    if action == "name":
        definition["name"] = args.name
    elif action == "configure":
        if (
            not any(
                value is not None
                for value in (args.description, args.retention, args.favorite)
            )
            and not args.label
            and not args.remove_label
        ):
            raise ValueError("profile configure requires at least one metadata change")
        if args.description is not None:
            definition["description"] = args.description
        if args.retention is not None:
            definition["installation_policy"] = args.retention
        if args.favorite is not None:
            definition["favorite"] = args.favorite == "true"
        labels = dict(cast(dict[str, str], definition.get("labels", {})))
        edits: dict[str, str] = {}
        for edit in args.label:
            key, separator, value = edit.partition("=")
            if not separator or not key or key in edits or key in args.remove_label:
                raise ValueError(
                    "label edits require unique KEY=VALUE values without conflicting removals"
                )
            edits[key] = value
        if len(set(args.remove_label)) != len(args.remove_label):
            raise ValueError("label removals must be unique")
        for key in args.remove_label:
            if key not in labels:
                raise ValueError(f"profile has no label named {key}")
            del labels[key]
        labels.update(edits)
        definition["labels"] = labels
    elif action == "add":
        recipe_selector = _resolve_recipe_selector(
            client, args.recipe_selector, deadline=selection_deadline
        )
        spark_ids = _resolve_spark_selectors(
            client, list(args.spark), deadline=selection_deadline
        )
        matching = [
            assignment
            for assignment in assignments
            if assignment["recipe_selector"] == recipe_selector
            and assignment.get("assignment_name") == args.assignment_name
        ]
        if len(matching) > 1:
            raise SelectorError("ambiguous assignment; select a unique assignment name")
        if matching:
            assignment = matching[0]
            assignment["spark_ids"] = sorted(
                set(_spark_id_list(assignment["spark_ids"])) | set(spark_ids)
            )
        else:
            assignment = {"recipe_selector": recipe_selector, "spark_ids": spark_ids}
            if args.assignment_name is not None:
                assignment["assignment_name"] = args.assignment_name
            assignments.append(assignment)
        if args.model_variant is not None:
            assignment["model_variant"] = args.model_variant
        if args.desired_state is not None:
            assignment["desired_state"] = args.desired_state
        definition["assignments"] = assignments
    elif action == "remove":
        target = args.assignment.casefold()
        matching = [
            assignment
            for assignment in assignments
            if str(assignment.get("assignment_name", "")).casefold() == target
        ]
        if not matching:
            recipe = _resolve_recipe_selector(
                client, args.assignment, deadline=selection_deadline
            )
            matching = [
                assignment
                for assignment in assignments
                if assignment["recipe_selector"] == recipe
            ]
        spark_ids = _resolve_spark_selectors(
            client, list(args.spark), deadline=selection_deadline
        )
        if spark_ids:
            matching = [
                assignment
                for assignment in matching
                if set(spark_ids) <= set(_spark_id_list(assignment["spark_ids"]))
            ]
        if len(matching) != 1:
            raise SelectorError(
                "assignment is absent or ambiguous; choose an exact assignment name or Spark group"
            )
        removed = matching[0]
        remaining = (
            [
                spark
                for spark in _spark_id_list(removed["spark_ids"])
                if spark not in spark_ids
            ]
            if spark_ids
            else []
        )
        if remaining:
            removed["spark_ids"] = remaining
        else:
            assignments.remove(removed)
        definition["assignments"] = assignments
    if selection_deadline is not None:
        _selection_remaining(selection_deadline)
    return _profile_save(args, client, definition, current_revision=current_revision)


def _selection_remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SelectorError(
            "selection deadline expired; nothing was saved. Retry with a larger "
            "--timeout-seconds value (maximum 300)."
        )
    return remaining


def _recipe_rows(
    client: ControllerClient, *, deadline: float
) -> Iterator[Mapping[str, object]]:
    """Read every page within one budget, without retaining the full catalog."""

    cursor: str | None = None
    seen_cursors: set[str] = set()
    seen_selectors: set[str] = set()
    while True:
        query: dict[str, object] = {
            "all_models": True,
            "limit": 512,
            "sort": "name",
            "assess": False,
        }
        if cursor is not None:
            query["cursor"] = cursor
        payload = client.request(
            "GET",
            "/api/recipe/library",
            query=query,
            timeout_seconds=_selection_remaining(deadline),
        )
        _selection_remaining(deadline)
        raw_recipes = payload.get("recipes")
        if not isinstance(raw_recipes, list):
            raise TypeError("recipe library response does not contain recipes")
        for row in raw_recipes:
            _selection_remaining(deadline)
            if not isinstance(row, Mapping) or not isinstance(
                row.get("identity"), Mapping
            ):
                raise TypeError("recipe library response contains an invalid recipe")
            selector = row.get("selector")
            if not isinstance(selector, str) or not selector:
                raise ValueError("recipe library response contains an invalid selector")
            if selector.casefold() in seen_selectors:
                raise SelectorError(
                    "recipe library repeated a selector; retry the complete selection"
                )
            seen_selectors.add(selector.casefold())
            yield row
        next_cursor = payload.get("next_cursor")
        if next_cursor is None:
            return
        if not isinstance(next_cursor, str) or not next_cursor:
            raise TypeError("recipe library response contains an invalid cursor")
        if next_cursor in seen_cursors:
            raise SelectorError(
                "recipe library cursor repeated; selection stopped without saving"
            )
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def _resolve_recipe_selector(
    client: ControllerClient, requested: str, *, deadline: float | None = None
) -> str:
    """Resolve an accepted identity before considering a friendly slug or title."""

    needle = requested.strip().casefold()
    if not needle:
        raise SelectorError("recipe selector cannot be empty")
    if deadline is None:
        deadline = time.monotonic() + 30
    identities: set[str] = set()
    matches: set[str] = set()
    for row in _recipe_rows(client, deadline=deadline):
        identity = cast(Mapping[str, object], row["identity"])
        canonical = cast(str, row["selector"])
        if any(
            isinstance(value, str) and value.casefold() == needle
            for value in (canonical, identity.get("recipe_id"))
        ):
            identities.add(canonical)
        if any(
            isinstance(value, str) and value.casefold() == needle
            for value in (identity.get("slug"), identity.get("title"))
        ):
            matches.add(canonical)
    _selection_remaining(deadline)
    matches = identities or matches
    if len(matches) == 1:
        return next(iter(matches))
    if not matches:
        raise SelectorError(f"unknown recipe selector: {requested}")
    candidates = tuple(sorted(matches))
    raise SelectorError(
        f"ambiguous recipe selector: {requested}; choose an exact canonical candidate",
        candidates=candidates,
    )


def _resolve_spark_selectors(
    client: ControllerClient, selectors: list[str], *, deadline: float | None = None
) -> list[str]:
    """Resolve operator-facing Spark names to immutable node IDs."""

    if not selectors:
        return []
    if deadline is None:
        deadline = time.monotonic() + 30
    payload = client.request(
        "GET", "/api/fleet", timeout_seconds=_selection_remaining(deadline)
    )
    _selection_remaining(deadline)
    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list):
        raise TypeError("fleet response does not contain nodes")
    nodes: list[Mapping[str, object]] = []
    ids: set[str] = set()
    for node in raw_nodes:
        if not isinstance(node, Mapping):
            raise TypeError("fleet response contains an invalid Spark")
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id or node_id in ids:
            raise ValueError("fleet response contains an invalid or repeated Spark ID")
        ids.add(node_id)
        nodes.append(node)
    resolved: list[str] = []
    for requested in selectors:
        needle = requested.strip().casefold()
        if not needle:
            raise SelectorError("spark selector cannot be empty")
        exact = [node for node in nodes if str(node["id"]).casefold() == needle]
        matches = exact or [
            node
            for node in nodes
            if any(
                isinstance(value, str) and value.casefold() == needle
                for key in ("display_name", "hostname")
                for value in (node.get(key),)
            )
        ]
        if not matches:
            raise SelectorError(f"unknown spark selector: {requested}")
        if len(matches) > 1:
            candidates = tuple(sorted(str(node["id"]) for node in matches))
            raise SelectorError(
                f"ambiguous spark selector: {requested}; choose an exact Spark ID",
                candidates=candidates,
            )
        resolved.append(cast(str, matches[0]["id"]))
    _selection_remaining(deadline)
    return sorted(set(resolved))


def _profile(
    args: argparse.Namespace,
    client: ControllerClient,
    factory: Callable[[], str],
) -> dict[str, object]:
    number = _profile_number(args)
    action = getattr(args, "profile_action", None)
    if action is None:
        return _overview(client, "profile", args)
    if action == "list":
        return client.request("GET", "/api/profile")
    if action == "endpoint":
        result = validate_control_document(
            "FleetProfileEndpointsView",
            client.profile_endpoints(number, alias=args.alias).to_dict(),
        )
        assignments = result.get("assignments")
        if (
            args.alias is not None
            and result.get("projection_issue") is None
            and (
                not isinstance(assignments, list)
                or not any(
                    isinstance(item, dict) and item.get("alias") == args.alias
                    for item in assignments
                )
            )
        ):
            raise ValueError(f"endpoint alias is not part of profile {number}")
        return result
    if action == "progress":
        if args.application:
            path = f"/api/profile/applications/{args.application}"
            selected_profile_id: str | None = None
            if getattr(args, "profile_number", None) is not None:
                selected_profile = client.request("GET", f"/api/profile/{number}")
                selected_profile_id_value = selected_profile.get("id")
                if (
                    not isinstance(selected_profile_id_value, str)
                    or not selected_profile_id_value
                ):
                    raise ControlMalformedResponse(
                        "selected profile response has no canonical identity"
                    )
                selected_profile_id = selected_profile_id_value
        elif args.request_key:
            path = f"/api/profile/{number}/requests/{args.request_key}"
            selected_profile_id = None
        else:
            path = f"/api/profile/{number}/progress"
            selected_profile_id = None
        result = client.request("GET", path)
        application_id = result.get("id")
        if not isinstance(application_id, str) or not application_id:
            raise ControlMalformedResponse(
                "profile progress response has no durable application identity"
            )
        if args.application and application_id != args.application:
            raise ControlMalformedResponse(
                "profile application response identifies another application"
            )
        if selected_profile_id is not None:
            application_profile_id = result.get("profile_id")
            if (
                not isinstance(application_profile_id, str)
                or not application_profile_id
            ):
                raise ControlMalformedResponse(
                    "profile application response has no canonical profile identity"
                )
            if application_profile_id != selected_profile_id:
                raise ValueError(
                    f"profile application does not belong to selected profile {number}"
                )
        if not args.follow:
            return result
        path = f"/api/profile/applications/{_quoted(application_id)}"

        def same_application(observed: Mapping[str, object]) -> None:
            if observed.get("id") != application_id:
                raise ControlMalformedResponse(
                    "profile progress observation changed application identity"
                )

        return _poll_path(client, path, result, args, validate=same_application)
    if action in {"name", "add", "remove", "configure", "export", "import"}:
        return _profile_authoring(args, client)
    if action == "cancel":
        if not args.yes:
            raise ValueError("profile cancel requires --yes")
        _confirm_action(
            args,
            f"Cancel profile {number} application {args.application_id} and reconcile issued effects?",
        )
        request_key = _request_key(args, factory)
        application_id = args.application_id
        path = f"/api/profile/applications/{_quoted(application_id)}/cancel"
        lookup = (
            f"/api/profile/applications/{_quoted(application_id)}"
            f"/cancellations/{_quoted(request_key)}"
        )

        def same_cancellation(receipt: Mapping[str, object]) -> str:
            cancellation = receipt.get("cancellation")
            cancellation_actor = (
                cancellation.get("actor") if isinstance(cancellation, Mapping) else None
            )
            if (
                receipt.get("id") != application_id
                or not isinstance(cancellation, Mapping)
                or cancellation.get("request_key") != request_key
                or cancellation.get("cause") != "operator"
                or not isinstance(cancellation_actor, str)
                or not cancellation_actor
            ):
                raise ControlMalformedResponse(
                    "profile cancellation receipt identifies another request or owner"
                )
            return application_id

        result = _submit_idempotent_request(
            client,
            args,
            key=request_key,
            path=path,
            lookup=lookup,
            body={"profile_number": number, "request_key": request_key},
            noun="profile",
            action="cancel",
            validate=same_cancellation,
            reconnect=shlex.join(
                [
                    "vonkctl",
                    "--profile",
                    str(number),
                    "profile",
                    "cancel",
                    application_id,
                    "--yes",
                    "--request-key",
                    request_key,
                    "--detach",
                    "--json",
                ]
            ),
        )
        if args.detach:
            return result

        def validate_observed_cancellation(
            observed: Mapping[str, object],
        ) -> None:
            same_cancellation(observed)

        return _poll_path(
            client,
            f"/api/profile/applications/{_quoted(application_id)}",
            result,
            args,
            validate=validate_observed_cancellation,
        )
    if action == "load":
        if args.dry_run:
            if args.expected_plan is not None or args.yes or args.detach:
                raise ValueError(
                    "--dry-run cannot be combined with --expected-plan, --yes, or --detach"
                )
            return client.request("POST", f"/api/profile/{number}/preview")
        expected_digest = args.expected_plan
        interactive = (
            not (
                args.global_json
                or getattr(args, "json", False)
                or getattr(args, "no_input", False)
            )
            and sys.stdin.isatty()
            and sys.stderr.isatty()
        )
        if expected_digest is not None:
            if re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
                raise ValueError(
                    "--expected-plan requires the complete lowercase plan digest"
                )
            if interactive and not args.yes:
                preview = client.request("POST", f"/api/profile/{number}/preview")
                if preview.get("allowed") is not True:
                    args.outcome_context = "preview"
                    return preview
                current_digest = preview.get("plan_digest")
                if (
                    not isinstance(current_digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", current_digest) is None
                ):
                    raise ControlMalformedResponse(
                        "profile preview has no valid reviewed plan digest"
                    )
                with redirect_stdout(sys.stderr):
                    render_payload(preview, "profile", action="preview")
                if current_digest != expected_digest:
                    raise ControlConflict(
                        409,
                        "profile review changed; inspect the current effects and "
                        "rerun with the new plan digest",
                    )
            _confirm_action(
                args, f"Load profile {number} with reviewed plan {expected_digest}?"
            )
        else:
            if not interactive or args.yes:
                raise ValueError(
                    "profile load requires --expected-plan DIGEST --yes in noninteractive mode; review with profile load --dry-run first"
                )
            preview = client.request("POST", f"/api/profile/{number}/preview")
            if preview.get("allowed") is not True:
                args.outcome_context = "preview"
                return preview
            with redirect_stdout(sys.stderr):
                render_payload(preview, "profile", action="preview")
            expected_digest = preview.get("plan_digest")
            if (
                not isinstance(expected_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
            ):
                raise ControlMalformedResponse(
                    "profile preview has no valid reviewed plan digest"
                )
            _confirm_action(args, f"Load profile {number} with these effects?")
        try:
            result = _submit_profile_load(
                client, number, expected_digest, args, factory
            )
        except ControlConflict as error:
            if error.code != "profile.stale_plan":
                raise
            try:
                current_review = client.request(
                    "POST", f"/api/profile/{number}/preview"
                )
                if type(current_review.get("allowed")) is not bool:
                    raise ControlMalformedResponse(
                        "current profile review has no admission decision"
                    )
                current_digest = current_review.get("plan_digest")
                if (
                    not isinstance(current_digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", current_digest) is None
                ):
                    raise ControlMalformedResponse(
                        "current profile review has no valid plan digest"
                    )
                with redirect_stdout(sys.stderr):
                    render_payload(current_review, "profile", action="preview")
                print(
                    "The submitted load was refused. Review these current effects, "
                    "then start a new load with the current plan digest.",
                    file=sys.stderr,
                    flush=True,
                )
            except (
                ControlClientError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
            ):
                # This follow-up is a read after a definitive stale refusal. Its
                # failure must not replace the original admission error.
                pass
            raise
        if args.detach:
            return result
        application_id = result.get("id")
        if not isinstance(application_id, str) or not application_id:
            raise ControlMalformedResponse(
                "profile load has no durable application identity"
            )

        def same_application(observed: Mapping[str, object]) -> None:
            if observed.get("id") != application_id:
                raise ControlMalformedResponse(
                    "profile observation identifies another application"
                )

        return _poll_path(
            client,
            f"/api/profile/applications/{_quoted(application_id)}",
            result,
            args,
            validate=same_application,
        )
    raise ValueError(f"unsupported profile action: {action}")


def run_controller(
    args: argparse.Namespace,
    client: ControllerClient,
    request_id_factory: Callable[[], str],
) -> dict[str, object]:
    command = getattr(args, "command", None) or "profile"
    if command == "fleet":
        return _fleet(args, client, request_id_factory)
    if command == "model":
        return _model(args, client, request_id_factory)
    if command == "recipe":
        return _recipe(args, client, request_id_factory)
    if command == "profile":
        return _profile(args, client, request_id_factory)
    raise ValueError(f"unsupported controller command: {command}")
