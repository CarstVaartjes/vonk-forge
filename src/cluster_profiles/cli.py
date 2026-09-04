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


class _GeneratedModel(Protocol):
    def to_dict(self) -> dict[str, object]: ...


class _RoutineControlClient(Protocol):
    def nodes(self) -> _GeneratedModel: ...

    def endpoint(self, alias: str) -> _GeneratedModel: ...


def _runs_without_controller(args: argparse.Namespace) -> bool:
    if args.command == "admin" and args.admin_command == "deploy":
        return not args.apply
    if args.command == "library" and args.library_command == "template":
        return True
    if (
        args.command == "library"
        and args.library_command == "job"
        and args.artifact_job_command == "download"
    ):
        return False
    return hasattr(args, "apply") and not args.apply


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


def _table_cell(value: object, *, maximum: int = 48) -> str:
    if value is None:
        return "-"
    if isinstance(value, list):
        text = ", ".join(str(item) for item in value)
    elif isinstance(value, Mapping):
        text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= maximum else text[: maximum - 1] + "…"


def _print_table(
    rows: list[Mapping[str, object]], columns: tuple[tuple[str, str], ...]
) -> None:
    rendered = [[_table_cell(row.get(key)) for key, _label in columns] for row in rows]
    widths = [
        max(len(label), *(len(row[index]) for row in rendered))
        for index, (_key, label) in enumerate(columns)
    ]
    print(
        "  ".join(
            label.ljust(widths[index]) for index, (_key, label) in enumerate(columns)
        )
    )
    print("  ".join("-" * width for width in widths))
    for row in rendered:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def _emit_list_table(payload: Mapping[str, object]) -> bool:
    facets = payload.get("facets")
    if isinstance(facets, Mapping):
        for facet, raw_options in facets.items():
            if not isinstance(raw_options, list):
                continue
            print(str(facet).replace("_", " ").title())
            rows = [option for option in raw_options if isinstance(option, Mapping)]
            if rows:
                _print_table(rows, (("value", "VALUE"), ("count", "COUNT")))
            else:
                print("No available values.")
            print()
        if "matching_count" in payload:
            print(f"matching_count: {_table_cell(payload['matching_count'])}")
        return True

    models = payload.get("models")
    if isinstance(models, list):
        library_rows: list[Mapping[str, object]] = []
        for model in models:
            if not isinstance(model, Mapping):
                continue
            identity = model.get("model")
            if isinstance(identity, Mapping):
                model_name = (
                    f"{identity.get('publisher', '-')}/{identity.get('slug', '-')}"
                )
            else:
                model_name = "-"
            recipes = model.get("recipes")
            if isinstance(recipes, list):
                library_rows.extend(
                    {**recipe, "model_name": model_name}
                    for recipe in recipes
                    if isinstance(recipe, Mapping)
                )
        unlinked = payload.get("unlinked_recipes")
        if isinstance(unlinked, list):
            library_rows.extend(
                {**recipe, "model_name": "Unlinked"}
                for recipe in unlinked
                if isinstance(recipe, Mapping)
            )
        if library_rows:
            _print_table(
                library_rows,
                (
                    ("model_name", "MODEL"),
                    ("title", "RECIPE"),
                    ("source_kind", "SOURCE"),
                    ("topology_name", "TOPOLOGY"),
                    ("recipe_id", "RECIPE ID"),
                ),
            )
        else:
            print("No models or recipes.")
        if payload.get("next_cursor") is not None:
            print(f"next_cursor: {_table_cell(payload['next_cursor'])}")
        return True

    recipes = payload.get("recipes")
    if isinstance(recipes, list):
        recipe_rows = [recipe for recipe in recipes if isinstance(recipe, Mapping)]
        identity = (
            "uri" if any("uri" in recipe for recipe in recipe_rows) else "recipe_id"
        )
        if recipe_rows:
            _print_table(
                recipe_rows,
                (
                    ("title", "RECIPE"),
                    ("qualification", "QUALIFICATION"),
                    ("execution_readiness", "READINESS"),
                    ("node_count", "SPARKS"),
                    (identity, "URI" if identity == "uri" else "RECIPE ID"),
                ),
            )
        else:
            print("No recipes.")
        for metadata in ("filtered_count", "next_cursor"):
            if metadata in payload and payload[metadata] is not None:
                print(f"{metadata}: {_table_cell(payload[metadata])}")
        return True

    candidates: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
        (
            "nodes",
            (
                ("display_name", "NAME"),
                ("operational_state", "HEALTH"),
                ("lifecycle", "LIFECYCLE"),
                ("id", "NODE ID"),
            ),
        ),
        (
            "jobs",
            (
                ("created_at", "CREATED"),
                ("kind", "KIND"),
                ("state", "STATE"),
                ("id", "JOB ID"),
            ),
        ),
        (
            "events",
            (
                ("label", "EVENT"),
                ("area", "AREA"),
                ("status", "STATUS"),
                ("occurred_at", "OCCURRED"),
                ("actor", "OPERATOR"),
                ("request_id", "REQUEST ID"),
            ),
        ),
        (
            "agents",
            (
                ("node_id", "NODE ID"),
                ("state", "STATE"),
                ("semantic_version", "VERSION"),
                ("last_seen_at", "LAST SEEN"),
            ),
        ),
        (
            "enrollments",
            (
                ("created_at", "CREATED"),
                ("state", "STATE"),
                ("node_id", "NODE ID"),
                ("id", "ENROLLMENT ID"),
            ),
        ),
    )
    for key, columns in candidates:
        value = payload.get(key)
        if not isinstance(value, list):
            continue
        rows = [row for row in value if isinstance(row, Mapping)]
        if rows:
            _print_table(rows, columns)
        else:
            print(f"No {key}.")
        for metadata in ("filtered_count", "next_cursor", "total"):
            if metadata in payload and payload[metadata] is not None:
                print(f"{metadata}: {_table_cell(payload[metadata])}")
        return True
    return False


def _emit_agent_upgrade_detail(payload: Mapping[str, object]) -> bool:
    diagnostics = payload.get("agent_upgrade_diagnostics")
    if payload.get("kind") != "agent-upgrade" or not isinstance(diagnostics, Mapping):
        return False
    print(f"state: {_table_cell(payload.get('state'), maximum=80)}")
    if payload.get("status_reason"):
        print(f"summary: {payload['status_reason']}")
    expected = diagnostics.get("expected_identity")
    if isinstance(expected, Mapping):
        print(f"expected_release: {_table_cell(expected.get('version'), maximum=128)}")
        print(
            f"expected_binary_digest: {_table_cell(expected.get('binary_digest'), maximum=80)}"
        )
        print(
            f"expected_build_digest: {_table_cell(expected.get('build_digest'), maximum=80)}"
        )
    targets = diagnostics.get("targets")
    if isinstance(targets, list):
        for target in targets:
            if not isinstance(target, Mapping):
                continue
            node_id = _table_cell(target.get("node_id"), maximum=128)
            print(f"spark: {node_id}")
            print(f"  install_attempts: {_table_cell(target.get('attempts'))}")
            print(f"  target_proven: {str(bool(target.get('target_proven'))).lower()}")
            observed = target.get("observed_identity")
            if isinstance(observed, Mapping):
                print(
                    f"  observed_version: {_table_cell(observed.get('version'), maximum=128)}"
                )
                print(
                    f"  observed_binary_digest: {_table_cell(observed.get('binary_digest'), maximum=80)}"
                )
                print(
                    f"  observed_build_digest: {_table_cell(observed.get('build_digest'), maximum=80)}"
                )
            if target.get("raw_reason"):
                print(f"  raw_helper_reason: {target['raw_reason']}")
            if target.get("retry_not_before"):
                print(f"  retry_not_before: {target['retry_not_before']}")
                print(
                    f"  retry_queued: {str(target.get('retry_queued') is True).lower()}"
                )
    if diagnostics.get("legacy_generic_ambiguous") is True:
        print(
            "diagnosis: legacy helper response is ambiguous; the exact target "
            "identity remains the success gate"
        )
    if diagnostics.get("next_action"):
        print(f"next_action: {diagnostics['next_action']}")
    return True


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
    if (
        getattr(args, "command", None) == "library"
        and getattr(args, "library_command", None) == "template"
    ):
        print(json.dumps(safe, sort_keys=True, indent=2))
        return
    if _emit_agent_upgrade_detail(safe):
        return
    if _emit_list_table(safe):
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


def _control_error(
    error: BaseException, args: argparse.Namespace | None = None
) -> dict[str, object]:
    if isinstance(error, (ControlUnavailable, ControlTransportError, OSError)):
        message = "control API unavailable"
    else:
        message = _sanitize_text(error)
    result: dict[str, object] = {"error": message, "error_type": "control_api"}
    request_key = getattr(args, "request_key", None) if args is not None else None
    operation_id = getattr(args, "operation_id", None) if args is not None else None
    if isinstance(request_key, str) and request_key:
        result["request_key"] = request_key
        result["reconcile"] = {
            "operation": "inspect the durable operation with the same request key",
            "request_key": request_key,
        }
        if isinstance(operation_id, str) and operation_id:
            result["operation_id"] = operation_id
            result["reconcile"]["operation_id"] = operation_id
    return result


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
        client = control_client
        if client is None and not _runs_without_controller(args):
            client = ControlClient.from_environment()
        if args.command == "admin":
            result = _admin(
                args,
                client,
                request_id_factory or (lambda: str(uuid.uuid4())),
            )
        elif args.command in {
            "fleet",
            "library",
            "activity",
            "models",
            "cache",
            "profiles",
            "operations",
        }:
            result = run_controller(
                args,
                client,  # type: ignore[arg-type]
                request_id_factory or (lambda: str(uuid.uuid4())),
            )
        else:
            result = _routine(
                args,
                client,  # type: ignore[arg-type]
                request_id_factory or (lambda: str(uuid.uuid4())),
            )
        _emit(
            result,
            args,
            exact_structure=args.command
            not in {"admin", "fleet", "library", "activity"},
        )
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
