"""Damaged retained observations stay local and reconnect to the same intent."""

from copy import deepcopy

import pytest
from sqlalchemy import select
from vonk_control.auth import TokenCodec
from vonk_control.fleet_profile_adapter_conversion import try_convert_application
from vonk_control.fleet_profile_contract import FleetProfileInput
from vonk_control.fleet_profiles import FleetProfileService, _persisted_profile_progress
from vonk_control.models import AgentOperation, FleetProfileApplication, Job
from vonk_control.operation_api import durable_operation_services

from .test_fleet_profile_api import _client, _headers
from .test_fleet_profiles import NOW
from .test_profile_adapter_conversion import _persist_retained, _retained_stop


@pytest.mark.usefixtures("damaged_json_rows")
def test_damaged_retained_metadata_keeps_unrelated_reads_and_repairs_same_intent(
    tmp_path,
):
    sessions, _lifecycle, _switches, accepted, _run, stop_id, original, _claims = (
        _retained_stop(tmp_path)
    )
    profiles = FleetProfileService(sessions, clock=lambda: NOW)
    readable = profiles.create(
        FleetProfileInput(name="Readable unrelated draft"), actor="admin"
    )
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, accepted.id)
        assert row is not None
        request_key, plan_digest = row.request_key, row.plan_digest
        damaged = deepcopy(original)
        damaged["total_steps"] = "unreadable count"
        _persist_retained(session, row, damaged)
        child_id = session.scalar(
            select(AgentOperation.id).where(AgentOperation.kind == "recipe.install")
        )
        assert child_id is not None

    operations = durable_operation_services(
        sessions,
        tmp_path / "routes",
        clock=lambda: NOW,
        cursors=TokenCodec(b"k" * 32).cursor_codec(),
        operation_providers=(profiles.operation_provider(),),
    )
    client, codec = _client(sessions, profiles=profiles, operations=operations)
    headers = _headers(codec, "operator")
    page = client.get("/api/operations", headers=headers)
    assert page.status_code == 200
    items = {item["id"]: item for item in page.json()["operations"]}
    assert items[child_id]["kind"] == "recipe.install"
    assert items[child_id]["state"] != "unavailable"
    assert items[accepted.id]["status_reason"]
    detail = client.get(f"/api/operations/{child_id}", headers=headers)
    assert detail.status_code == 200 and detail.json()["id"] == child_id
    listing = client.get("/api/profile", headers=headers)
    assert listing.status_code == 200
    assert any(item["id"] == readable.id for item in listing.json()["profiles"])
    projected = client.get(f"/api/profile/applications/{accepted.id}", headers=headers)
    assert projected.status_code == 200
    assert projected.json()["id"] == accepted.id
    assert projected.json()["projection_issue"] is not None
    observed = profiles.application(accepted.id)
    assert observed.id == accepted.id and observed.request_key == request_key
    assert observed.projection_issue is not None
    assert observed.plan_digest == plan_digest
    with sessions() as session:
        row = session.get(FleetProfileApplication, accepted.id)
        assert row is not None and row.progress == damaged
        child = session.get(Job, stop_id)
        assert child is not None
        child_request = child.request_id

    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, accepted.id)
        assert row is not None
        _persist_retained(session, row, original)
        assert try_convert_application(session, row, NOW).state == "converted"
        progress = _persisted_profile_progress(row)
        assert progress.switch_adapter is not None
        assert [
            pending.operation_id for pending in progress.switch_adapter.pending_children
        ] == [stop_id]
    repaired = profiles.application(accepted.id)
    assert repaired.id == accepted.id and repaired.request_key == request_key
    assert repaired.plan_digest == plan_digest and repaired.projection_issue is None
    with sessions() as session:
        assert (
            session.scalar(select(Job.id).where(Job.request_id == child_request))
            == stop_id
        )
