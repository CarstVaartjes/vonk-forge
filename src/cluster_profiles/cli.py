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
_PLATFORM_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PACKAGE_CANDIDATE = re.compile(r"[0-9a-f]{64}\Z")
_PACKAGE_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,126}\Z")
_UPDATE_RELEASE = re.compile(
    r"platform/releases/"
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)/"
    r"[0-9a-f]{64}\.json\Z"
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
    updates = admin_commands.add_parser("updates")
    update_commands = updates.add_subparsers(
        dest="updates_command", required=True, parser_class=_CliParser
    )
    _add_json(update_commands.add_parser("skew"))
    update_plan = update_commands.add_parser("plan")
    update_plan.add_argument("--release", required=True)
    _add_json(update_plan)
    update_apply = update_commands.add_parser("apply")
    update_apply.add_argument("--plan-digest", required=True)
    _add_json(update_apply)
    update_status = update_commands.add_parser("status")
    update_status.add_argument("job_id")
    _add_json(update_status)
    packages = admin_commands.add_parser("packages")
    package_commands = packages.add_subparsers(
        dest="packages_command", required=True, parser_class=_CliParser
    )
    package_candidates = package_commands.add_parser("candidates")
    package_candidate_commands = package_candidates.add_subparsers(
        dest="package_candidates_command", required=True, parser_class=_CliParser
    )
    package_list = package_candidate_commands.add_parser("list")
    package_list.add_argument("--family")
    package_list.add_argument("--cursor")
    package_list.add_argument("--limit", type=int, default=20)
    _add_json(package_list)
    for name in ("get", "resolution", "compatibility"):
        package_read = package_candidate_commands.add_parser(name)
        package_read.add_argument("--candidate", required=True)
        _add_json(package_read)
    package_families = package_commands.add_parser("families")
    package_families.add_argument("--cursor")
    package_families.add_argument("--limit", type=int, default=20)
    _add_json(package_families)
    package_preview = package_commands.add_parser("promote-preview")
    package_preview.add_argument("--candidate", required=True)
    _add_json(package_preview)
    package_validation_preview = package_commands.add_parser("validation-preview")
    package_validation_preview.add_argument("--candidate", required=True)
    _add_json(package_validation_preview)
    package_validate = package_commands.add_parser("validate")
    package_validate.add_argument("--candidate", required=True)
    package_validate.add_argument("--plan-digest", required=True)
    _add_json(package_validate)
    package_validation_status = package_commands.add_parser("validation-status")
    package_validation_status.add_argument("--validation", required=True)
    _add_json(package_validation_status)
    package_promote = package_commands.add_parser("promote")
    package_promote.add_argument("--candidate", required=True)
    package_promote.add_argument("--preview-digest", required=True)
    _add_json(package_promote)
    package_gc_preview = package_commands.add_parser("gc-preview")
    _add_json(package_gc_preview)
    package_gc = package_commands.add_parser("gc")
    package_gc.add_argument("--plan-digest", required=True)
    _add_json(package_gc)
    deployments = admin_commands.add_parser("deployments")
    deployment_commands = deployments.add_subparsers(
        dest="deployments_command", required=True, parser_class=_CliParser
    )
    deployment_list = deployment_commands.add_parser("list")
    deployment_list.add_argument("--cursor")
    deployment_list.add_argument("--limit", type=int, default=20)
    _add_json(deployment_list)
    deployment_get = deployment_commands.add_parser("get")
    deployment_get.add_argument("--deployment", required=True)
    _add_json(deployment_get)
    deployment_rollout_preview = deployment_commands.add_parser("rollout-preview")
    deployment_rollout_preview.add_argument("--deployment", required=True)
    _add_json(deployment_rollout_preview)
    deployment_rollout = deployment_commands.add_parser("rollout")
    deployment_rollout.add_argument("--deployment", required=True)
    deployment_rollout.add_argument("--plan-digest", required=True)
    _add_json(deployment_rollout)
    deployment_status = deployment_commands.add_parser("status")
    deployment_status.add_argument("--deployment", required=True)
    deployment_status.add_argument("--rollout", required=True)
    _add_json(deployment_status)
    deployment_rollback_preview = deployment_commands.add_parser("rollback-preview")
    deployment_rollback_preview.add_argument("--deployment", required=True)
    deployment_rollback_preview.add_argument("--rollout", required=True)
    _add_json(deployment_rollback_preview)
    deployment_rollback = deployment_commands.add_parser("rollback")
    deployment_rollback.add_argument("--deployment", required=True)
    deployment_rollback.add_argument("--rollout", required=True)
    deployment_rollback.add_argument("--plan-digest", required=True)
    _add_json(deployment_rollback)
    deployment_repair_preview = deployment_commands.add_parser("repair-preview")
    deployment_repair_preview.add_argument("--deployment", required=True)
    _add_json(deployment_repair_preview)
    deployment_repair = deployment_commands.add_parser("repair")
    deployment_repair.add_argument("--deployment", required=True)
    deployment_repair.add_argument("--plan-digest", required=True)
    _add_json(deployment_repair)
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
    if args.admin_command == "updates":
        if args.updates_command == "skew":
            return _model_payload(client.update_skew())  # type: ignore[attr-defined]
        if args.updates_command == "plan":
            if (
                len(args.release) > 512
                or _UPDATE_RELEASE.fullmatch(args.release) is None
            ):
                raise ControlClientError("platform update release name is invalid")
            return _model_payload(client.plan_update(args.release))  # type: ignore[attr-defined]
        if args.updates_command == "apply":
            if _PLATFORM_DIGEST.fullmatch(args.plan_digest) is None:
                raise ControlClientError("platform update plan digest is invalid")
            return _model_payload(client.apply_update(args.plan_digest))  # type: ignore[attr-defined]
        try:
            rollout_id = str(uuid.UUID(args.job_id))
        except ValueError:
            raise ControlClientError("platform update rollout ID is invalid") from None
        if rollout_id != args.job_id:
            raise ControlClientError("platform update rollout ID is invalid")
        return _model_payload(client.update_status(rollout_id))  # type: ignore[attr-defined]
    if args.admin_command == "packages":
        if args.packages_command == "candidates":
            if args.package_candidates_command == "list":
                if args.family is not None and _PACKAGE_IDENTIFIER.fullmatch(args.family) is None:
                    raise ControlClientError("package family ID is invalid")
                return _admin_payload(client.package_candidates(args.family, cursor=args.cursor, limit=args.limit))  # type: ignore[attr-defined]
            if _PACKAGE_CANDIDATE.fullmatch(args.candidate) is None:
                raise ControlClientError("package candidate ID is invalid")
            method = {
                "get": client.package_candidate,
                "resolution": client.package_resolution,
                "compatibility": client.package_compatibility,
            }[args.package_candidates_command]
            return _admin_payload(method(args.candidate))
        if args.packages_command == "families":
            return _admin_payload(client.package_families(cursor=args.cursor, limit=args.limit))  # type: ignore[attr-defined]
        if args.packages_command == "validation-status":
            try:
                validation_id = str(uuid.UUID(args.validation))
            except ValueError:
                raise ControlClientError("package validation ID is invalid") from None
            if validation_id != args.validation:
                raise ControlClientError("package validation ID is invalid")
            return _model_payload(client.package_validation(validation_id))  # type: ignore[attr-defined]
        if args.packages_command in {"gc-preview", "gc"}:
            if args.packages_command == "gc-preview":
                return _model_payload(client.preview_package_gc())  # type: ignore[attr-defined]
            if _PLATFORM_DIGEST.fullmatch(args.plan_digest) is None:
                raise ControlClientError("package garbage collection plan digest is invalid")
            return _model_payload(client.apply_package_gc(args.plan_digest, request_id=request_id_factory()))  # type: ignore[attr-defined]
        if _PACKAGE_CANDIDATE.fullmatch(args.candidate) is None:
            raise ControlClientError("package candidate ID is invalid")
        if args.packages_command == "promote-preview":
            return _model_payload(client.preview_package_promotion(args.candidate))  # type: ignore[attr-defined]
        if args.packages_command == "validation-preview":
            return _model_payload(client.preview_package_validation(args.candidate))  # type: ignore[attr-defined]
        if args.packages_command == "validate":
            if _PLATFORM_DIGEST.fullmatch(args.plan_digest) is None:
                raise ControlClientError("package validation plan digest is invalid")
            return _model_payload(client.validate_package(args.candidate, args.plan_digest, request_id=request_id_factory()))  # type: ignore[attr-defined]
        if _PLATFORM_DIGEST.fullmatch(args.preview_digest) is None:
            raise ControlClientError("package preview digest is invalid")
        return _model_payload(client.promote_package(args.candidate, args.preview_digest, request_id=request_id_factory()))  # type: ignore[attr-defined]
    if args.admin_command == "deployments":
        if args.deployments_command == "list":
            return _admin_payload(client.package_deployments(cursor=args.cursor, limit=args.limit))  # type: ignore[attr-defined]
        if _PACKAGE_IDENTIFIER.fullmatch(args.deployment) is None:
            raise ControlClientError("package deployment ID is invalid")
        if args.deployments_command == "get":
            return _admin_payload(client.package_deployment(args.deployment))  # type: ignore[attr-defined]
        if args.deployments_command in {"status", "rollback-preview", "rollback"}:
            try:
                rollout_id = str(uuid.UUID(args.rollout))
            except ValueError:
                raise ControlClientError("package rollout ID is invalid") from None
            if rollout_id != args.rollout:
                raise ControlClientError("package rollout ID is invalid")
            if args.deployments_command == "status":
                return _model_payload(client.package_rollout(args.deployment, rollout_id))  # type: ignore[attr-defined]
            if args.deployments_command == "rollback-preview":
                return _model_payload(client.preview_deployment_rollback(args.deployment, rollout_id))  # type: ignore[attr-defined]
            if _PLATFORM_DIGEST.fullmatch(args.plan_digest) is None:
                raise ControlClientError("package rollback plan digest is invalid")
            return _model_payload(client.rollback_deployment(args.deployment, rollout_id, args.plan_digest, request_id=request_id_factory()))  # type: ignore[attr-defined]
        if args.deployments_command == "rollout-preview":
            return _model_payload(client.preview_deployment_rollout(args.deployment))  # type: ignore[attr-defined]
        if args.deployments_command == "repair-preview":
            return _model_payload(client.preview_deployment_repair(args.deployment))  # type: ignore[attr-defined]
        if _PLATFORM_DIGEST.fullmatch(args.plan_digest) is None:
            raise ControlClientError("package deployment plan digest is invalid")
        if args.deployments_command == "repair":
            return _model_payload(client.repair_deployment(args.deployment, args.plan_digest, request_id=request_id_factory()))  # type: ignore[attr-defined]
        return _admin_payload(client.rollout_deployment(args.deployment, args.plan_digest, request_id=request_id_factory()))  # type: ignore[attr-defined]
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
