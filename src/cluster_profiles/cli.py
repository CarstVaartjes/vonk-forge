"""Authenticated command-line adapter for the four operator nouns."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import overload

from .build_identity import current_build
from .cli_render import render_payload
from .cli_update import (
    CliUpdateError,
    cache_update_notice,
    interactive_notice,
    run_update,
)
from .control_client import (
    ControlClient,
    ControlClientError,
    ControlTransportError,
    ControlUnavailable,
)
from .controller_cli import add_controller_commands, result_exit_code, run_controller

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
    parser = _CliParser(prog="vonkctl")
    parser.add_argument("--json", dest="global_json", action="store_true")
    parser.add_argument(
        "--version", action="store_true", help="Show the installed CLI build identity"
    )
    parser.add_argument("--profile", dest="profile_number", type=int, default=1)
    commands = parser.add_subparsers(
        dest="command", required=False, parser_class=_CliParser
    )
    add_controller_commands(commands)
    update = commands.add_parser(
        "update", help="Check or install the accepted CLI release"
    )
    update.add_argument(
        "--apply",
        action="store_true",
        help="Install the signed wheel in this Python environment",
    )
    update.add_argument("--channel", choices=("dev", "stable"), default="stable")
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
    return text


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


def _emit(payload: Mapping[str, object], args: argparse.Namespace) -> None:
    safe = (
        dict(payload)
        if args.global_json or getattr(args, "json", False)
        else _sanitize(payload)
    )
    if args.global_json or getattr(args, "json", False):
        print(json.dumps(safe, sort_keys=True, separators=(",", ":")))
        return
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
    del root
    raw_argv = tuple(argv) if argv is not None else tuple(sys.argv[1:])
    try:
        args = _parser().parse_args(raw_argv)
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
                    channel=args.channel,
                    public_key=Path(key),
                    origin=args.origin,
                    apply=args.apply,
                )
                cache_update_notice(result)
                status = 0
            except (CliUpdateError, OSError) as error:
                result = {"error": _sanitize_text(error), "error_type": "update"}
                status = 2
        if args.global_json or getattr(args, "json", False):
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        else:
            for name, value in result.items():
                print(f"{name.replace('_', ' ')}: {value}")
        return status

    try:
        client = control_client or ControlClient.from_environment()
        watch_rendered = False

        def render_watch(observed: Mapping[str, object]) -> None:
            nonlocal watch_rendered
            if sys.stdout.isatty():
                print("\033[2J\033[H", end="")
            render_payload(
                _sanitize(observed),
                getattr(args, "command", None) or "profile",
                wide=getattr(args, "wide", False),
            )
            watch_rendered = True

        if not args.global_json and not getattr(args, "json", False):
            args._watch_callback = render_watch
        result = run_controller(
            args,
            client,  # type: ignore[arg-type]
            request_id_factory or (lambda: str(uuid.uuid4())),
        )
        if not watch_rendered or args.global_json or getattr(args, "json", False):
            _emit(result, args)
        if (
            sys.stderr.isatty()
            and not args.global_json
            and not getattr(args, "json", False)
        ):
            notice = interactive_notice()
            if notice:
                print(notice, file=sys.stderr)
        return result_exit_code(result)
    except (
        ControlClientError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        _emit(_control_error(error, args), args)
        return 2
    except KeyboardInterrupt:
        _emit(_control_error(ControlClientError("operation interrupted"), args), args)
        return 130
