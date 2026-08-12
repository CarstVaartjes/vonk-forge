from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "nodes" / "bin" / "apply-node-policy"
DEFAULT_POLICY = ROOT / "nodes" / "policy" / "default.json"


def _fake_earlyoom(tmp_path: Path) -> tuple[Path, Path, Path]:
    state = tmp_path / "earlyoom-state"
    state.write_text("change-required\n")
    actions = tmp_path / "actions"
    actions.write_text("")
    helper = tmp_path / "disable-earlyoom"
    helper.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
state="$(cat "${POLICY_TEST_STATE:?}")"
case "${1:-}" in
  --check)
    if [[ "$state" == compliant ]]; then exit 0; fi
    exit 2
    ;;
  --apply)
    printf 'apply\n' >> "${POLICY_TEST_ACTIONS:?}"
    printf 'compliant\n' > "${POLICY_TEST_STATE:?}"
    ;;
  *) exit 64 ;;
esac
"""
    )
    helper.chmod(0o755)
    return helper, state, actions


def _run(
    policy: Path,
    action: str,
    helper: Path,
    state: Path,
    actions: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), "--policy", str(policy), action],
        env={
            **os.environ,
            "VONK_DISABLE_EARLYOOM_BIN": str(helper),
            "POLICY_TEST_STATE": str(state),
            "POLICY_TEST_ACTIONS": str(actions),
        },
        check=False,
        capture_output=True,
        text=True,
    )


def test_policy_apply_refuses_platform_mutation_and_reports_stable_digest(
    tmp_path: Path,
) -> None:
    helper, state, actions = _fake_earlyoom(tmp_path)

    first = _run(DEFAULT_POLICY, "--apply", helper, state, actions)
    second = _run(DEFAULT_POLICY, "--apply", helper, state, actions)

    assert first.returncode == 3, first.stderr
    assert second.returncode == 3, second.stderr
    first_result = json.loads(first.stdout)
    second_result = json.loads(second.stdout)
    assert first_result["status"] == "operator-action-required"
    assert second_result["status"] == "operator-action-required"
    assert first_result["policy_sha256"] == second_result["policy_sha256"]
    assert len(first_result["policy_sha256"]) == 64
    assert state.read_text() == "change-required\n"
    assert actions.read_text() == ""


def test_policy_check_reports_change_without_mutating(tmp_path: Path) -> None:
    helper, state, actions = _fake_earlyoom(tmp_path)

    result = _run(DEFAULT_POLICY, "--check", helper, state, actions)

    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "change-required"
    assert state.read_text() == "change-required\n"
    assert actions.read_text() == ""


def test_policy_verify_fails_when_node_is_not_compliant(tmp_path: Path) -> None:
    helper, state, actions = _fake_earlyoom(tmp_path)

    result = _run(DEFAULT_POLICY, "--verify", helper, state, actions)

    assert result.returncode == 3
    assert json.loads(result.stdout)["status"] == "noncompliant"
    assert actions.read_text() == ""


@pytest.mark.parametrize(
    "document",
    [
        {"schema_version": 2, "earlyoom": "disabled"},
        {"schema_version": 1, "earlyoom": "enabled"},
        {"schema_version": 1, "earlyoom": "disabled", "unknown": True},
        {"schema_version": 1},
    ],
)
def test_policy_rejects_unknown_version_values_and_fields(
    tmp_path: Path,
    document: dict[str, object],
) -> None:
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(document))
    helper, state, actions = _fake_earlyoom(tmp_path)

    result = _run(policy, "--check", helper, state, actions)

    assert result.returncode == 64
    assert result.stdout == ""
    assert actions.read_text() == ""


def test_default_policy_has_no_node_names_addresses_or_users() -> None:
    document = json.loads(DEFAULT_POLICY.read_text())
    encoded = json.dumps(document).lower()

    assert document == {"schema_version": 1, "earlyoom": "disabled"}
    assert "node1" not in encoded
    assert "192.168." not in encoded
    assert "carst" not in encoded
