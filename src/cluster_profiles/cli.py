"""Authenticated command-line adapter for the four operator nouns."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast, overload

from .build_identity import current_build
from .cli_completion import completion_script
from .cli_outcome import CommandOutcome, Observation
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
_MAX_COLLECTION_ITEMS = 1_024
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


def _sanitize_text(value: object) -> str:
    text = str(value).replace("\x00", "")
    text = _SENSITIVE_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}: <redacted>", text
    )
    text = _BEARER.sub("Bearer <redacted>", text)
    if "-----BEGIN " in text:
        text = text.split("-----BEGIN ", 1)[0] + "<redacted private key>"
    if len(text) > _MAX_TEXT_CHARS:
        text = text[: _MAX_TEXT_CHARS - 15] + "... (truncated)"
    return terminal_text(text)


@overload
def _sanitize(value: Mapping[str, object]) -> dict[str, object]: ...


@overload
def _sanitize(value: object) -> object: ...


def _sanitize(value: object) -> object:
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, Mapping):
        return {str(key): _sanitize(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_sanitize(item) for item in value[:_MAX_COLLECTION_ITEMS]]
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
    context = getattr(error, "context", None)
    if context is not None:
        result.update(context.as_dict())
    result = {key: value for key, value in result.items() if value is not None}
    request_key = getattr(args, "request_key", None) if args is not None else None
    if isinstance(request_key, str) and request_key:
        result["request_key"] = request_key
        result["reconcile"] = {
            "operation": "inspect the durable operation with the same request key",
            "request_key": request_key,
        }
    return result


def _emit(
    payload: Mapping[str, object], args: argparse.Namespace, *, error: bool = False
) -> None:
    safe = (
        dict(payload)
        if args.global_json or getattr(args, "json", False)
        else _sanitize(payload)
    )
    if args.global_json or getattr(args, "json", False):
        print(json.dumps(safe, sort_keys=True, separators=(",", ":")))
        return
    with redirect_stdout(sys.stderr if error else sys.stdout):
        render_payload(
            safe,
            getattr(args, "command", None) or "profile",
            wide=getattr(args, "wide", False),
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
        status = _main(
            argv,
            root=root,
            control_client=control_client,
            request_id_factory=request_id_factory,
        )
        sys.stdout.flush()
        sys.stderr.flush()
        return status
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
            message = _sanitize_text(progress_line(observed))
            observation = getattr(args, "observation", None)
            if isinstance(observation, Observation) and observation.error:
                message = f"Reconnecting: {observation.error}; last confirmed {message}"
            if message != last_progress:
                print(message, file=sys.stderr)
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
                f"Observation deadline reached; accepted work continues.\nReconnect: {outcome.observation.reconnect_command}",
                file=sys.stderr,
            )
            _emit(result, args)
        else:
            _emit(outcome.output, args)
        if interactive:
            notice = interactive_notice()
            if notice:
                print(notice, file=sys.stderr)
        return outcome.exit_code
    except BrokenPipeError:
        raise
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
                ControlClientError("observation interrupted; accepted work continues"),
                args,
            ),
            args,
            error=True,
        )
        return 130
