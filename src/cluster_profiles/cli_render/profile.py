"""Terminal presentation for profile responses."""

from __future__ import annotations

import shlex
from collections.abc import Mapping

from .. import cli_states
from .common import (
    _actions,
    _field,
    _freshness,
    _object,
    _optional,
    _reasons,
    _records,
    _table,
    _text,
    _time,
    _words,
    terminal_text,
)
from .library import _option_choices


def _profile(payload: Mapping[str, object]) -> None:
    print(f"Profile {_text(payload.get('number'))}: {_text(payload.get('name'))}")
    _field("Revision", payload.get("revision"))
    _field("Loaded revision", payload.get("loaded_revision"))
    _field("State", payload.get("status"))
    if (
        payload.get("definition") is None
        and payload.get("projection_issue") is not None
    ):
        issue = _object(payload["projection_issue"], "profile projection issue")
        _field("Definition", "unknown")
        _field("Cause", issue.get("detail"))
        _field("Next", issue.get("next_action"))
        return
    _field("Retention", payload.get("installation_policy"))
    if "favorite" in payload:
        _field("Favorite", payload["favorite"])
    if payload.get("description"):
        _field("Description", payload["description"])
    for key, value in _object(payload.get("labels", {}), "labels").items():
        _field("Label", f"{key}={_text(value)}")
    definition = _object(payload.get("definition"), "definition")
    desired = _records(definition, "assignments")
    observed = _records(payload, "assignments")
    if not desired:
        print("No assignments saved.")
    for assignment in desired:
        selector = assignment.get("recipe_selector")
        nodes = assignment.get("spark_ids")
        match = next(
            (
                row
                for row in observed
                if row.get("recipe_selector") == selector
                and row.get("spark_ids") == nodes
            ),
            None,
        )
        _field("Recipe", selector)
        _field("Sparks", _words(nodes))
        _field("Desired", assignment.get("desired_state"))
        _option_choices(assignment.get("option_choices"))
        _field("Observed", match.get("observed_state") if match is not None else None)
        update = match.get("recipe_update") if match is not None else None
        if isinstance(update, Mapping):
            _field("Update", update.get("detail"))
    _reasons(payload.get("warnings"))
    _actions(payload.get("next_actions"))


def _profile_endpoints(payload: Mapping[str, object]) -> None:
    number = payload.get("number")
    application_id = payload.get("application_id")
    if application_id is None:
        print(f"Profile {number} has no loaded application.")
        print("Saved profile edits do not publish routes until the profile is loaded.")
        return
    _field("Loaded application", application_id)
    _field("Application state", payload.get("application_state"))
    issue = _optional(payload.get("projection_issue"), "projection_issue")
    if issue:
        print(
            "Endpoint assignments are unavailable because stored application history is invalid."
        )
        _field("Stored evidence", issue.get("detail"))
        return
    assignments = _records(payload, "assignments")
    if not assignments:
        print("The loaded application has no endpoint assignments.")
        return
    for assignment in assignments:
        print()
        _field("Assignment", assignment.get("recipe_title"))
        _field("Endpoint state", assignment.get("state"))
        alias = assignment.get("alias")
        endpoint = _optional(assignment.get("endpoint"), "endpoint")
        if assignment.get("state") == cli_states.PUBLISHED:
            _field("Client model identifier", alias)
            api_base = endpoint.get("api_base")
            _field("API base (inference gateway)", api_base)
            _field("Route generation", endpoint.get("generation"))
            _field("Route observed at", _time(endpoint.get("observed_at")))
            _field(
                "Freshness",
                _freshness(endpoint.get("observed_at"), payload.get("observed_at")),
            )
            _field("Spark backend (diagnostic)", endpoint.get("backend_api_base"))
            if isinstance(api_base, str) and isinstance(alias, str):
                print("Client configuration:")
                print(
                    "  export OPENAI_BASE_URL=" + terminal_text(shlex.quote(api_base))
                )
                print('  export OPENAI_API_KEY="$(cat CLIENT_KEY_FILE)"')
                print("  model: " + terminal_text(alias))
                print(
                    "Create a client key with: "
                    "vonkctl key create NAME --output CLIENT_KEY_FILE"
                )
            continue
        if alias is not None:
            _field("Client model identifier", alias)
        messages = {
            cli_states.ENDPOINT_INSTALLED_ONLY: "Install-only assignment; no published endpoint was requested.",
            cli_states.ENDPOINT_NOT_PUBLISHED_YET: "The current assignment route is not published yet.",
            cli_states.ENDPOINT_EXPIRED: "The published route lease has expired.",
            cli_states.ENDPOINT_WITHDRAWN: "No current published route is associated with this assignment.",
            cli_states.ENDPOINT_UNAVAILABLE: "The Controller cannot verify a current route for this assignment.",
        }
        print(
            messages.get(str(assignment.get("state")), "Endpoint state is unavailable.")
        )


def _gateway_keys(payload: Mapping[str, object], action: object) -> None:
    if action in ("create", "roll"):
        _field("Key", payload.get("name"))
        _field("Models", _words(payload.get("models") or ["all"]))
        _field("Expires", payload.get("expires_at") or "never")
        if payload.get("output") is not None:
            _field("Written to", payload.get("output"))
        else:
            print(_text(payload.get("key")))
            print("Store this key now; it is not shown again.")
    elif action == "revoke":
        print(f"Revoked key {_text(payload.get('name'))}.")
    else:
        rows = _records(payload, "keys")
        if not rows:
            print("No gateway client keys. Create one with: vonkctl key create NAME")
            return
        _table(
            ("NAME", "MODELS", "CREATED", "EXPIRES", "LAST USED"),
            [
                (
                    row.get("name"),
                    _words(row.get("models") or ["all"]),
                    _time(row.get("created_at")),
                    _time(row.get("expires_at")) if row.get("expires_at") else "never",
                    _time(row.get("last_used_at")),
                )
                for row in rows
            ],
        )
