"""Profile draft authoring and assignment changes."""

from __future__ import annotations

import argparse
import copy
import time
from collections.abc import Mapping
from typing import cast

from ..cli_files import read_json_document, write_private_document
from ..cli_select import SelectorError
from ..control_client import (
    ControlClientError,
    ControlMalformedResponse,
    validate_control_document,
)
from .common import ControllerClient, _profile_number
from .profile_options import _apply_option_choices, _configure_option_target
from .selection import (
    _resolve_recipe_selector,
    _resolve_spark_selectors,
    _selection_remaining,
)
from .submission import _known_http_refusal_status


def _spark_id_list(value: object) -> list[str]:
    """The definition's bounded canonical decode already validated this field."""
    return cast(list[str], value)


def _profile_save(
    args: argparse.Namespace,
    client: ControllerClient,
    definition: Mapping[str, object],
    *,
    current_revision: int,
) -> dict[str, object]:
    expected = (
        args.expected_revision
        if args.expected_revision is not None
        else current_revision
    )
    body = validate_control_document(
        "FleetProfileInput", {**definition, "expected_revision": expected}
    )
    from .observation import _bounded_timeout, _poll_path

    deadline = time.monotonic() + _bounded_timeout(args)
    try:
        result = client.request(
            "PUT",
            f"/api/profile/{_profile_number(args)}",
            body,
            timeout_seconds=_selection_remaining(deadline),
        )
    except (ControlClientError, OSError) as error:
        if isinstance(error, ControlClientError) and _known_http_refusal_status(
            error
        ) in {400, 401, 403, 409, 422}:
            # An intact owner answer is surfaced, including revision consent.
            raise

        def definition_view(observed: object) -> None:
            validate_control_document("FleetProfileDefinitionView", observed)

        # There is no keyed PUT receipt route. Preserve the exact accepted
        # snapshot for observation; never overwrite a newer edit to reconcile.
        return _poll_path(
            client,
            f"/api/profile/{_profile_number(args)}/definition",
            {},
            args,
            fetch_initial=True,
            validate=definition_view,
            terminal=lambda _: False,
            deadline=deadline,
            attempts=3,
        )
    args.profile_saved = True
    return result


def _profile_authoring(
    args: argparse.Namespace, client: ControllerClient
) -> dict[str, object]:
    action = args.profile_action
    selection_deadline = (
        time.monotonic() + args.timeout_seconds if action in {"add", "remove"} else None
    )
    if action == "import":
        definition = validate_control_document(
            "FleetProfileDefinition", read_json_document(args.file)
        )
        return _profile_save(
            args, client, definition, current_revision=args.expected_revision
        )
    from .observation import _bounded_timeout, _poll_path

    deadline = selection_deadline or (time.monotonic() + _bounded_timeout(args))

    def readable_definition(observed: object) -> None:
        try:
            document = validate_control_document("FleetProfileDefinitionView", observed)
            definition = validate_control_document(
                "FleetProfileDefinition", document["definition"]
            )
            if action == "configure" and args.option:
                _configure_option_target(
                    cast(list, definition["assignments"]), args.assignment
                )
        except (ControlClientError, KeyError, TypeError, ValueError):
            raise ControlMalformedResponse(
                "saved profile definition is unavailable"
            ) from None

    current = _poll_path(
        client,
        f"/api/profile/{_profile_number(args)}/definition",
        {},
        args,
        fetch_initial=True,
        terminal=lambda _: True,
        validate=readable_definition,
        deadline=deadline,
    )
    if args.observation.status != "complete":
        return current
    current_revision = cast(int, current["revision"])
    definition = copy.deepcopy(
        validate_control_document("FleetProfileDefinition", current["definition"])
    )
    if action == "export":
        if args.output is not None:
            write_private_document(args.output, definition)
            return {
                "profile": _profile_number(args),
                "revision": current_revision,
                "output": str(args.output),
            }
        args.document_output = True
        return definition
    assignments = cast(list[dict[str, object]], definition.get("assignments", []))
    if action == "configure":
        if (
            not any(
                value is not None
                for value in (
                    args.name,
                    args.description,
                    args.retention,
                    args.favorite,
                )
            )
            and not args.label
            and not args.remove_label
            and not args.option
        ):
            raise ValueError("profile configure requires at least one change")
        if args.option:
            target = _configure_option_target(assignments, args.assignment)
            _apply_option_choices(client, target, args)
        if args.name is not None:
            definition["name"] = args.name
        if args.description is not None:
            definition["description"] = args.description
        if args.retention is not None:
            definition["installation_policy"] = args.retention
        if args.favorite is not None:
            definition["favorite"] = args.favorite == "true"
        labels = dict(cast(dict[str, str], definition.get("labels", {})))
        edits: dict[str, str] = {}
        for edit in args.label:
            key, separator, value = edit.partition("=")
            if not separator or not key or key in edits or key in args.remove_label:
                raise ValueError(
                    "label edits require unique KEY=VALUE values without conflicting removals"
                )
            edits[key] = value
        if len(set(args.remove_label)) != len(args.remove_label):
            raise ValueError("label removals must be unique")
        for key in args.remove_label:
            labels.pop(key, None)
        labels.update(edits)
        definition["labels"] = labels
    elif action == "add":
        recipe_selector = _resolve_recipe_selector(
            client, args.recipe_selector, deadline=selection_deadline
        )
        spark_ids = _resolve_spark_selectors(
            client, list(args.spark), deadline=selection_deadline
        )
        matching = [
            assignment
            for assignment in assignments
            if assignment["recipe_selector"] == recipe_selector
            and assignment.get("assignment_name") == args.assignment_name
        ]
        if len(matching) > 1:
            raise SelectorError("ambiguous assignment; select a unique assignment name")
        if matching:
            assignment = matching[0]
            assignment["spark_ids"] = sorted(
                set(_spark_id_list(assignment["spark_ids"])) | set(spark_ids)
            )
        else:
            assignment = {"recipe_selector": recipe_selector, "spark_ids": spark_ids}
            if args.assignment_name is not None:
                assignment["assignment_name"] = args.assignment_name
            assignments.append(assignment)
        _apply_option_choices(client, assignment, args)
        if args.model_variant is not None:
            assignment["model_variant"] = args.model_variant
        if args.desired_state is not None:
            assignment["desired_state"] = args.desired_state
        definition["assignments"] = assignments
    elif action == "remove":
        target = args.assignment.casefold()
        matching = [
            assignment
            for assignment in assignments
            if str(assignment.get("assignment_name", "")).casefold() == target
        ]
        if not matching:
            recipe = _resolve_recipe_selector(
                client, args.assignment, deadline=selection_deadline
            )
            matching = [
                assignment
                for assignment in assignments
                if assignment["recipe_selector"] == recipe
            ]
        spark_ids = _resolve_spark_selectors(
            client, list(args.spark), deadline=selection_deadline
        )
        if spark_ids:
            matching = [
                assignment
                for assignment in matching
                if set(spark_ids) <= set(_spark_id_list(assignment["spark_ids"]))
            ]
        if not matching:
            # The removal is already satisfied in this complete definition;
            # the owner still checks the requested revision on the save.
            return _profile_save(
                args, client, definition, current_revision=current_revision
            )
        if len(matching) != 1:
            raise SelectorError("assignment selector is ambiguous")
        removed = matching[0]
        remaining = (
            [
                spark
                for spark in _spark_id_list(removed["spark_ids"])
                if spark not in spark_ids
            ]
            if spark_ids
            else []
        )
        if remaining:
            removed["spark_ids"] = remaining
        else:
            assignments.remove(removed)
        definition["assignments"] = assignments
    if selection_deadline is not None:
        _selection_remaining(selection_deadline)
    return _profile_save(args, client, definition, current_revision=current_revision)
