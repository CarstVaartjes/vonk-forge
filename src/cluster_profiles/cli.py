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
from typing import cast, overload

from .build_identity import current_build
from .cli_artifact_jobs import _may_have_completed
from .cli_completion import completion_script
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
from .controller_cli import ControllerClient, add_controller_commands, run_controller

_MAX_TEXT_CHARS = 1_024
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|api[_-]?key|password|secret|token)\b"
    r"(\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_SENSITIVE_OPTION = re.compile(
    r"(?i)^--(?:[a-z0-9]+-)*(?:authorization|api-key|password|secret|token|private-key)(?:=|$)"
)


class _UsageError(ValueError):
    pass


class _CliParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _CliParser(
        prog="vonkctl",
        description="Inspect Sparks, prepare assets, and manage whole-fleet profiles.",
        epilog="Start with vonkctl fleet or vonkctl model library. "
        "Use vonkctl COMMAND --help for details. Connection: VONK_CONTROL_URL "
        "and VONK_CONTROL_TOKEN_FILE (a private file).",
    )
    parser.add_argument("--json", dest="global_json", action="store_true")
    parser.add_argument(
        "--version", action="store_true", help="Show the installed CLI build identity"
    )
    parser.add_argument("--profile", dest="profile_number", type=int, default=None)
    parser.add_argument("--no-input", action="store_true", help="Never prompt")
    parser.add_argument(
        "--check-connection",
        action="store_true",
        help="Validate credentials, TLS, and an authorized Controller read",
    )
    commands = parser.add_subparsers(
        dest="command", required=False, parser_class=_CliParser
    )
    add_controller_commands(commands)
    completion = commands.add_parser(
        "completion", help="Generate offline shell completion"
    )
    completion.add_argument("shell", choices=("bash", "zsh"))
    update = commands.add_parser(
        "update", help="Check or install the accepted CLI release"
    )
    update.add_argument(
        "--apply",
        action="store_true",
        help="Install the signed wheel in this Python environment",
    )
    update.add_argument("--channel", choices=("dev", "stable"), default=None)
    update.add_argument("--public-key", type=Path, default=None)
    update.add_argument("--origin", default="https://install.vonkforge.ai")
    update.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    return parser


def _sanitize_text(value: object, *, limit: int | None = _MAX_TEXT_CHARS) -> str:
    text = str(value)
    text = _SENSITIVE_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}: <redacted>", text
    )
    text = _BEARER.sub("Bearer <redacted>", text)
    if "-----BEGIN " in text:
        text = text.split("-----BEGIN ", 1)[0] + "<redacted private key>"
    if limit is not None and len(text) > limit:
        text = text[: limit - 15] + "... (truncated)"
    return terminal_text(text)


@overload
def _sanitize(value: Mapping[str, object]) -> dict[str, object]: ...


@overload
def _sanitize(value: object) -> object: ...


def _sanitize(value: object) -> object:
    if isinstance(value, str):
        return _sanitize_text(value, limit=None)
    if isinstance(value, Mapping):
        return {str(key): _sanitize(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_sanitize(item) for item in value]
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
    message = (
        "control API unavailable"
        if isinstance(error, (ControlUnavailable, ControlTransportError, OSError))
        else _sanitize_text(error)
    )
    result: dict[str, object] = {
        "error": message,
        "error_type": "control_api",
        "code": getattr(error, "code", None) or "control.api_error",
        "detail": getattr(error, "detail", message),
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
        getattr(args, "command", None) == "profile"
        and getattr(args, "profile_action", None) == "load"
        and not isinstance(submission, Submission)
    )
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
                        "--strategy",
                        str(getattr(args, "strategy", "one-at-a-time")),
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
        elif noun == "profile":
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
            getattr(args, "command", None) == "profile"
            and getattr(args, "profile_action", None) == "load"
            and result.get("code") == "profile.stale_plan"
        ):
            result["reconcile"] = {
                "operation": shlex.join(
                    [
                        "vonkctl",
                        "--profile",
                        str(getattr(args, "profile_number", 1)),
                        "profile",
                        "load",
                        "--dry-run",
                    ]
                )
            }
        elif (
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


def _emit(
    payload: Mapping[str, object], args: argparse.Namespace, *, error: bool = False
) -> None:
    if getattr(args, "document_output", False):
        print(json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False))
        return
    safe = (
        dict(payload)
        if args.global_json or getattr(args, "json", False)
        else _sanitize(payload)
    )
    if args.global_json or getattr(args, "json", False):
        print(json.dumps(safe, sort_keys=True, separators=(",", ":")))
        return
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
        error_args = argparse.Namespace(
            global_json="--json" in raw_argv, json=False, command="profile"
        )
        _emit(
            {
                "error": (
                    "invalid command arguments"
                    if _arguments_may_contain_secrets(raw_argv)
                    else str(error)
                ),
                "error_type": "arguments",
            },
            error_args,
            error=True,
        )
        return 2

    if args.version:
        identity = current_build()
        if args.global_json:
            print(json.dumps(identity, sort_keys=True, separators=(",", ":")))
        else:
            source = identity["source_sha"] or "unstamped"
            print(f"vonkctl {identity['version']} ({source})")
        return 0
    if args.command == "completion":
        print(completion_script(parser, args.shell), end="")
        return 0
    if args.command is None and not args.check_connection:
        if args.global_json:
            print(json.dumps({"help": parser.format_help()}))
        else:
            parser.print_help()
        return 0
    if args.command == "update":
        key = args.public_key or os.environ.get("VONK_INSTALLER_PUBLIC_KEY_FILE")
        if key is None:
            result: dict[str, object] = {
                "error": "set --public-key to the trusted installer signing public key",
                "error_type": "update",
            }
            status = 2
        else:
            try:
                result = run_update(
                    channel=args.channel or configured_update_channel(),
                    public_key=Path(key),
                    origin=args.origin,
                    apply=args.apply,
                )
                cache_update_notice(result, public_key=Path(key), origin=args.origin)
                status = 0
            except (CliUpdateError, OSError) as error:
                result = {"error": _sanitize_text(error), "error_type": "update"}
                status = 2
        if args.global_json or getattr(args, "json", False):
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
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
            raise ValueError("this command requires explicit --profile N selection")
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
