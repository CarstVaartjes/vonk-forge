"""The real ordinary job-target Stop grant reaches the hosted Rust verifier."""

import os
import subprocess
from datetime import datetime

from vonk_agent_protocol import canonical_message
from vonk_agent_protocol.host_helper import SignedHostHelperGrant

from .test_artifact_residue_stop import (
    test_unknown_cancelled_job_retains_run_claims_until_exact_stop_receipt as _ordinary_job_target_recovery,
)
from .wire_probes import prebuilt_probe


def test_ordinary_job_target_grant_is_accepted_by_rust_helper(tmp_path):
    """Hosted prebuilt helper verifies the real producer grant unchanged."""
    probe = prebuilt_probe("VONK_HOST_HELPER_WIRE_PROBE")
    observed: list[tuple[SignedHostHelperGrant, datetime]] = []
    _ordinary_job_target_recovery(
        tmp_path, grant_observer=lambda grant, now: observed.append((grant, now))
    )
    assert len(observed) == 1
    grant, now = observed[0]
    raw = canonical_message(grant)
    result = subprocess.run(
        [str(probe)],
        input=raw,
        capture_output=True,
        timeout=10,
        check=False,
        env={**os.environ, "VONK_HOST_HELPER_WIRE_NOW": str(int(now.timestamp()))},
    )
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.rstrip(b"\n") == raw
