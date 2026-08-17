"""Command-line interface for resumable per-node installation."""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cluster_profiles.fleet import ManagementEndpoint, NodeId
from cluster_profiles.fleet.install_contracts import (
    InstallationJournal,
    InstallationRequest,
    InvalidInstallationTransition,
)

from .orchestrator import FileEvidenceStore, NodeInstaller
from .store import InstallConflict, InstallStore, InstallStoreError


class CliUsageError(ValueError):
    pass


@dataclass(frozen=True)
class CliDependencies:
    installer: NodeInstaller
    node_id_factory: Callable[[], NodeId]


def _node_id() -> NodeId:
    return NodeId.parse(f"spk_{secrets.token_hex(16)}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="node-install")
    commands = parser.add_subparsers(dest="scope", required=True)
    node = commands.add_parser("node")
    node_commands = node.add_subparsers(dest="command", required=True)

    start = node_commands.add_parser("start")
    start.add_argument("--host", required=True)
    start.add_argument("--user", required=True)
    start.add_argument("--port", type=int, default=22)
    start.add_argument("--credential-ref", required=True)
    start.add_argument("--display-name", required=True)
    start.add_argument("--label", action="append", default=[])
    start.add_argument("--apply", action="store_true")
    start.add_argument("--json", action="store_true")

    def production_inputs(command: argparse.ArgumentParser) -> None:
        command.add_argument("--admin-public-key")
        command.add_argument("--admin-key-fingerprint")
        command.add_argument("--trusted-serial-sha256")
        command.add_argument(
            "--trusted-host-key-fingerprint", action="append", default=[]
        )
        command.add_argument("--recovery-verified", action="store_true")

    production_inputs(start)

    for name in ("status", "resume", "retry", "verify", "emit-record"):
        command = node_commands.add_parser(name)
        command.add_argument("node_id")
        if name in {"resume", "retry"}:
            command.add_argument("--apply", action="store_true")
            production_inputs(command)
        command.add_argument("--json", action="store_true")
    return parser


def _labels(values: Sequence[str]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise CliUsageError("labels must use KEY=VALUE")
        key, label_value = value.split("=", 1)
        if not key.strip() or not label_value.strip():
            raise CliUsageError("label keys and values must not be blank")
        if key in labels:
            raise CliUsageError(f"duplicate label: {key}")
        labels[key] = label_value
    return labels


def _journal_payload(
    journal: InstallationJournal,
    *,
    mode: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "node_id": journal.request.node_id.value,
        "display_name": journal.request.display_name,
        "host": journal.request.endpoint.host,
        "state": journal.state,
        "waiting_reason": journal.waiting_reason,
        "failure_reason": journal.failure_reason,
        "completed_gates": len(journal.steps),
        "retry_count": journal.retry_count,
        "resume_count": journal.resume_count,
    }
    if mode is not None:
        payload["mode"] = mode
    return payload


def _emit(payload: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    for key, value in payload.items():
        print(f"{key}={value}")


def build_dependencies(root: Path, arguments: argparse.Namespace) -> CliDependencies:
    """Build live dependencies only after a mutating/inspection command parses."""

    from .remote import OpenSshInstallTransport
    from .steps import ProductionStepOptions, build_production_handlers

    state_root = root / ".state" / "node-install"
    clock = lambda: datetime.now(UTC)
    store = InstallStore(state_root / "journals", clock=clock)
    transport = OpenSshInstallTransport()
    options = ProductionStepOptions.from_arguments(arguments, root=root)
    installer = NodeInstaller(
        store=store,
        evidence_store=FileEvidenceStore(state_root / "evidence"),
        handlers=build_production_handlers(options, transport),
        clock=clock,
    )
    return CliDependencies(installer=installer, node_id_factory=_node_id)


def main(
    argv: Sequence[str] | None = None,
    *,
    root: Path | None = None,
    dependencies: Callable[[], CliDependencies] | None = None,
) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    repository_root = root or Path(__file__).resolve().parents[3]
    cached_dependencies: CliDependencies | None = None

    def get_dependencies() -> CliDependencies:
        nonlocal cached_dependencies
        if cached_dependencies is None:
            cached_dependencies = (
                dependencies()
                if dependencies is not None
                else build_dependencies(repository_root, arguments)
            )
        return cached_dependencies

    try:
        if arguments.command == "start":
            node_id = (
                get_dependencies().node_id_factory()
                if dependencies is not None or arguments.apply
                else _node_id()
            )
            request = InstallationRequest(
                node_id=node_id,
                display_name=arguments.display_name,
                endpoint=ManagementEndpoint(
                    host=arguments.host,
                    user=arguments.user,
                    port=arguments.port,
                    credential_ref=arguments.credential_ref,
                ),
                labels=_labels(arguments.label),
            )
            if not arguments.apply:
                payload = request.as_public_dict()
                payload["mode"] = "plan"
                _emit(payload, json_output=arguments.json)
                return 0
            deps = get_dependencies()
            journal = deps.installer.run(
                deps.installer.start(request).request.node_id
            )
            _emit(_journal_payload(journal, mode="apply"), json_output=arguments.json)
            return 3 if journal.state == "failed" else 0

        node_id = NodeId.parse(arguments.node_id)
        deps = get_dependencies()
        if arguments.command == "status":
            journal = deps.installer.status(node_id)
        elif arguments.command == "resume":
            if not arguments.apply:
                journal = deps.installer.status(node_id)
                _emit(_journal_payload(journal, mode="plan"), json_output=arguments.json)
                return 0
            deps.installer.resume(node_id)
            journal = deps.installer.run(node_id)
        elif arguments.command == "retry":
            if not arguments.apply:
                journal = deps.installer.status(node_id)
                _emit(_journal_payload(journal, mode="plan"), json_output=arguments.json)
                return 0
            deps.installer.retry(node_id)
            journal = deps.installer.run(node_id)
        elif arguments.command == "verify":
            journal = deps.installer.status(node_id)
        elif arguments.command == "emit-record":
            from .records import emit_node_record

            journal = deps.installer.status(node_id)
            sys.stdout.buffer.write(emit_node_record(journal))
            return 0
        else:
            raise CliUsageError(f"unsupported command: {arguments.command}")
        _emit(_journal_payload(journal), json_output=arguments.json)
        if arguments.command == "verify" and journal.state != "accepted":
            return 3
        return 3 if journal.state == "failed" else 0
    except (CliUsageError, ValueError, InstallConflict, InstallStoreError, InvalidInstallationTransition) as error:
        print(f"node-install: {error}", file=sys.stderr)
        return 2
