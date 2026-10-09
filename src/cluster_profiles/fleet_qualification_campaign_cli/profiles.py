"""Profiles for fleet qualification campaign cli."""

from __future__ import annotations

import argparse
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..cli_states import SUCCEEDED
from ..control_client import (
    ControlClientError,
    ControlForbidden,
    ControlMalformedResponse,
    ControlUnauthorized,
)
from ..controller_cli.profile_load import _submit_profile_load
from ..fleet_qualification import (
    QualificationError,
    QualificationObservationUnknown,
    _BudgetClient,
    _durable_deadline,
    _durable_key,
    _forget_key,
    _observe,
)
from .contracts import _TERMINAL_APPLICATIONS, PROFILE_LABEL, Campaign, Lane, Row


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _alias(campaign: Campaign, row: Row) -> tuple[str, str]:
    service = campaign.fixtures.service_recipes.get(row.key)
    if service is not None:
        return service.alias, "openai-service"
    if row.key in campaign.fixtures.recipes:
        return f"q{row.sequence}", "artifact-job"
    raise QualificationObservationUnknown(f"{row.key} fixture is not yet observed")


def _profile(client: Any, number: int, authority_id: str) -> dict[str, Any]:
    profile = client.request("GET", f"/api/profile/{number}")
    if profile.get("status") is None:
        raise QualificationObservationUnknown(
            "qualification profile projection is not yet observed"
        )
    return profile


def _save_profile(
    client: Any,
    number: int,
    authority_id: str,
    assignments: Sequence[Mapping[str, object]],
) -> None:
    profile = _profile(client, number, authority_id)
    labels = profile.get("labels") or {}
    # Consent covers an existing campaign draft or creation of an absent one.
    # The Controller revision fence decides whether that exact effect is valid;
    # an unrelated saved profile cannot silently become an overwrite target.
    expected_revision = (
        profile.get("revision") if labels.get(PROFILE_LABEL) == authority_id else 0
    )
    client.request(
        "PUT",
        f"/api/profile/{number}",
        {
            "name": f"Qualification {authority_id}"[:120],
            "description": f"Recipe qualification campaign {authority_id}",
            "installation_policy": "keep-cached",
            "labels": {PROFILE_LABEL: authority_id},
            "favorite": False,
            "expected_revision": expected_revision,
            "assignments": list(assignments),
        },
    )


def _apply_profile(
    client: Any,
    number: int,
    campaign: Campaign,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
    *,
    request_directory: Path | None = None,
    request_scope: str = "",
) -> str:
    deadline = clock() + campaign.timeout_seconds
    bounded = _BudgetClient(client, deadline, clock)
    scope = f"{campaign.authority_id}/{number}/{request_scope}"
    key = _observe(
        lambda: _durable_key(request_directory, scope),
        deadline,
        clock,
        sleeper,
        campaign.poll_seconds,
    )
    try:
        args = argparse.Namespace(global_json=True, json=True, request_key=key)
        deadline = _observe(
            lambda: _durable_deadline(
                request_directory, scope, key, campaign.timeout_seconds, clock
            ),
            deadline,
            clock,
            sleeper,
            campaign.poll_seconds,
        )
        expired = deadline <= clock()
        if expired:
            deadline = clock() + min(30, campaign.timeout_seconds)
        bounded = _BudgetClient(client, deadline, clock)

        def acceptance():
            if not expired:
                return _submit_profile_load(bounded, number, args, lambda: key)
            value = bounded.request(
                "GET", f"/api/profile/{number}/requests/{_quote(key)}"
            )
            if value.get("request_key") != key or not isinstance(value.get("id"), str):
                raise ControlMalformedResponse(
                    "profile acceptance projection is not yet observed"
                )
            return value

        accepted = _observe(acceptance, deadline, clock, sleeper, campaign.poll_seconds)
        application_id = accepted.get("id")
        if not isinstance(application_id, str):
            raise QualificationError("profile acceptance identity was not observed")

        def application():
            value = bounded.request(
                "GET", f"/api/profile/applications/{_quote(application_id)}"
            )
            if value.get("id") != application_id:
                raise ControlMalformedResponse(
                    "qualification application projection identifies another application"
                )
            return value

        while clock() < deadline:
            observed = _observe(
                application, deadline, clock, sleeper, campaign.poll_seconds
            )
            if observed.get("state") in _TERMINAL_APPLICATIONS:
                if observed.get("state") != SUCCEEDED:
                    raise QualificationError(
                        f"profile application {application_id} ended without measured success"
                    )
                return key
            sleeper(min(campaign.poll_seconds, max(0, deadline - clock())))
        raise QualificationError(
            f"profile application {application_id} observation deadline elapsed"
        )

    except (ControlUnauthorized, ControlForbidden):
        _forget_key(request_directory, scope, key)
        raise
    except (ControlClientError, QualificationError, OSError):
        _forget_key(request_directory, scope, key)
        raise


def _fleet_nodes(client: Any) -> dict[str, Mapping[str, Any]]:
    fleet = client.request("GET", "/api/fleet")
    return {str(node["id"]): node for node in fleet.get("nodes", [])}


def _serving_run(nodes: Mapping[str, Mapping[str, Any]], lane: Lane) -> str | None:
    """The run ID once every rank of the lane is healthy and published."""

    presences = [
        presence
        for node_id in lane.node_ids
        for presence in (nodes.get(node_id) or {}).get("loaded", [])
        if presence.get("alias") == lane.alias
    ]
    run_ids = {presence.get("run_id") for presence in presences}
    if (
        len(presences) != lane.row.node_count
        or len(run_ids) != 1
        or any(
            presence.get("healthy") is not True
            or presence.get("route_state") != "published"
            or presence.get("run_state") != "running"
            for presence in presences
        )
    ):
        return None
    run_id = run_ids.pop()
    return run_id if isinstance(run_id, str) else None


def _wait_serving(
    client: Any,
    lane: Lane,
    campaign: Campaign,
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> str:
    deadline = clock() + campaign.timeout_seconds
    bounded = _BudgetClient(client, deadline, clock)
    while clock() < deadline:
        nodes = _observe(
            lambda: _fleet_nodes(bounded),
            deadline,
            clock,
            sleeper,
            campaign.poll_seconds,
        )
        run_id = _serving_run(nodes, lane)
        if run_id is not None:
            return run_id
        sleeper(min(campaign.poll_seconds, max(0, deadline - clock())))
    raise QualificationError(f"{lane.row.key} serving observation deadline elapsed")
