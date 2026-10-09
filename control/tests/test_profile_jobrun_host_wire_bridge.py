"""The real selected-profile Stop grant reaches the hosted Rust verifier."""

import os
import subprocess

from vonk_agent_protocol import canonical_message

from .test_profile_jobrun_host_grant import _selected_profile_grant
from .wire_probes import prebuilt_probe


def test_selected_profile_job_target_grant_is_accepted_by_rust_helper(tmp_path):
    """Hosted prebuilt helper verifies the real producer grant unchanged."""
    probe = prebuilt_probe("VONK_HOST_HELPER_WIRE_PROBE")
    grant, now = _selected_profile_grant(tmp_path)
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
