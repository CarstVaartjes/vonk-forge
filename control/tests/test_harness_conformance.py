from __future__ import annotations

import pytest

from vonk_control.harness_conformance import run_synthetic_conformance
from vonk_control.harnesses import BUILTIN_HARNESS_SLUGS


@pytest.mark.parametrize("slug", BUILTIN_HARNESS_SLUGS)
def test_harness_completes_synthetic_lifecycle(slug: str) -> None:
    evidence = run_synthetic_conformance(slug)

    assert evidence.phases == (
        "inspect",
        "prepare",
        "verify",
        "start",
        "ready",
        "invoke",
        "inspect",
        "stop",
        "verify-stopped",
    )
    assert evidence.offline_runtime is True
    assert evidence.security["docker_socket"] is False
    assert evidence.security == {
        "architecture": "linux/arm64",
        "capabilities": [],
        "docker_socket": False,
        "model_mounts_read_only": True,
        "no_new_privileges": True,
        "numeric_non_root_uid": True,
        "outputs_isolated": True,
    }
    assert evidence.interrupted_start_recovered is True
    assert evidence.interrupted_stop_recovered is True
    assert evidence.stop_bounded is True
    assert evidence.recovery_phases == (
        "start-interrupted",
        "inspect-idempotent",
        "start-recovered",
        "stop-interrupted",
        "inspect-idempotent",
        "stop-recovered",
    )
    assert evidence.document["schema_version"] == 1


def test_conformance_fails_closed_for_unknown_harness() -> None:
    with pytest.raises(ValueError, match="unknown execution harness"):
        run_synthetic_conformance("legacy-harness")
