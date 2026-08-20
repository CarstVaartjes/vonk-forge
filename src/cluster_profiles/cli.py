"""Control-API command line adapter for routine GPU node administration."""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from .control_client import (
    ControlClient,
    ControlClientError,
    ControlTransportError,
    ControlUnavailable,
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
class _UsageError(ValueError):
    pass


class _CliParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _UsageError(message)


class _GeneratedModel(Protocol):
    def to_dict(self) -> dict[str, object]: ...


class _RoutineControlClient(Protocol):
    def nodes(self) -> _GeneratedModel: ...

    def endpoint(self, alias: str) -> _GeneratedModel: ...


def _add_json(command: argparse.ArgumentParser) -> None:
    command.add_argument("--json", action="store_true")


def _parser() -> argparse.ArgumentParser:
    parser = _CliParser(prog="vonkctl")
    parser.add_argument("--json", dest="global_json", action="store_true")
    commands = parser.add_subparsers(
        dest="command", required=True, parser_class=_CliParser
    )

    nodes = commands.add_parser("nodes")
    node_commands = nodes.add_subparsers(
        dest="nodes_command", required=True, parser_class=_CliParser
    )
    _add_json(node_commands.add_parser("status"))

    endpoint = commands.add_parser("endpoint")
    endpoint.add_argument("name")
    _add_json(endpoint)

    admin = commands.add_parser("admin")
    admin_commands = admin.add_subparsers(
        dest="admin_command", required=True, parser_class=_CliParser
    )
    for name in ("fleet", "jobs", "audit"):
        _add_json(admin_commands.add_parser(name))
    proposal = admin_commands.add_parser("proposal")
    proposal.add_argument("--file", type=Path, required=True)
    _add_json(proposal)
    deploy = admin_commands.add_parser("deploy")
    deploy.add_argument("--proposal-digest", required=True)
    deploy.add_argument("--apply", action="store_true")
    _add_json(deploy)
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
        return [_sanitize(item) for item in value[:64]]
    return value


def _arguments_may_contain_secrets(argv: Sequence[str]) -> bool:
    return any(
        _SENSITIVE_OPTION.match(argument.replace("_", "-"))
        or _SENSITIVE_ASSIGNMENT.search(argument)
        or _BEARER.search(argument)
        or "-----BEGIN " in argument
        for argument in argv
    )


def _emit(
    payload: Mapping[str, object],
    args: argparse.Namespace,
    *,
    exact_structure: bool = False,
) -> None:
    safe = dict(payload) if exact_structure else _sanitize(payload)
    assert isinstance(safe, dict)
    if args.global_json or getattr(args, "json", False):
        print(json.dumps(safe, sort_keys=True, separators=(",", ":")))
        return
    priority = (
        "state",
        "status",
        "digest",
        "job_id",
        "id",
        "alias",
        "error",
    )
    keys = [key for key in priority if key in safe]
    keys.extend(sorted(set(safe) - set(keys)))
    for key in keys:
        value = safe[key]
        if value is None:
            rendered = "-"
        elif isinstance(value, (dict, list)):
            rendered = json.dumps(value, sort_keys=True, separators=(",", ":"))
        else:
            rendered = str(value).lower() if isinstance(value, bool) else str(value)
        print(f"{key}: {rendered}")


def _model_payload(result: _GeneratedModel) -> dict[str, object]:
    payload = result.to_dict()
    if not isinstance(payload, dict):
        raise ControlClientError("control API response must be an object")
    return payload


def _admin_payload(result: object) -> dict[str, object]:
    if isinstance(result, Mapping):
        return dict(result)
    return _model_payload(result)  # type: ignore[arg-type]


def _control_error(error: BaseException) -> dict[str, object]:
    if isinstance(error, (ControlUnavailable, ControlTransportError, OSError)):
        message = "control API unavailable"
    else:
        message = _sanitize_text(error)
    return {"error": message, "error_type": "control_api"}


def _admin(
    args: argparse.Namespace,
    client: object,
    request_id_factory: Callable[[], str],
) -> Mapping[str, object]:
    if args.admin_command == "proposal":
        path = args.file
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_048_576:
            raise ControlClientError(
                "proposal input must be a bounded regular non-symlink file"
            )
        payload = json.loads(path.read_bytes())
        if not isinstance(payload, dict):
            raise ControlClientError("proposal input must be a JSON object")
        return client.create_proposal(payload)  # type: ignore[attr-defined, no-any-return]
    if args.admin_command == "deploy":
        if args.apply:
            return client.submit_change(args.proposal_digest)  # type: ignore[attr-defined, no-any-return]
        return {
            "mode": "plan",
            "proposal_digest": args.proposal_digest,
            "apply": False,
        }
    endpoint = {
        "fleet": "/api/v1/fleet",
        "jobs": "/api/v1/jobs",
        "audit": "/api/v1/audit",
    }[args.admin_command]
    return client.get(endpoint)  # type: ignore[attr-defined, no-any-return]


def _routine(
    args: argparse.Namespace,
    client: _RoutineControlClient,
    request_id_factory: Callable[[], str],
) -> dict[str, object]:
    if args.command == "nodes":
        return _model_payload(client.nodes())
    if args.command == "endpoint":
        return _model_payload(client.endpoint(args.name))
    raise ControlClientError("unsupported routine command")


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
        error_args = argparse.Namespace(global_json="--json" in raw_argv, json=False)
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
        if args.command == "admin":
            result = _admin(
                args,
                client,
                request_id_factory or (lambda: str(uuid.uuid4())),
            )
        else:
            result = _routine(
                args,
                client,  # type: ignore[arg-type]
                request_id_factory or (lambda: str(uuid.uuid4())),
            )
        _emit(result, args, exact_structure=args.command != "admin")
        return 0
    except (ControlClientError, OSError, ValueError, json.JSONDecodeError) as error:
        _emit(_control_error(error), args)
        return 2
