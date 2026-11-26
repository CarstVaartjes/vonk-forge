"""Terminal presentation for dispatch responses."""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping

from cluster_profiles.cli_json_contract import Word
from cluster_profiles.cli_platform_render import render_platform
from cluster_profiles.cli_render.activity import _activity, _locks
from cluster_profiles.cli_render.administration import (
    _catalog_sync,
    _enrollment,
    _error,
)
from cluster_profiles.cli_render.artifact_jobs import (
    _artifact_job,
    artifact_job_details,
    prints_record,
)
from cluster_profiles.cli_render.common import (
    _field,
    _object,
    _records,
    _table,
    _text,
    _time,
    _words,
)
from cluster_profiles.cli_render.fleet import _fleet_overview, _node
from cluster_profiles.cli_render.gateway import _gateway_documents
from cluster_profiles.cli_render.library import (
    LibraryNoun,
    _library,
    recipe_cut_note,
    top_recipes,
)
from cluster_profiles.cli_render.metrics import _metric_list, _metric_series
from cluster_profiles.cli_render.operations import (
    _application,
    _job,
    _operation,
    _run_result,
)
from cluster_profiles.cli_render.profile import (
    _endpoints_view,
    _gateway_keys,
    _profile,
    _profile_endpoints,
)
from cluster_profiles.cli_render.removal_review import (
    _cache_removal_review,
    _unused_removal,
)
from cluster_profiles.cli_render.review import _preview
from cluster_profiles.cli_states_generated import ENDPOINT_UNAVAILABLE, SUCCEEDED
from cluster_profiles.control_client.errors import ControlMalformedResponse
from cluster_profiles.generated_control.models.artifact_job_response import (
    ArtifactJobResponse,
)
from cluster_profiles.generated_control.models.metric_spec import MetricSpec
from cluster_profiles.generated_control.models.metrics_series_response import (
    MetricsSeriesResponse,
)
from cluster_profiles.generated_control.models.platform_observation import (
    PlatformObservation,
)


def render_payload(
    payload: Mapping[str, object],
    noun: str,
    *,
    action: str | None = None,
    wide: bool = False,
    show_all: bool = False,
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
                show_all=show_all,
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
        elif noun == "metrics":
            if action == "show":
                _metric_series(
                    MetricsSeriesResponse.from_dict(payload),
                    {
                        str(key): str(value)
                        for key, value in _object(
                            payload.get("names", {}), "Spark names"
                        ).items()
                    },
                )
            else:
                _metric_list(
                    [MetricSpec.from_dict(row) for row in _records(payload, "metrics")]
                )
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
                    f"Resumed operation {_text(job_id)}; it is now {_text(payload.get('state'))}."
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
                    _field("Operation", payload["operation_id"])
                    _field(
                        "Next",
                        f"vonkctl fleet progress {shlex.quote(str(payload['operation_id']))} --follow",
                    )
            else:
                _field("Observation", ENDPOINT_UNAVAILABLE)
        elif noun == Word.JOB and artifact_job_action is not None:
            _artifact_job(payload, artifact_job_action)
            if wide and prints_record(artifact_job_action, payload.keys()):
                artifact_job_details(ArtifactJobResponse.from_dict(dict(payload)))
        elif noun == "recipe" and action == "sync-status":
            _catalog_sync(payload)
        elif noun in {"model", "recipe"}:
            if payload.get("unused_removal"):
                _unused_removal(payload)
            elif action == "preview":
                _cache_removal_review(payload, verbose=wide)
            elif action in {None, "library", "detail"}:
                rows = (
                    _records(payload, "recipes") if noun == LibraryNoun.RECIPE else []
                )
                if action is None and rows and not (wide or show_all):
                    top = top_recipes(rows)
                    _library(
                        {**payload, "recipes": top}, noun, detail=False, wide=False
                    )
                    recipe_cut_note(len(rows), len(top))
                else:
                    _library(payload, noun, detail=action == "detail", wide=wide)
            else:
                _operation(payload, noun)
        elif noun == "gateway":
            _gateway_documents(payload)
        elif noun == "key":
            _gateway_keys(payload, action)
        elif noun == "run":
            _run_result(payload)
        elif noun == "profile":
            if action == "endpoint":
                _profile_endpoints(_endpoints_view(payload), wide=wide)
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
            elif action == "delete":
                _field("Profile", payload.get("number"))
                _field("Deleted", payload.get("outcome"))
            elif action == "export":
                _field("Profile", payload.get("profile"))
                _field("Revision", payload.get("revision"))
                _field("File", payload.get("output"))
            else:
                _profile(payload)
        else:
            _field("Observation", ENDPOINT_UNAVAILABLE)
        if technical and "document" in payload:
            print("Canonical definition:")
            print(
                json.dumps(
                    payload["document"], sort_keys=True, indent=2, ensure_ascii=True
                )
            )
    except ControlMalformedResponse, KeyError, TypeError, ValueError:
        _field("Observation", ENDPOINT_UNAVAILABLE)
        for field in ("request_key", "request_id", "operation_id", "id", "job_id"):
            if payload.get(field) is not None:
                _field(field, payload[field])
