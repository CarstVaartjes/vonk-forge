"""Exercise the bounded acceptance diagnostic query on PostgreSQL."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text


@pytest.mark.parametrize("expired", [False, True])
def test_preflight_query_projects_only_comparison_fields(
    postgres_engine, expired
) -> None:
    operation_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    node_id = "spk_" + "a" * 32
    receipt = {
        "fingerprint": "b" * 64,
        "request_sha256": "c" * 64,
        "observed_at": 100,
        "findings": [],
        "private": "must-not-appear",
    }
    with postgres_engine.begin() as connection:
        for statement in (
            "CREATE TEMP TABLE jobs (id text, result jsonb, state text)",
            "CREATE TEMP TABLE agent_nodes (node_id text, capabilities jsonb)",
            "CREATE TEMP TABLE agent_operations (id text, node_id text, kind text, payload_digest text, current_attempt int, updated_at int, parent_job_id text)",
            "CREATE TEMP TABLE agent_operation_attempts (operation_id text, attempt int, result jsonb, state text, lease_deadline timestamptz, progress jsonb)",
        ):
            connection.exec_driver_sql(statement)
        connection.execute(
            text("INSERT INTO jobs (id,result) VALUES (:id, CAST(:result AS jsonb))"),
            {
                "id": operation_id,
                "result": json.dumps({"preflight": {"receipts": {node_id: receipt}}}),
            },
        )
        connection.execute(
            text("INSERT INTO agent_nodes VALUES (:node, CAST(:caps AS jsonb))"),
            {
                "node": node_id,
                "caps": json.dumps(["runtime.preflight.fingerprint." + "d" * 64]),
            },
        )
        connection.execute(
            text(
                "INSERT INTO agent_operations (id,node_id,kind,payload_digest,current_attempt,updated_at) VALUES ('probe', :node, 'runtime.preflight.v1', :digest, 1, 100)"
            ),
            {"node": node_id, "digest": "c" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO agent_operation_attempts (operation_id,attempt,result) VALUES ('probe', 1, CAST(:result AS jsonb))"
            ),
            {"result": json.dumps(receipt)},
        )
        if expired:
            connection.execute(
                text(
                    "UPDATE jobs SET result=jsonb_set(result,'{preflight,pending_job_id}','\"pending-job\"'::jsonb)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO jobs (id,state) VALUES ('pending-job','waiting-for-operator')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO agent_operations (id,parent_job_id,current_attempt) VALUES ('pending','pending-job',1)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO agent_operation_attempts (operation_id,attempt,state,lease_deadline) VALUES ('pending',1,'expired','2026-09-08T12:00:00Z')"
                )
            )
        repository = Path(__file__).resolve().parents[2]
        queries = subprocess.check_output(
            [
                sys.executable,
                "-c",
                (
                    "import runpy; m=runpy.run_path('tests/acceptance/test_spark_lifecycle.py'); "
                    "c=m['SparkLifecycle']; r=c.__new__(c); r._psql=lambda q: print(q) or []; "
                    f"r._preflight_failure_evidence('{operation_id}')"
                ),
            ],
            cwd=repository,
            text=True,
        ).splitlines()
        evidence = [
            row[0] for query in queries for row in connection.execute(text(query))
        ]
    assert len(evidence) == (2 if expired else 1)
    if expired:
        assert evidence[1]["child_state"] == "waiting-for-operator"
        assert evidence[1]["attempt_state"] == "expired"
        assert evidence[1]["lease_deadline"] == "2026-09-08T12:00:00+00:00"
    assert evidence[0]["current_fingerprint"] == "d" * 64
    assert evidence[0]["receipt_fingerprint"] == "b" * 64
    assert evidence[0]["payload_sha256"] == evidence[0]["request_sha256"] == "c" * 64
    assert evidence[0]["observed_at"] == 100
    assert evidence[0]["controller_now"] > 100
    assert "must-not-appear" not in json.dumps(evidence)
