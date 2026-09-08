"""Exercise the bounded acceptance diagnostic query on PostgreSQL."""

import json
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text


def test_preflight_query_projects_only_comparison_fields(postgres_engine) -> None:
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
            "CREATE TEMP TABLE jobs (id text, result jsonb)",
            "CREATE TEMP TABLE agent_nodes (node_id text, capabilities jsonb)",
            "CREATE TEMP TABLE agent_operations (id text, node_id text, kind text, payload_digest text, current_attempt int, updated_at int)",
            "CREATE TEMP TABLE agent_operation_attempts (operation_id text, attempt int, result jsonb)",
        ):
            connection.exec_driver_sql(statement)
        connection.execute(
            text("INSERT INTO jobs VALUES (:id, CAST(:result AS jsonb))"),
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
                "INSERT INTO agent_operations VALUES ('probe', :node, 'runtime.preflight.v1', :digest, 1, 100)"
            ),
            {"node": node_id, "digest": "c" * 64},
        )
        connection.execute(
            text(
                "INSERT INTO agent_operation_attempts VALUES ('probe', 1, CAST(:result AS jsonb))"
            ),
            {"result": json.dumps(receipt)},
        )
        repository = Path(__file__).resolve().parents[2]
        query = subprocess.check_output(
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
        ).strip()
        evidence = [row[0] for row in connection.execute(text(query))]
    assert len(evidence) == 1
    assert evidence[0]["current_fingerprint"] == "d" * 64
    assert evidence[0]["receipt_fingerprint"] == "b" * 64
    assert evidence[0]["payload_sha256"] == evidence[0]["request_sha256"] == "c" * 64
    assert evidence[0]["observed_at"] == 100
    assert evidence[0]["controller_now"] > 100
    assert "must-not-appear" not in json.dumps(evidence)
