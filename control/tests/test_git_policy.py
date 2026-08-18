from pathlib import Path

import pytest
from vonk_control.code_host import InMemoryCodeHost
from vonk_control.git_policy import (
    GitPolicy,
    IrreversiblePolicyError,
    PolicyStore,
    ReleaseGateError,
)
from vonk_control.proposals import ProposalPreview


def _preview(digest: str = "a" * 64) -> ProposalPreview:
    return ProposalPreview("admin", "b" * 40, b"patch", ("inventory/topology.json",), ("passed",), digest)


def test_release_mode_cannot_return_to_direct_and_survives_restart(tmp_path: Path) -> None:
    store = PolicyStore(tmp_path)
    assert store.mode == "development-direct"
    store.enable_release_pr_only(actor="admin", release_digest="f" * 64, release_status="passed")
    assert PolicyStore(tmp_path).mode == "release-pr-only"
    with pytest.raises(IrreversiblePolicyError):
        store.enable_development_direct(actor="admin")


def test_release_submission_uses_stable_branch_and_is_idempotent(tmp_path: Path) -> None:
    store = PolicyStore(tmp_path)
    store.enable_release_pr_only(actor="admin", release_digest="f" * 64, release_status="passed")
    host = InMemoryCodeHost(required_checks=("tests", "security"))
    policy = GitPolicy(store, host, protected_branch="deploy", required_checks=("tests", "security"))
    first = policy.submit(_preview(), actor="admin", request_id="req-1")
    second = policy.submit(_preview(), actor="admin", request_id="req-2")
    assert first == second
    assert first.mode == "pull-request"
    assert first.branch == f"vonk-control/{'a' * 12}"
    assert host.submission_count == 1
    assert "Proposal-Digest: " + "a" * 64 in host.last_message
    assert "Actor: admin" in host.last_message


def test_failed_release_cannot_enable_pr_only(tmp_path: Path) -> None:
    store = PolicyStore(tmp_path)
    with pytest.raises(ReleaseGateError):
        store.enable_release_pr_only(actor="admin", release_digest="f" * 64, release_status="blocked")
    assert store.mode == "development-direct"


def test_development_mode_creates_direct_audited_commit(tmp_path: Path) -> None:
    host = InMemoryCodeHost(required_checks=())
    policy = GitPolicy(PolicyStore(tmp_path), host, protected_branch="deploy", required_checks=())
    change = policy.submit(_preview(), actor="admin", request_id="req")
    assert change.mode == "direct-commit"
    assert change.branch == "deploy"


def test_unmerged_or_failing_commit_is_ineligible(tmp_path: Path) -> None:
    host = InMemoryCodeHost(required_checks=("tests", "security"))
    policy = GitPolicy(PolicyStore(tmp_path), host, protected_branch="deploy", required_checks=("tests", "security"))
    commit = host.seed_commit(merged=False, checks={"tests": "success", "security": "failure"})
    result = policy.eligible(commit)
    assert not result.ok
    assert any("not reachable" in reason for reason in result.reasons)
    assert "required check security is failure" in result.reasons


def test_merged_commit_requires_exact_configured_checks(tmp_path: Path) -> None:
    host = InMemoryCodeHost(required_checks=("tests",))
    policy = GitPolicy(PolicyStore(tmp_path), host, protected_branch="deploy", required_checks=("tests", "security"))
    commit = host.seed_commit(merged=True, checks={"tests": "success"})
    assert policy.eligible(commit).reasons == ("required check security is missing",)
    host.set_check(commit, "security", "success")
    assert policy.eligible(commit).ok
