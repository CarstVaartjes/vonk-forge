"""Authenticated command-line adapter for the four operator nouns."""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from .cli_render import render_payload
from .control_client import (
    ControlClient,
    ControlClientError,
    ControlTransportError,
    ControlUnavailable,
)
from .controller_cli import add_controller_commands, run_controller

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
    parser.add_argument("--profile", dest="profile_number", type=int, default=1)
    commands = parser.add_subparsers(
        dest="command", required=False, parser_class=_CliParser
    )
    add_controller_commands(commands)
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
    safe = dict(payload) if args.global_json or getattr(args, "json", False) else _sanitize(payload)
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

    try:
        client = control_client or ControlClient.from_environment()
        result = run_controller(
            args,
            client,  # type: ignore[arg-type]
            request_id_factory or (lambda: str(uuid.uuid4())),
        )
        _emit(result, args)
        return 0
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
