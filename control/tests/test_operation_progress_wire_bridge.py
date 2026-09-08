"""Connected canonical Python producer -> deployed Rust progress graph."""

import json
import os
import subprocess

import pytest
from vonk_agent_protocol import OperationMemberProgress, OperationProgress


def test_python_progress_crosses_rust_parser_without_losing_measurements():
    probe = os.environ.get("VONK_PROGRESS_WIRE_PROBE")
    if not probe:
        pytest.skip("set VONK_PROGRESS_WIRE_PROBE to the compiled protocol probe")
    source = OperationProgress(
        phase="copying",
        completed_bytes=12,
        total_bytes=24,
        total_bytes_known=True,
        completed_items=1,
        total_items=2,
        bytes_per_second=3.0,
        smoothed_bytes_per_second=2.5,
        eta_seconds=4.8,
        elapsed_seconds=8.0,
        activity="active",
        observed_at="2026-09-08T00:00:08+00:00",
        last_progress_at="2026-09-08T00:00:08+00:00",
        members=[
            OperationMemberProgress(
                member_id="spark-a", phase="copying", completed_bytes=12, total_bytes=24
            )
        ],
    )
    document = source.model_dump(mode="json", exclude_none=True)
    completed = subprocess.run(
        [probe],
        input=json.dumps(document) + "\n",
        text=True,
        capture_output=True,
        check=True,
    )
    assert OperationProgress.model_validate_json(completed.stdout) == source
