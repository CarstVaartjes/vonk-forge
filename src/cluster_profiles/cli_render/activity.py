"""Terminal presentation for activity responses."""

from __future__ import annotations

import shlex
from collections.abc import Mapping

from .common import (
    _actions,
    _field,
    _optional,
    _records,
    _table,
    _text,
    _time,
    _waiting,
    _warn,
    _words,
)


def _age(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "unavailable"
    return f"{value:.1f}"


def _locks(payload: Mapping[str, object]) -> None:
    held = _records(payload, "held")
    if not held:
        print("No admission locks are held.")
    else:
        _table(
            ("Node", "Holder", "State", "Age (s)", "Query"),
            [
                (
                    row.get("node_id") or row.get("namespace"),
                    row.get("holder"),
                    row.get("state"),
                    _age(row.get("transaction_age_seconds")),
                    row.get("query"),
                )
                for row in held
            ],
        )
    transactions = _records(payload, "open_transactions")
    if transactions:
        print()
        print("Open transactions:")
        _table(
            ("Application", "State", "Age (s)", "Query"),
            [
                (
                    row.get("application_name"),
                    row.get("state"),
                    _age(row.get("transaction_age_seconds")),
                    row.get("query"),
                )
                for row in transactions
            ],
        )


def _activity(
    payload: Mapping[str, object], filters: Mapping[str, object] | None
) -> None:
    if (
        payload.get("operations") is None
        and payload.get("total") is None
        and isinstance(payload.get("projection_issue"), str)
    ):
        _field("Activity observation", payload["projection_issue"])
        return
    operations = _records(payload, "operations")
    total = payload.get("total")
    if type(total) is not int or total < 0:
        total = "unavailable"
    _field("Activity", f"{len(operations)} of {total} references on this page")
    if not operations:
        print("No activity matches this request.")
    for operation in operations:
        _field("Operation", operation.get("id"))
        _field("Kind", operation.get("kind"))
        _field("State", operation.get("state"))
        if operation.get("projection_issues") is not None:
            for issue in _records(operation, "projection_issues"):
                _warn(
                    f"{_text(issue.get('field'))} unavailable: response requires "
                    f"{_text(issue.get('observed_bytes'))} bytes; "
                    f"reader budget {_text(issue.get('budget_bytes'))} bytes. "
                    "Operation identity and state remain known."
                )
        _field("Created", _time(operation.get("created_at")))
        _field("Targets", _words(operation.get("node_ids")))
        if operation.get("attempt") is not None:
            _field("Attempt", operation.get("attempt"))
        owner = _optional(operation.get("owner"), "operation owner")
        if owner:
            _field("Owner", f"{_text(owner.get('kind'))} {_text(owner.get('id'))}")
            if owner.get("request_id") is not None:
                _field("Request", owner.get("request_id"))
            owner_kind = owner.get("kind")
            owner_id = owner.get("id")
            reconnect = None
            if isinstance(owner_id, str) and owner_id:
                if owner_kind == "job":
                    reconnect = ["vonkctl", "fleet", "progress", owner_id, "--follow"]
                elif owner_kind == "model-cache-operation":
                    reconnect = ["vonkctl", "model", "progress", owner_id, "--follow"]
                elif owner_kind == "fleet-profile-application":
                    reconnect = [
                        "vonkctl",
                        "profile",
                        "progress",
                        "--application",
                        owner_id,
                        "--follow",
                    ]
            _field(
                "Reconnect",
                "unavailable" if reconnect is None else shlex.join(reconnect),
            )
        cancellation = _optional(operation.get("cancellation"), "profile cancellation")
        if cancellation:
            _field(
                "Cancellation",
                f"{_text(cancellation.get('state'))} ({_text(cancellation.get('cause'))})",
            )
            _field("Cancellation request", cancellation.get("request_key"))
            _field("Cancellation actor", cancellation.get("actor"))
            if cancellation.get("owner") is not None:
                _field("Cancellation owner", cancellation.get("owner"))
            if cancellation.get("dependency") is not None:
                _field("Cancellation dependency", cancellation.get("dependency"))
            if cancellation.get("deadline_at") is not None:
                _field("Cancellation deadline", _time(cancellation.get("deadline_at")))
            for field, label in (
                ("completed_effects", "Completed effect"),
                ("pending_effects", "Pending effect"),
                ("cancelled_effects", "Cancelled or unissued effect"),
            ):
                effects = _records(cancellation, field)
                _field(label + " count", len(effects))
                for effect in effects:
                    _field(
                        label,
                        f"{_text(effect.get('outcome'))}: "
                        f"{_text(effect.get('kind'))} "
                        f"{_text(effect.get('effect_id'))}: "
                        f"{_text(effect.get('label'))}",
                    )
        failure = _optional(operation.get("failure"), "operation failure")
        if failure:
            _warn(
                "Blocker: "
                + _text(failure.get("code", failure.get("error_code")))
                + ": "
                + _text(failure.get("detail", failure.get("summary")))
            )
        if operation.get("status_reason") is not None:
            _warn(f"Reason: {_text(operation['status_reason'])}")
        _waiting(operation)
        recovery = _optional(operation.get("recovery"), "operation recovery")
        _actions(recovery.get("actions"))
        print()

    cursor = payload.get("next_cursor")
    if cursor is None:
        _field("More results", "no")
        return
    if not isinstance(cursor, str) or not cursor or len(cursor) > 512:
        _field("More results", "unavailable")
        return
    command = ["vonkctl", "fleet", "activity"]
    query_filters = filters or {}
    limit = query_filters.get("limit", 20)
    command.extend(("--limit", str(limit), "--cursor", cursor))
    for key, flag in (
        ("state", "--state"),
        ("target", "--target"),
        ("request_id", "--request-id"),
    ):
        value = query_filters.get(key)
        if isinstance(value, str) and value:
            command.extend((flag, value))
    print("More results are available. Continue with:")
    print(shlex.join(command))
