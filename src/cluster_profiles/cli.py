"""Authenticated command-line adapter for the four operator nouns."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any, NoReturn, cast, overload

from .build_identity import current_build
from .cli_artifact_jobs import _may_have_completed
from .cli_completion import completion_script
from .cli_help import (
    GROUP_VIEWS,
    command_help,
    group_help,
    subcommands,
    usage_error,
)
from .cli_outcome import (
    CommandOutcome,
    EnrollmentDeliveryError,
    Observation,
    Submission,
)
from .cli_render import progress_line, render_payload, terminal_text
from .cli_update import (
    CliUpdateError,
    begin_interactive_update_check,
    cache_update_notice,
    configured_update_channel,
    interactive_notice,
    run_update,
)
from .control_client import (
    ControlClient,
    ControlClientError,
    ControlTransportError,
    ControlUnavailable,
)
from .controller_cli import (
    ActionDeclined,
    ControllerClient,
    add_controller_commands,
    run_controller,
)

_MAX_TEXT_CHARS = 1_024
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|api[_-]?key|password|secret|token)\b"
    r"(\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_SENSITIVE_OPTION = re.compile(
    r"(?i)^--(?:[a-z0-9]+-)*(?:authorization|api-key|password|secret|token|private-key)(?:=|$)"
)


_UPDATE_ORIGIN = "https://install.vonkforge.ai"


class _UsageError(ValueError):
    def __init__(self, message: str, *, usage: str | None, next_step: str) -> None:
        super().__init__(message)
        self.usage = usage
        self.next_step = next_step


class _Subcommands(argparse._SubParsersAction):  # type: ignore[type-arg]
    """Subcommands whose own help opens with the summary their group lists."""

    def add_parser(self, name: str, **kwargs: Any) -> argparse.ArgumentParser:
        if "help" in kwargs:
            kwargs.setdefault("description", kwargs["help"])
        return super().add_parser(name, **kwargs)


class _CliParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.register("action", "parsers", _Subcommands)
        for action in self._actions:
            if isinstance(action, argparse._HelpAction):
                action.help = "Show help"

    def error(self, message: str) -> NoReturn:
        text, usage, next_step = usage_error(self, message)
        raise _UsageError(text, usage=usage, next_step=next_step)

    def format_help(self) -> str:
        if subcommands(self) is not None:
            return group_help(self)
        return command_help(self)


def _parser() -> argparse.ArgumentParser:
    parser = _CliParser(
        prog="vonkctl",
        description="Run models on your Spark fleet: see what runs where, "
        "prepare recipes, and load whole-fleet profiles.",
        epilog="Connection: set VONK_CONTROL_URL and VONK_CONTROL_TOKEN_FILE "
        "(a private file), then check it with vonkctl --check-connection.",
    )
    parser.add_argument(
        "--json",
        dest="global_json",
        action="store_true",
        help="Print one JSON document instead of text",
    )
    parser.add_argument(
        "--profile",
        dest="profile_number",
        type=int,
        default=None,
        metavar="N",
        help="Profile number for profile commands (default: 1)",
    )
    parser.add_argument(
        "--no-input",
        action="store_true",
        help="Never prompt; changes then need --yes",
    )
    parser.add_argument(
        "--check-connection",
        action="store_true",
        help="Check the Controller URL, token file, and access",
    )
    parser.add_argument(
        "--version", action="store_true", help="Show the installed version"
    )
    commands = parser.add_subparsers(
        dest="command", required=False, parser_class=_CliParser
    )
    add_controller_commands(commands)
    completion = commands.add_parser(
        "completion", help="Print shell completion for bash or zsh"
    )
    completion.add_argument(
        "shell", nargs="?", choices=("bash", "zsh"), metavar="SHELL", help="bash or zsh"
    )
    update = commands.add_parser("update", help="Check for or install a newer vonkctl")
    update.add_argument(
        "--apply",
        action="store_true",
        help="Install the newer signed release in this Python environment",
    )
    update.add_argument(
        "--channel",
        choices=("dev", "stable"),
        default=None,
        help="Release channel (default: the one this install came from)",
    )
    update.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Print one JSON document instead of text",
    )
    return parser


def _sanitize_text(
    value: object, *, limit: int | None = _MAX_TEXT_CHARS, terminal: bool = True
) -> str:
    text = str(value)
    text = _SENSITIVE_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}: <redacted>", text
    )
    text = _BEARER.sub("Bearer <redacted>", text)
    if "-----BEGIN " in text:
        text = text.split("-----BEGIN ", 1)[0] + "<redacted private key>"
    if limit is not None and len(text) > limit:
        text = text[: limit - 15] + "... (truncated)"
    return terminal_text(text) if terminal else text


@overload
def _sanitize(
    value: Mapping[str, object], *, terminal: bool = True
) -> dict[str, object]: ...


@overload
def _sanitize(value: object, *, terminal: bool = True) -> object: ...


def _sanitize(value: object, *, terminal: bool = True) -> object:
    if isinstance(value, str):
        return _sanitize_text(value, limit=None, terminal=terminal)
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize(item, terminal=terminal) for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [_sanitize(item, terminal=terminal) for item in value]
    return value


def _arguments_may_contain_secrets(argv: Sequence[str]) -> bool:
    return any(
        _SENSITIVE_OPTION.match(argument.replace("_", "-"))
        or _SENSITIVE_ASSIGNMENT.search(argument)
        or _BEARER.search(argument)
        or "-----BEGIN " in argument
        for argument in argv
    )


def _control_error(
    error: BaseException, args: argparse.Namespace | None = None
) -> dict[str, object]:
    code = getattr(error, "code", None) or "control.api_error"
    detail = getattr(error, "detail", str(error))
    message = _plain_language_error(code, detail)
    message = (
        "control API unavailable"
        if isinstance(error, (ControlUnavailable, ControlTransportError, OSError))
        else message
    )
    result: dict[str, object] = {
        "error": message,
        "error_type": "control_api",
        "code": code,
        "detail": _sanitize_text(detail),
        "recovery_actions": list(getattr(error, "recovery", ()) or ()),
        "retryable": getattr(error, "retryable", False) is True,
        "retry_time": getattr(error, "retry_time", None),
        "retry_after_seconds": getattr(error, "retry_after_seconds", None),
        "preserved": getattr(error, "preserved", None),
        "required_bytes": getattr(error, "required_bytes", None),
        "free_bytes": getattr(error, "free_bytes", None),
        "shortfall_bytes": getattr(error, "shortfall_bytes", None),
        "log_excerpt": getattr(error, "log_excerpt", None),
    }
    candidates = getattr(error, "candidates", ())
    if candidates:
        result["candidates"] = list(candidates)
    context = getattr(error, "context", None)
    if context is not None:
        result.update(context.as_dict())
    submission = getattr(args, "submission", None) if args is not None else None
    if isinstance(submission, Submission):
        result["submission"] = submission.document()
        if submission.acceptance == "unknown":
            result["error"] = f"{submission.action.capitalize()} acceptance is unknown"
    if isinstance(submission, Submission):
        acceptance_may_exist = submission.acceptance in {"unknown", "accepted"}
    else:
        acceptance_may_exist = _may_have_completed(error)
    result = {key: value for key, value in result.items() if value is not None}
    request_key = getattr(args, "request_key", None) if args is not None else None
    profile_load_not_submitted = (
        getattr(args, "command", None) == "run"
        or (
            getattr(args, "command", None) == "profile"
            and getattr(args, "profile_action", None) == "load"
        )
    ) and not isinstance(submission, Submission)
    if isinstance(request_key, str) and request_key and not profile_load_not_submitted:
        result["request_key"] = request_key
        noun = getattr(args, "command", None)
        fleet_upgrade_replay = None
        if (
            noun == "fleet"
            and getattr(args, "fleet_action", None) == "upgrade"
            and isinstance(submission, Submission)
            and submission.acceptance == "unknown"
        ):
            selector = getattr(args, "selector", None)
            scope = (
                ["--all"]
                if getattr(args, "all", False)
                else [selector]
                if isinstance(selector, str) and selector
                else []
            )
            if scope:
                fleet_upgrade_replay = shlex.join(
                    [
                        "vonkctl",
                        "fleet",
                        "upgrade",
                        *scope,
                        "--request-key",
                        request_key,
                        "--yes",
                    ]
                )
        if (
            noun in {"model", "recipe"}
            and getattr(args, f"{noun}_action", None) == "cancel"
        ):
            reconcile_operation = shlex.join(
                [
                    "vonkctl",
                    noun,
                    "cancel",
                    str(getattr(args, "operation_id", "")),
                    "--yes",
                    "--request-key",
                    request_key,
                    "--reason",
                    str(getattr(args, "reason", "operator requested cancellation")),
                ]
            )
        elif noun in {"model", "recipe"}:
            reconcile_operation = shlex.join(
                [
                    "vonkctl",
                    noun,
                    "progress",
                    "--request-key",
                    request_key,
                    "--follow",
                ]
            )
        elif noun in {"profile", "run"}:
            reconcile_operation = shlex.join(
                [
                    "vonkctl",
                    "--profile",
                    str(getattr(args, "profile_number", 1)),
                    "profile",
                    "progress",
                    "--request-key",
                    request_key,
                    "--follow",
                ]
            )
        elif fleet_upgrade_replay is not None:
            reconcile_operation = fleet_upgrade_replay
        else:
            reconcile_operation = (
                "inspect the durable operation with the same request key"
            )
        result["reconcile"] = {
            "operation": reconcile_operation,
            "request_key": request_key,
        }
    artifact_job_id = getattr(args, "artifact_job_id", None)
    if isinstance(artifact_job_id, str):
        result["artifact_job_id"] = artifact_job_id
    reconcile = getattr(args, "artifact_job_reconcile", None)
    if isinstance(reconcile, str):
        result["reconcile"] = {
            "operation": reconcile,
            **(
                {"request_key": request_key}
                if isinstance(request_key, str) and request_key
                else {}
            ),
        }
    uploaded = getattr(args, "artifact_job_uploaded", None)
    if isinstance(uploaded, list):
        result["uploaded_inputs"] = uploaded
    upload_unknown = getattr(args, "artifact_job_upload_unknown", None)
    if isinstance(upload_unknown, str):
        result["upload_acceptance"] = "unknown"
        result["input_name"] = upload_unknown
    downloaded = getattr(args, "artifact_job_downloaded", None)
    if isinstance(downloaded, list):
        result["downloaded_files"] = downloaded
        result["partial_collection"] = bool(downloaded)
        total_files = getattr(args, "artifact_job_download_total", None)
        if isinstance(total_files, int):
            result["downloaded_file_count"] = len(downloaded)
            result["expected_file_count"] = total_files
    if (
        getattr(args, "command", None) == "recipe"
        and getattr(args, "recipe_action", None) == "job"
        and isinstance(request_key, str)
        and request_key
        and getattr(args, "recipe_job_action", None) == "create"
    ):
        result["reconcile"] = {
            "operation": shlex.join(
                [
                    "vonkctl",
                    "recipe",
                    "job",
                    "create",
                    "--run",
                    str(getattr(args, "run", "")),
                    "--file",
                    str(getattr(args, "file", "")),
                    "--request-key",
                    request_key,
                ]
            ),
            "request_key": request_key,
            **(
                {"artifact_job_id": artifact_job_id}
                if isinstance(artifact_job_id, str)
                else {}
            ),
        }
    artifact_job_command = (
        getattr(args, "command", None) == "recipe"
        and getattr(args, "recipe_action", None) == "job"
    )
    artifact_job_action = (
        getattr(args, "recipe_job_action", None) if artifact_job_command else None
    )
    artifact_job_id = getattr(args, "artifact_job_id", None)
    artifact_reconcile = getattr(args, "artifact_job_reconcile", None)
    command_noun = getattr(args, "command", None)
    if artifact_job_command:
        if acceptance_may_exist:
            if isinstance(artifact_reconcile, str):
                result["reconcile"] = {
                    "operation": artifact_reconcile,
                    **(
                        {"request_key": request_key}
                        if isinstance(request_key, str)
                        and request_key
                        and artifact_job_action in {"submit", "cancel"}
                        else {}
                    ),
                }
        elif artifact_job_action in {"create", "upload", "submit", "cancel"}:
            if isinstance(artifact_job_id, str):
                result["reconcile"] = {
                    "operation": shlex.join(
                        ["vonkctl", "recipe", "job", "detail", artifact_job_id]
                    )
                }
            else:
                result.pop("reconcile", None)
    elif not acceptance_may_exist:
        result.pop("reconcile", None)
        if (
            isinstance(command_noun, str)
            and command_noun in {"model", "recipe"}
            and getattr(args, f"{command_noun}_action", None) == "cancel"
        ):
            operation_id = getattr(args, "operation_id", None)
            if isinstance(operation_id, str):
                result["reconcile"] = {
                    "operation": shlex.join(
                        [
                            "vonkctl",
                            command_noun,
                            "progress",
                            operation_id,
                            "--follow",
                        ]
                    )
                }
    return result


def _plain_language_error(code: object, detail: object) -> str:
    """Translate common operator refusals while retaining the wire detail."""
    messages = {
        "profile.admission_busy": (
            "Another workload change is using a selected Spark. Wait for it to "
            "finish, then try again."
        ),
        "profile.admission_effect_busy": (
            "A selected Spark is still cleaning up an earlier workload. Wait "
            "for cleanup to finish, then try again."
        ),
        "profile.spark_unavailable": (
            "A selected Spark is unreachable. Restore its connection or choose "
            "a reachable Spark, then review the run again."
        ),
        "profile.topology_incomplete": (
            "This model needs more Sparks than are available in the selected "
            "group. Add the missing Sparks and review the run again."
        ),
        "profile.review_stale": (
            "The plan changed after you reviewed it, so nothing was loaded. "
            "Review the current plan and load again."
        ),
        "profile.preparation_unavailable": (
            "A required model file or runtime image is not ready. Prepare the "
            "named asset, then review the run again."
        ),
        "controller.fleet.enrollment_denied": (
            "The Controller did not accept this Spark enrollment. Check that "
            "the Spark is reachable and has a current enrollment grant, then "
            "request a new grant if needed."
        ),
        "controller.authentication_required": (
            "Vonk could not authenticate this request. Check the configured "
            "Controller token and try again."
        ),
        "controller.request_rejected": (
            "The Controller rejected this request. Check your access and the "
            "requested action, then try again."
        ),
        "catalog.reference_missing": (
            "This recipe refers to a model version that is not available in "
            "the active library. Choose a current recipe or update the library."
        ),
    }
    if isinstance(code, str) and code in messages:
        return messages[code]
    text = _sanitize_text(detail)
    replacements = (
        ("digest-bound", "tied to the reviewed plan"),
        ("authority revision", "current Controller authorization"),
        ("provenance", "verified source details"),
        ("admission", "run check"),
    )
    for technical, plain in replacements:
        text = re.sub(re.escape(technical), plain, text, flags=re.IGNORECASE)
    return text


def _plain_language_document(value: object) -> object:
    """Translate user-visible reason text without changing stable codes."""
    if isinstance(value, Mapping):
        code = value.get("code")
        return {
            str(key): (
                _plain_language_error(code, item)
                if key == "detail" and isinstance(item, str)
                else _plain_language_document(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_plain_language_document(item) for item in value]
    return value


def _emit(
    payload: Mapping[str, object], args: argparse.Namespace, *, error: bool = False
) -> None:
    if getattr(args, "document_output", False):
        print(json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False))
        return
    json_output = args.global_json or getattr(args, "json", False)
    safe = _sanitize(payload, terminal=not json_output)
    if json_output:
        print(_json_text(safe))
        return
    if error:
        code = safe.get("code")
        if isinstance(safe.get("detail"), str):
            safe["detail"] = _plain_language_error(code, safe["detail"])
    else:
        safe = cast(dict[str, object], _plain_language_document(safe))
    preview = (
        getattr(args, "dry_run", False)
        or getattr(args, "outcome_context", None) == "preview"
    )
    preview_only = (
        not getattr(args, "dry_run", False)
        and getattr(args, "outcome_context", None) == "preview"
    )
    with redirect_stdout(sys.stderr if error or preview_only else sys.stdout):
        render_payload(
            safe,
            getattr(args, "command", None) or "profile",
            wide=getattr(args, "wide", False),
            action=(
                "connection"
                if getattr(args, "check_connection", False)
                else "preview"
                if preview
                else getattr(args, f"{getattr(args, 'command', '')}_action", None)
            ),
            technical=getattr(args, "technical", False),
            activity_filters=(
                {
                    "limit": getattr(args, "limit", 20),
                    "state": getattr(args, "state", None),
                    "target": getattr(args, "target", None),
                    "request_id": getattr(args, "request_id", None),
                }
                if getattr(args, "command", None) == "fleet"
                and getattr(args, "fleet_action", None) == "activity"
                else None
            ),
            artifact_job_action=(
                getattr(args, "recipe_job_action", None)
                if getattr(args, "command", None) == "recipe"
                and getattr(args, "recipe_action", None) == "job"
                else None
            ),
        )


def _json_text(document: object) -> str:
    """One JSON document: indented for a person, compact for a pipe."""

    if sys.stdout.isatty():
        return json.dumps(document, sort_keys=True, indent=2)
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def _bare_group(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> argparse.ArgumentParser | None:
    """Return the group invoked without a command when it has no view of its own."""

    current = parser
    while (commands := subcommands(current)) is not None:
        chosen = getattr(args, commands.dest, None)
        if chosen is None:
            return None if current.prog in GROUP_VIEWS - {"vonkctl"} else current
        current = commands.choices[chosen]
    if current.prog == "vonkctl completion" and args.shell is None:
        return current
    return None


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path | None = None,
    control_client: object | None = None,
    request_id_factory: Callable[[], str] | None = None,
) -> int:
    """Run the API-backed CLI."""
    try:
        try:
            return _main(
                argv,
                root=root,
                control_client=control_client,
                request_id_factory=request_id_factory,
            )
        finally:
            # Argparse help exits early; its buffered output still belongs to
            # this process boundary rather than interpreter shutdown.
            sys.stdout.flush()
            sys.stderr.flush()
    except BrokenPipeError:
        # Avoid a second flush into the closed pipe during interpreter shutdown.
        for stream in (sys.stdout, sys.stderr):
            try:
                descriptor = stream.fileno()
            except (AttributeError, OSError, ValueError):
                continue
            if descriptor not in (1, 2):
                continue
            null = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(null, descriptor)
            finally:
                os.close(null)
        return 141


def _report_usage_error(raw_argv: Sequence[str], error: _UsageError) -> int:
    error_args = argparse.Namespace(
        global_json="--json" in raw_argv, json=False, command="profile"
    )
    document: dict[str, object] = {
        "error": (
            "invalid command arguments"
            if _arguments_may_contain_secrets(raw_argv)
            else str(error)
        ),
        "error_type": "arguments",
        "recovery_actions": [error.next_step],
    }
    if error.usage is not None:
        document["usage"] = error.usage
    _emit(document, error_args, error=True)
    return 2


def _main(
    argv: Sequence[str] | None = None,
    *,
    root: Path | None = None,
    control_client: object | None = None,
    request_id_factory: Callable[[], str] | None = None,
) -> int:
    del root
    raw_argv = tuple(argv) if argv is not None else tuple(sys.argv[1:])
    try:
        parser = _parser()
        args = parser.parse_args(raw_argv)
    except _UsageError as error:
        return _report_usage_error(raw_argv, error)

    if args.version:
        identity = current_build()
        if args.global_json:
            print(_json_text(identity))
        else:
            source = identity["source_sha"] or "unstamped"
            print(f"vonkctl {identity['version']} ({source})")
        return 0
    if args.command == "completion" and args.shell is not None:
        print(completion_script(parser, args.shell), end="")
        return 0
    bare = _bare_group(parser, args)
    if bare is not None and not args.check_connection:
        if args.global_json:
            print(_json_text({"help": bare.format_help()}))
        else:
            bare.print_help()
        return 0
    if args.command == "update":
        try:
            result: dict[str, object] = run_update(
                channel=args.channel or configured_update_channel(),
                origin=_UPDATE_ORIGIN,
                apply=args.apply,
            )
            cache_update_notice(result)
            status = 0
        except (CliUpdateError, OSError) as error:
            result = {"error": _sanitize_text(error), "error_type": "update"}
            status = 2
        if args.global_json or getattr(args, "json", False):
            print(_json_text(result))
        else:
            for name, value in result.items():
                print(
                    f"{name.replace('_', ' ')}: {terminal_text(str(value))}",
                    file=sys.stderr if status else sys.stdout,
                )
        return status

    interactive = (
        sys.stderr.isatty()
        and sys.stdin.isatty()
        and not args.no_input
        and not args.global_json
        and not getattr(args, "json", False)
    )
    if interactive:
        begin_interactive_update_check()

    try:
        args.invocation = raw_argv
        if args.profile_number is not None and args.profile_number < 1:
            raise ValueError("--profile must be a positive stable profile number")
        if getattr(args, "requires_profile", False) and args.profile_number is None:
            return _report_usage_error(
                raw_argv,
                _UsageError(
                    "this command changes a profile, so it needs --profile N",
                    usage=None,
                    next_step=(
                        "add --profile N, for example: "
                        + (
                            "vonkctl " + shlex.join(["--profile", "1", *raw_argv])
                            if not _arguments_may_contain_secrets(raw_argv)
                            else "vonkctl --profile 1 ..."
                        )
                    ),
                ),
            )
        if args.check_connection and args.command is not None:
            raise ValueError("--check-connection cannot be combined with a command")
        client = (
            cast(ControllerClient, control_client)
            if control_client is not None
            else ControlClient.from_environment()
        )
        if args.check_connection:
            client.request("GET", "/api/fleet")
            _emit(
                {
                    "connected": True,
                    "client": current_build(),
                    "authorized_read": "/api/fleet",
                    "origin": os.environ.get("VONK_CONTROL_URL"),
                },
                args,
            )
            return 0
        last_progress: str | None = None

        def render_watch(observed: Mapping[str, object]) -> None:
            nonlocal last_progress
            if (
                getattr(args, "watch", False)
                or getattr(args, "fleet_action", None) == "loginfo"
            ):
                buffer = StringIO()
                with redirect_stderr(buffer):
                    _emit(observed, args, error=True)
                message = buffer.getvalue().rstrip()
            else:
                message = _sanitize_text(progress_line(observed))
            observation = getattr(args, "observation", None)
            if isinstance(observation, Observation) and observation.error:
                message = f"Reconnecting: {observation.error}; last confirmed {message}"
            if message != last_progress:
                encoding = sys.stderr.encoding or "utf-8"
                print(
                    message.encode(encoding, "backslashreplace").decode(encoding),
                    file=sys.stderr,
                )
                last_progress = message

        if not args.global_json and not getattr(args, "json", False):
            args._watch_callback = render_watch
        result = run_controller(
            args,
            client,
            request_id_factory or (lambda: str(uuid.uuid4())),
        )
        outcome = CommandOutcome.from_command(args, result)
        if (
            outcome.observation
            and outcome.observation.status == "timed_out"
            and not (args.global_json or getattr(args, "json", False))
        ):
            print(
                (
                    "Observation deadline reached; latest snapshot follows."
                    if getattr(args, "watch", False)
                    else "Observation deadline reached; accepted work continues."
                )
                + f"\nReconnect: {outcome.observation.reconnect_command}",
                file=sys.stderr,
            )
            _emit(result, args)
        else:
            _emit(outcome.output, args)
        if getattr(args, "profile_saved", False) and not (
            args.global_json or getattr(args, "json", False)
        ):
            print("Saved; running fleet unchanged.")
        if interactive:
            notice = interactive_notice()
            if notice:
                print(notice, file=sys.stderr)
        return outcome.exit_code
    except BrokenPipeError:
        raise
    except ActionDeclined as error:
        _emit(
            {
                "error": str(error),
                "error_type": "declined",
                "recovery_actions": list(error.next_steps),
            },
            args,
            error=True,
        )
        return 2
    except EnrollmentDeliveryError as error:
        _emit(error.document, args, error=True)
        return 130 if error.interrupted else 2
    except (
        ControlClientError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        _emit(_control_error(error, args), args, error=True)
        return 2
    except KeyboardInterrupt:
        observation = getattr(args, "observation", None)
        if isinstance(observation, Observation):
            observation.status = "interrupted"
            _emit(observation.document(), args, error=True)
            return 130
        _emit(
            _control_error(
                ControlClientError("command interrupted"),
                args,
            ),
            args,
            error=True,
        )
        return 130
