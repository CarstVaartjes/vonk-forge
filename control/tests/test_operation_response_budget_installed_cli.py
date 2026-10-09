"""Actual typed observation producers must fit the installed reader allocation."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import (
    OperationMemberProgress,
    OperationProgress,
    canonical_message,
)
from vonk_control.api import create_app
from vonk_control.auth import Actor, TokenCodec
from vonk_control.jobs import JobService
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Base,
    Job,
)
from vonk_control.operation_api import (
    OperationDetailResponse,
    OperationListPage,
    OperationsResponse,
    _response_bytes,
    durable_operation_services,
    operation_detail_response,
)
from vonk_control.operation_item_contract import operation_item
from vonk_control.operation_progress import aggregate_progress

from cluster_profiles.control_limits import MAX_CONTROL_DOCUMENT_BYTES

from .test_profile_load_installed_cli import _https_api_peer, _process_environment

pytest_plugins = ["tests.test_profile_load_installed_cli"]
NODE = "spk_" + "1" * 32


def _members(count: int) -> list[OperationMemberProgress]:
    return [
        OperationMemberProgress(
            member_id="🧱" * 120 + f"{index:04d}",
            phase="🧱" * 80,
            kind="🧱" * 80,
            state="🧱" * 32,
        )
        for index in range(count)
    ]


@pytest.mark.lane
def test_installed_activity_byte_continuation_and_aggregate_fact_recovery(
    installed_vonkctl: Path,
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'operation-bytes.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    progress = OperationProgress(phase="copying", members=_members(64))
    # Every individual source report fits the existing ingress/reader budget.
    assert len(canonical_message(progress)) < MAX_CONTROL_DOCUMENT_BYTES
    with sessions.begin() as session:
        session.add(AgentNode(node_id=NODE, state="active"))
        job = Job(
            request_id="22222222-2222-4222-8222-222222222222",
            kind="reconcile",
            state="running",
            actor="operator",
            authority_revision="a" * 40,
            targets=[NODE],
            payload_digest="b" * 64,
            payload={},
            current_attempt=1,
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        session.flush()
        for index in range(20):
            identifier = f"00000000-0000-4000-8000-{index:012d}"
            session.add(
                AgentOperation(
                    id=identifier,
                    parent_job_id=job.id,
                    node_id=NODE,
                    kind="node.probe",
                    payload_digest="c" * 64,
                    payload={},
                    authority_revision="a" * 40,
                    state="running",
                    current_attempt=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                AgentOperationAttempt(
                    operation_id=identifier,
                    attempt=1,
                    fence=f"11111111-1111-4111-8111-{index:012d}",
                    lease_deadline=now + timedelta(seconds=60),
                    agent_certificate_serial="certificate",
                    state="running",
                    progress=progress.model_dump(mode="json"),
                )
            )
    codec = TokenCodec(b"operation-response-byte-proof-key")
    source = durable_operation_services(
        sessions,
        tmp_path / "routes",
        clock=lambda: now,
        cursors=codec.cursor_codec(),
    )
    assert source.list_operations is not None and source.get_operation is not None
    unbounded = source.list_operations(None, 20, None, None, None)
    full = OperationsResponse(
        operations=[operation_detail_response(row) for row in unbounded.items],
        next_cursor=unbounded.next_cursor,
        total=unbounded.total,
    )
    assert len(canonical_message(full)) > MAX_CONTROL_DOCUMENT_BYTES

    # The owning aggregation constructor can create an indivisible larger fact
    # from independently valid member reports. This does not claim a huge single
    # heartbeat was accepted through the ingress cap.
    aggregate = aggregate_progress(_members(1024))
    assert len(canonical_message(aggregate)) > MAX_CONTROL_DOCUMENT_BYTES
    affected = operation_item(unbounded.items[0]).id
    oversized = False
    indivisible_id: str | None = None
    native_get = source.get_operation
    native_list = source.list_operations

    def detail(identifier: str):
        item = operation_item(native_get(identifier))
        # Defensive canonical boundary: SQL timestamps are actual datetimes,
        # but the public optional string field itself has no length bound.
        if identifier == indivisible_id:
            return item.model_copy(
                update={"updated_at": "x" * (MAX_CONTROL_DOCUMENT_BYTES + 1)}
            )
        return (
            item.model_copy(update={"progress": aggregate})
            if oversized and identifier == affected
            else item
        )

    def page(cursor, limit, state, node_id, request_id):
        result = native_list(cursor, limit, state, node_id, request_id)
        return OperationListPage(
            items=tuple(detail(operation_item(row).id) for row in result.items),
            next_cursor=result.next_cursor,
            total=result.total,
        )

    services = replace(
        source, operation_providers=(), get_operation=detail, list_operations=page
    )
    headers = {
        "Authorization": "Bearer "
        + codec.issue(Actor("operator", "operator"), ttl_seconds=100, now=0)
    }
    app = create_app(
        jobs=JobService(sessions, clock=lambda: now),
        tokens=codec,
        operations=services,
        now=lambda: 1,
    )
    with (
        TestClient(app) as api,
        _https_api_peer(tmp_path, api, headers) as (url, certificate, _peer),
    ):
        environment = _process_environment(tmp_path, url, certificate, headers)
        base = [
            str(installed_vonkctl),
            "--no-input",
            "--json",
            "fleet",
            "activity",
            "--limit",
            "20",
            "--target",
            NODE,
            "--state",
            "running",
            "--request-id",
            "22222222-2222-4222-8222-222222222222",
        ]
        observed = []
        cursor = None
        while True:
            arguments = base + (["--cursor", cursor] if cursor else [])
            read = subprocess.run(
                arguments,
                env=environment,
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
            assert read.returncode == 0, read.stderr
            document = json.loads(read.stdout)
            assert document["total"] == 20
            assert document["operations"]
            observed.extend(row["id"] for row in document["operations"])
            cursor = document.get("next_cursor")
            if cursor is None:
                break
        assert observed == [operation_item(row).id for row in unbounded.items]
        assert len(observed) == len(set(observed)) == 20
        oversized = True
        response = api.get(f"/api/operations/{affected}", headers=headers)
        assert response.status_code == 200
        assert len(response.content) <= MAX_CONTROL_DOCUMENT_BYTES
        unknown = response.json()
        assert unknown["id"] == affected and unknown["kind"] == "node.probe"
        assert unknown["state"] == "running" and unknown["attempt"] == 1
        assert unknown["node_ids"] == [NODE] and unknown.get("progress") is None
        assert unknown["projection_issues"][0]["field"] == "progress"
        assert (
            unknown["projection_issues"][0]["observed_bytes"]
            > MAX_CONTROL_DOCUMENT_BYTES
        )
        human = subprocess.run(
            [argument for argument in base if argument != "--json"],
            env=environment,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        assert human.returncode == 0, human.stderr
        assert "progress unavailable" in human.stderr
        assert affected in human.stdout
        oversized = False
        repaired = api.get(f"/api/operations/{affected}", headers=headers)
        assert repaired.status_code == 200
        assert repaired.json()["progress"]["members"]
        assert repaired.json().get("projection_issues") is None
        assert _response_bytes(
            OperationDetailResponse.model_validate_json(repaired.content)
        ) == len(repaired.content)
        indivisible_id = operation_item(unbounded.items[1]).id
        first = api.get("/api/operations", headers=headers, params={"limit": 20})
        assert first.status_code == 200
        first_document = first.json()
        observed = [item["id"] for item in first_document["operations"]]
        cursor = first_document["next_cursor"]
        for _attempt in range(20):
            if cursor is None:
                break
            continuation = api.get(
                "/api/operations",
                headers=headers,
                params={"limit": 20, "cursor": cursor},
            )
            assert continuation.status_code == 200
            assert len(continuation.content) <= MAX_CONTROL_DOCUMENT_BYTES
            continued = continuation.json()
            observed.extend(item["id"] for item in continued["operations"])
            cursor = continued.get("next_cursor")
        assert cursor is None
        assert observed == [operation_item(row).id for row in unbounded.items]
        assert first_document["total"] == 20
        unknown_row = next(
            item
            for item in first_document["operations"]
            if item["id"] == indivisible_id
        )
        assert unknown_row["observation_unavailable"]
        assert unknown_row["node_ids"] == [NODE]
        assert unknown_row["state"] == operation_item(unbounded.items[1]).state
        assert len(first.content) <= MAX_CONTROL_DOCUMENT_BYTES
        direct = api.get(f"/api/operations/{indivisible_id}", headers=headers)
        assert direct.status_code == 200
        assert direct.json()["id"] == indivisible_id
        assert direct.json()["observation_unavailable"]
        assert direct.json()["node_ids"] == [NODE]
        assert len(direct.content) <= MAX_CONTROL_DOCUMENT_BYTES
        indivisible_id = None
        resumed = api.get(
            "/api/operations",
            headers=headers,
            params={"limit": 20},
        )
        assert resumed.status_code == 200 and resumed.json()["total"] == 20
        assert (
            resumed.json()["operations"][0]["id"]
            == operation_item(unbounded.items[0]).id
        )
        healed = api.get(
            f"/api/operations/{operation_item(unbounded.items[1]).id}", headers=headers
        )
        assert (
            healed.status_code == 200 and not healed.json()["observation_unavailable"]
        )
    engine.dispose()
