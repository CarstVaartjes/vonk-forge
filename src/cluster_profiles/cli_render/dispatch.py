"""Terminal presentation for dispatch responses."""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping

from ..cli_platform_render import render_platform
from ..cli_states_generated import SUCCEEDED
from ..control_client import ControlMalformedResponse
from ..generated_control.models.platform_observation import PlatformObservation
from .activity import _activity, _locks
from .administration import _catalog_sync, _enrollment, _error
from .artifact_jobs import _artifact_job
from .common import _field, _object, _records, _table, _text, _time, _words
from .fleet import _fleet_overview, _node
from .library import _library
from .operations import _application, _job, _operation, _run_result
from .profile import _gateway_keys, _profile, _profile_endpoints
from .review import _cache_removal_review, _preview


def render_payload(
    payload: Mapping[str, object],
    noun: str,
    *,
    action: str | None = None,
    wide: bool = False,
    technical: bool = False,
    activity_filters: Mapping[str, object] | None = None,
    artifact_job_action: str | None = None,
) -> None:
    """Render readable sections without discarding accepted operation identity."""
    try:
        if "observation" in payload:
            observation = _object(payload["observation"], "observation")
            _field("Observation", observation.get("status"))
            _field("Last confirmed", _time(observation.get("observed_at")))
            _field("Age in seconds", observation.get("age_seconds"))
            _field("Reconnect", observation.get("reconnect_command"))
            render_payload(
                _object(payload.get("result"), "observed result"),
                noun,
                action=action,
                wide=wide,
                technical=technical,
                activity_filters=activity_filters,
                artifact_job_action=artifact_job_action,
            )
            return
        if "delivery" in payload or (noun == "fleet" and action == "enrollment"):
            _enrollment(payload)
        elif "error" in payload:
            _error(payload)
        elif action == "connection":
            _field("Connected", payload.get("connected"))
            _field("Controller", payload.get("origin"))
            _field("Authorized read", payload.get("authorized_read"))
            client = _object(payload.get("client"), "client")
            _field("CLI version", client.get("version"))
        elif noun == "platform":
            render_platform(PlatformObservation.from_dict(payload))
        elif noun == "fleet":
            if action is None:
                _fleet_overview(payload, wide=wide)
            elif action == "rename":
                print(
                    f"Renamed {_text(payload.get('id'))} to {_text(payload.get('display_name'))}."
                )
            elif action == "detail":
                _node(payload, detail=True)
            elif action == "progress":
                _job(payload)
            elif action == "activity":
                _activity(payload, activity_filters)
            elif action == "locks":
                _locks(payload)
            elif action == "evidence":
                _field("Operation", payload.get("operation_id"))
                _field("Attempt", payload.get("attempt"))
                _field("File", payload.get("output"))
            elif action == "loginfo":
                _field("Spark", payload.get("node_id"))
                entries = _records(payload, "entries")
                if not entries:
                    print("No retained log entries match this request.")
                for entry in entries:
                    print(
                        f"{_time(entry.get('observed_at'))} {_text(entry.get('level'))} {_text(entry.get('source'))}: {_text(entry.get('message'))}"
                    )
                _field("Retained evidence", payload.get("retained"))
            elif (
                action == "upgrade"
                and payload.get("action") == "upgrade"
                and not payload.get("targets")
                and payload.get("state") == SUCCEEDED
            ):
                print("All Sparks already run the current agent; nothing to upgrade.")
            elif action == "resume":
                job_id = payload.get("id")
                print(
                    f"Resumed job {_text(job_id)}; it is now {_text(payload.get('state'))}."
                )
                _field(
                    "Next",
                    f"vonkctl fleet progress {shlex.quote(str(job_id))} --follow",
                )
            elif action in {"remove", "upgrade"}:
                _field("Action", payload.get("action"))
                _field("State", payload.get("state"))
                _field("Spark", payload.get("node_id"))
                _field("Targets", _words(payload.get("targets")))
                if payload.get("detail") is not None:
                    _field("Detail", payload["detail"])
                if payload.get("operation_id") is not None:
                    _field("Job", payload["operation_id"])
                    _field(
                        "Next",
                        f"vonkctl fleet progress {shlex.quote(str(payload['operation_id']))} --follow",
                    )
            else:
                _field("Observation", "unavailable")
        elif noun == "recipe" and artifact_job_action is not None:
            _artifact_job(payload, artifact_job_action)
        elif noun == "recipe" and action == "sync-status":
            _catalog_sync(payload)
        elif noun in {"model", "recipe"}:
            if action == "preview":
                _cache_removal_review(payload)
            elif action in {None, "library", "detail"}:
                _library(payload, noun, detail=action == "detail", wide=wide)
            else:
                _operation(payload, noun)
        elif noun == "key":
            _gateway_keys(payload, action)
        elif noun == "run":
            _run_result(payload)
        elif noun == "profile":
            if action == "endpoint":
                _profile_endpoints(payload)
            elif action == "list":
                rows = _records(payload, "profiles")
                if not rows:
                    print("No profiles saved.")
                _table(
                    ("PROFILE", "NAME", "STATE", "REVISION"),
                    [
                        (
                            row.get("number"),
                            row.get("name"),
                            row.get("status"),
                            row.get("revision"),
                        )
                        for row in rows
                    ],
                )
                for row in rows:
                    if row.get("projection_issue") is not None:
                        issue = _object(
                            row["projection_issue"], "profile projection issue"
                        )
                        _field(
                            f"Profile {row.get('number')} cause", issue.get("detail")
                        )
                        _field("Next", issue.get("next_action"))
            elif action == "preview":
                _preview(payload)
            elif action in {"progress", "load", "cancel"}:
                _application(payload)
            elif action == "export":
                _field("Profile", payload.get("profile"))
                _field("Revision", payload.get("revision"))
                _field("File", payload.get("output"))
            else:
                _profile(payload)
        else:
            _field("Observation", "unavailable")
        if technical and "document" in payload:
            print("Canonical definition:")
            print(
                json.dumps(
                    payload["document"], sort_keys=True, indent=2, ensure_ascii=True
                )
            )
    except (ControlMalformedResponse, KeyError, TypeError, ValueError):
        _field("Observation", "unavailable")
        for field in ("request_key", "request_id", "operation_id", "id", "job_id"):
            if payload.get(field) is not None:
                _field(field, payload[field])
