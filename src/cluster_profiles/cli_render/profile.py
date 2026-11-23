"""Terminal presentation for profile responses."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum

from cluster_profiles.cli_render.common import (
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
    _waiting,
    _words,
    terminal_text,
)
from cluster_profiles.cli_render.library import _option_choices
from cluster_profiles.cli_states_generated import (
    ENDPOINT_EXPIRED,
    ENDPOINT_INSTALLED_ONLY,
    ENDPOINT_NOT_PUBLISHED_YET,
    ENDPOINT_UNAVAILABLE,
    ENDPOINT_WITHDRAWN,
    PUBLISHED,
)
from cluster_profiles.control_client.errors import ControlMalformedResponse
from cluster_profiles.generated_control.models.endpoint_response import EndpointResponse
from cluster_profiles.generated_control.models.fleet_profile_endpoints_view import (
    FleetProfileEndpointsView,
)
from cluster_profiles.generated_control.types import Unset


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
        _field("Sparks", _words(_spark_labels(nodes, payload.get("spark_names"))))
        _field("Desired", assignment.get("desired_state"))
        _option_choices(assignment.get("option_choices"))
        _field("Observed", match.get("observed_state") if match is not None else None)
        update = match.get("recipe_update") if match is not None else None
        if isinstance(update, Mapping):
            _field("Update", update.get("detail"))
    wait = _optional(payload.get("maintenance_wait"), "maintenance_wait")
    if wait:
        _field("Recovery reason", wait.get("reason"))
        _waiting(
            {
                "blockers": wait.get("blockers"),
                "next_attempt_at": wait.get("next_check"),
            }
        )
        _field("Dependency deadline", _time(wait.get("dependency_deadline")))
        _field("Resumes when", wait.get("resume_condition"))
    _reasons(payload.get("warnings"))
    _actions(payload.get("next_actions"))


def _shell_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def connection_snippets(api_base: str, alias: str) -> dict[str, str]:
    """The copyable client examples."""
    body = json.dumps(
        {"model": alias, "messages": [{"role": "user", "content": "Hello"}]},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return {
        "setup": "\n".join(
            (
                "vonkctl key create NAME --output CLIENT_KEY_FILE",
                'export OPENAI_API_KEY="$(cat CLIENT_KEY_FILE)"',
                "export OPENAI_BASE_URL=" + _shell_quote(api_base.rstrip("/")),
            )
        ),
        "curl": "\n".join(
            (
                'curl "$OPENAI_BASE_URL/chat/completions" \\',
                '  -H "Authorization: Bearer $OPENAI_API_KEY" \\',
                '  -H "Content-Type: application/json" \\',
                "  -d " + _shell_quote(body),
            )
        ),
        "python": "\n".join(
            (
                "from openai import OpenAI",
                "",
                "client = OpenAI()",
                "response = client.chat.completions.create(",
                "    model=" + json.dumps(alias, ensure_ascii=False) + ",",
                '    messages=[{"role": "user", "content": "Hello"}],',
                ")",
                "print(response.choices[0].message.content)",
            )
        ),
    }


CONNECTION_NOTE = (
    "Thinking and sampling are usually set per request "
    "(for example chat_template_kwargs and temperature), not in the recipe."
)


def _connection_examples(api_base: str, alias: str, *, wide: bool = False) -> None:
    """Print the examples; the key is only ever read from the key file."""
    snippets = connection_snippets(terminal_text(api_base), terminal_text(alias))
    shown = [("Client setup", "setup"), ("curl example", "curl")]
    if wide:
        shown.append(("Python example (openai client)", "python"))
    for title, name in shown:
        print(title + ":")
        for line in snippets[name].split("\n"):
            print(("  " + line) if line else "")
    if wide:
        print(CONNECTION_NOTE)


def _serving_now(assignments: Sequence[Sequence[object]]) -> None:
    """One line per published alias, exactly as the dashboard's "Serving now" block shows them.

    Each row is (alias, recipe title, state).
    """
    published = [row for row in assignments if row[2] == PUBLISHED and row[0]]
    if not published:
        return
    print("Serving now")
    for alias, title, state in published:
        print(f"{_text(alias)} · {_text(title)} · {_text(state)}")


def _endpoints_view(payload: object) -> FleetProfileEndpointsView:
    """Validate the endpoint answer once, at the edge of rendering."""
    try:
        return FleetProfileEndpointsView.from_dict(dict(_object(payload, "endpoints")))
    except (KeyError, TypeError, ValueError) as error:
        raise ControlMalformedResponse(
            "endpoints observation is unavailable"
        ) from error


def _set(value: object) -> object:
    """An optional contract field as a plain value: absent and null both read None."""
    return None if isinstance(value, Unset) else value


def _profile_endpoints(view: FleetProfileEndpointsView, *, wide: bool = False) -> None:
    assignments = view.assignments or []
    listing = endpoint_listing(
        _set(view.application_id), _set(view.projection_issue), view.assignments
    )
    if listing is EndpointListing.NOT_LOADED:
        print(f"Profile {view.number} is not loaded.")
        print("Saved profile edits do not publish routes until the profile is loaded.")
        return
    _field("Load", _set(view.application_id))
    _field("Load state", _set(view.application_state))
    if listing is EndpointListing.HISTORY_INVALID:
        print(
            "Endpoint assignments are unavailable because stored load history is invalid."
        )
        issue = view.projection_issue
        _field(
            "Stored evidence", None if isinstance(issue, Unset | None) else issue.detail
        )
        return
    if listing is EndpointListing.EMPTY:
        print("The loaded profile has no endpoint assignments.")
        print(
            "Job recipes have no endpoint; run one with vonkctl job run ALIAS (see vonkctl job --help)"
        )
        return
    _serving_now(
        [(_set(item.alias), item.recipe_title, item.state) for item in assignments]
    )
    for assignment in assignments:
        print()
        _field("Assignment", assignment.recipe_title)
        _field("Endpoint state", assignment.state)
        alias = _set(assignment.alias)
        endpoint = assignment.endpoint
        if assignment.state == PUBLISHED and isinstance(endpoint, EndpointResponse):
            _field("Client model identifier", alias)
            _field("API base (inference gateway)", endpoint.api_base)
            if wide:
                _field("Route generation", endpoint.generation)
                _field("Route observed at", _time(endpoint.observed_at))
                _field("Freshness", _freshness(endpoint.observed_at, view.observed_at))
                _field("Spark backend (diagnostic)", endpoint.backend_api_base)
            if isinstance(alias, str):
                _connection_examples(endpoint.api_base, alias, wide=wide)
            continue
        if alias is not None:
            _field("Client model identifier", alias)
        messages = {
            ENDPOINT_INSTALLED_ONLY: "Install-only assignment; no published endpoint was requested.",
            ENDPOINT_NOT_PUBLISHED_YET: "The current assignment route is not published yet.",
            ENDPOINT_EXPIRED: "The published route lease has expired.",
            ENDPOINT_WITHDRAWN: "No current published route is associated with this assignment.",
            ENDPOINT_UNAVAILABLE: "The Controller cannot verify a current route for this assignment.",
        }
        print(messages.get(assignment.state, "Endpoint state is unavailable."))


def _spark_labels(nodes: object, names: object) -> object:
    """Spark names for ids the Controller named; an id stands in for the rest."""
    if not isinstance(nodes, list) or not isinstance(names, Mapping):
        return nodes
    return [names.get(node, node) for node in nodes]


def _gateway_keys(payload: Mapping[str, object], action: object) -> None:
    if action in ("create", "roll"):
        _field("Key", payload.get("name"))
        _field("Models", _words(payload.get("models") or ["all"]))
        _field("Expires", _key_expiry(payload.get("expires_at")))
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
                    _key_expiry(row.get("expires_at")),
                    _time(row.get("last_used_at")),
                )
                for row in rows
            ],
        )


def _key_expired(value: object, now: datetime) -> bool:
    """A key is expired when its expiry is a real time at or before ``now``."""
    try:
        return datetime.fromisoformat(str(value)) <= now
    except ValueError:
        return False


def _key_expiry(value: object) -> str:
    if not value:
        return "never"
    shown = _time(value)
    return f"expired {shown}" if _key_expired(value, datetime.now(UTC)) else shown


class EndpointListing(StrEnum):
    NOT_LOADED = "not-loaded"
    HISTORY_INVALID = "history-invalid"
    EMPTY = "empty"
    ASSIGNED = "assigned"


def endpoint_listing(
    application_id: object, projection_issue: object, assignments: object
) -> EndpointListing:
    """What a profile's endpoint answer can say; invalid history never reads as empty."""
    if application_id is None:
        return EndpointListing.NOT_LOADED
    if projection_issue:
        return EndpointListing.HISTORY_INVALID
    if assignments == []:
        return EndpointListing.EMPTY
    return EndpointListing.ASSIGNED
