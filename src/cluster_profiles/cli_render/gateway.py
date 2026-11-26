"""Terminal presentation of external models, credentials and their reviews."""

from __future__ import annotations

import shlex
from collections.abc import Mapping

from cluster_profiles.cli_json_contract import Word
from cluster_profiles.cli_render.common import (
    _field,
    _object,
    _optional,
    _records,
    _table,
    _text,
    _time,
    _words,
)


def _destination(value: object) -> str:
    if not isinstance(value, Mapping):
        return _text(None)
    origins = _words(value.get("origins"))
    region = value.get("region")
    return origins if region is None else f"{origins} ({_text(region)})"


def _approved(destinations: object) -> str:
    if not isinstance(destinations, list):
        return _text(None)
    return (
        ", ".join(
            _text(item.get("origin"))
            + (f" ({_text(item['region'])})" if item.get("region") else "")
            + (" [private]" if item.get("private") else "")
            for item in destinations
            if isinstance(item, Mapping)
        )
        or Word.NONE
    )


def _model_rows(payload) -> None:
    rows = _records(payload, "models")
    if not rows:
        print(
            "No external models. Add one with: vonkctl gateway model put --file ENTRY"
        )
        return
    _table(
        ("ALIAS", "STATE", "SERVING", "DESTINATION", "NEXT CHECK"),
        [
            (
                row.get("alias"),
                row.get("state"),
                row.get("published"),
                _destination(row.get("destination")),
                _time(row.get("next_check_at")),
            )
            for row in rows
        ],
    )


def _model_view(payload) -> None:
    alias = payload.get("alias")
    _field("Alias", alias)
    _field("State", payload.get("state"))
    _field("Serving", payload.get("published"))
    _field("Enabled", payload.get("enabled"))
    _field("Destination", _destination(payload.get("destination")))
    _field("Credentials", _words(payload.get("credential_bindings")))
    _field("Revision", payload.get("desired_revision"))
    _field("Applied revision", payload.get("applied_revision"))
    if payload.get("reason") is not None:
        _field("Reason", payload.get("reason"))
    if payload.get("start_failures"):
        _field("Failed starts", payload.get("start_failures"))
    if payload.get("next_check_at") is not None:
        _field("Next check", _time(payload.get("next_check_at")))
    if payload.get("operator_action") is not None:
        # The Controller gave up on this content and waits for the operator.
        _field(
            "Waiting for you",
            f"{_text(payload.get('operator_action'))}: edit the entry, or run "
            f"vonkctl gateway model enable {shlex.quote(str(alias))}",
        )


def _credential_rows(payload) -> None:
    rows = _records(payload, "bindings")
    if not rows:
        print(
            "No credentials. Approve one with: vonkctl gateway credential put NAME --origin URL"
        )
        return
    _table(
        ("NAME", "REVISION", "LOADED", "DESTINATIONS"),
        [
            (
                row.get("name"),
                row.get("revision"),
                row.get("credential_loaded"),
                _approved(row.get("destinations")),
            )
            for row in rows
        ],
    )


def _problems(problems: object) -> None:
    for problem in problems if isinstance(problems, list) else []:
        if isinstance(problem, Mapping):
            line = f"  {_text(problem.get('path'))}: {_text(problem.get('code'))}"
            if problem.get("limit") is not None:
                line += f" (limit {_text(problem.get('limit'))})"
            print(line)


def _review(review, target: str) -> None:
    problems = review.get("problems")
    if problems:
        print("Refused; nothing was changed:")
        _problems(problems)
        return
    if target == Word.CREDENTIAL:
        _field("Added", _approved(review.get("added")))
        _field("Removed", _approved(review.get("removed")))
        for dependent in _records(review, "dependents"):
            _field(
                "Model",
                f"{_text(dependent.get('alias'))} "
                f"({'withdrawn' if dependent.get('withdrawn') else 'kept'})",
            )
        return
    _field("Destination", _destination(review.get("destination")))
    if review.get("changed_from") is not None:
        _field("Moved from", _destination(review.get("changed_from")))
    _field("Effects", _words(review.get("effects")))
    _field("Gateway grace", f"{_text(review.get('grace_seconds'))} s")


def _change(payload, target: object) -> None:
    review = _object(payload.get("review"), "review")
    _field(str(target).capitalize(), payload.get("name"))
    _field("Action", payload.get("action"))
    _review(review, str(target))
    accepted = _optional(payload.get("accepted"), "accepted")
    if not accepted:
        return
    _field("Revision", accepted.get("revision"))
    if accepted.get("state") is not None:
        _field("State", accepted.get("state"))
    if target == Word.MODEL:
        _field(
            "Next",
            f"vonkctl gateway model show {shlex.quote(str(payload.get('name')))}",
        )


def _gateway_documents(payload) -> None:
    if payload.get("gateway_change"):
        _change(payload, payload.get("target"))
    elif "models" in payload:
        _model_rows(payload)
    elif "bindings" in payload:
        _credential_rows(payload)
    elif Word.ALIAS in payload:
        _model_view(payload)
    else:
        problems = payload.get("problems")
        print("Refused; nothing was sent:" if problems else "The entry is valid.")
        _problems(problems)
