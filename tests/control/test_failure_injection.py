import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_workload_failure_matrix_report_is_structured_and_secret_free() -> None:
    completed = subprocess.run(
        [ROOT / "scripts/accept-workload-package-failures", "--json"],
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(completed.stdout)
    assert report["report_type"] == "vonk-forge-workload-package-failure-matrix"
    assert report["failure_matrix"] is True
    assert report["status"] == "passed"
    assert report["physical_nodes_exercised"] is False
    assert report["ssh_calls"] == report["agent_update_calls"] == 0
    assert len(report["cases"]) >= 15
    assert all(
        {"family_id", "release_digest", "node_id", "fence", "reason_code", "disposition"}
        <= case.keys()
        for case in report["cases"]
    )
    assert "secret" not in completed.stdout.lower()
    assert "https://" not in completed.stdout
