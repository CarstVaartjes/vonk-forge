from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REPO = "owner/repo"


def module():
    loader = importlib.machinery.SourceFileLoader(
        "tested_ci", str(ROOT / "scripts/resolve-tested-ci")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec
    result = importlib.util.module_from_spec(spec)
    loader.exec_module(result)
    return result


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=root, text=True, timeout=10
    ).strip()


def commit(root: Path, path: str, content: str) -> str:
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content)
    git(root, "add", "--", path)
    git(root, "commit", "-qm", path)
    return git(root, "rev-parse", "HEAD")


def zipped(raw: bytes, *, name: str = "receipt.json") -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, raw)
    return output.getvalue()


@pytest.fixture
def proof(tmp_path, monkeypatch, request):
    """A real PR synthetic merge and a squash have identical trees, not SHAs."""
    resolver = module()
    monkeypatch.chdir(tmp_path)
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.name", "CI test")
    git(tmp_path, "config", "user.email", "ci@example.invalid")
    git(tmp_path, "config", "commit.gpgsign", "false")
    base = commit(tmp_path, "docs/base.md", "base\n")
    git(tmp_path, "checkout", "-qb", "feature")
    feature_path = (
        "scripts/feature"
        if getattr(request, "param", None) == "runtime"
        else "docs/feature.md"
    )
    head = commit(tmp_path, feature_path, "feature\n")
    git(tmp_path, "checkout", "-q", "main")
    if getattr(request, "param", None) == "advanced-base":
        commit(tmp_path, "docs/new-main.md", "main advanced before synthetic merge\n")
    git(tmp_path, "merge", "--no-ff", "-qm", "tested PR", "feature")
    source = git(tmp_path, "rev-parse", "HEAD")
    merge_base = git(tmp_path, "rev-parse", "HEAD^1")
    tree = git(tmp_path, "rev-parse", "HEAD^{tree}")
    event = {
        "number": 7,
        "pull_request": {
            "head": {"sha": head, "repo": {"full_name": REPO}},
            "base": {"sha": base, "ref": "main", "repo": {"full_name": REPO}},
        },
    }
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps(event))
    selected = resolver.selected_for(base, source)
    for key, value in {
        "GITHUB_EVENT_PATH": str(event_file),
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_REPOSITORY": REPO,
        "GITHUB_RUN_ID": "50",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_SHA": source,
        "SELECTED_AREAS": json.dumps(
            {name: str(value).lower() for name, value in selected.items()}
        ),
    }.items():
        monkeypatch.setenv(key, value)
    output = tmp_path / "proof" / "receipt.json"
    resolver.write_receipt(output)
    receipt = json.loads(output.read_bytes())
    target = git(tmp_path, "commit-tree", tree, "-p", base, "-m", "squashed PR")
    git(tmp_path, "checkout", "-q", "--detach", target)
    git(tmp_path, "remote", "add", "origin", str(tmp_path))
    pull = {
        "number": 7,
        "merge_commit_sha": target,
        "merged_at": "2026-10-12T00:00:00Z",
        "base": event["pull_request"]["base"],
        "head": event["pull_request"]["head"],
    }
    record = {
        "id": 50,
        "run_attempt": 2,
        "head_sha": head,
        "head_repository": {"full_name": REPO},
        "event": "pull_request",
        "path": resolver.WORKFLOW,
        "status": "completed",
        "conclusion": "success",
        # Actual GitHub runs can have no associated PR list after merging.
        "pull_requests": [],
    }
    artifact = {
        "id": 90,
        "name": resolver.ARTIFACT,
        "expired": False,
        "size_in_bytes": len(zipped(output.read_bytes())),
        "workflow_run": {"id": 50, "head_sha": head},
    }
    remote_commit = {
        "sha": source,
        "tree": {"sha": tree},
        "parents": [{"sha": merge_base}, {"sha": head}],
    }
    base_tree = git(tmp_path, "rev-parse", f"{base}^{{tree}}")

    def git_tree(identity):
        entries = []
        for line in git(tmp_path, "ls-tree", "-r", identity).splitlines():
            metadata, path = line.split("\t", 1)
            mode, kind, sha = metadata.split()
            entries.append({"path": path, "mode": mode, "type": kind, "sha": sha})
        return {"sha": identity, "truncated": False, "tree": entries}

    state = {
        "resolver": resolver,
        "root": tmp_path,
        "target": target,
        "receipt": receipt,
        "raw": output.read_bytes(),
        "pull": pull,
        "run": record,
        "artifact": artifact,
        "commit": remote_commit,
        "artifacts": [artifact],
        "runs": [record],
        "pulls": [pull],
        "zip_name": "receipt.json",
        "trees": {base_tree: git_tree(base_tree), tree: git_tree(tree)},
    }

    def api(endpoint, *, deadline=None):
        if "/commits/" in endpoint and "/pulls?" in endpoint:
            return state["pulls"]
        if "/workflows/ci.yml/runs?" in endpoint:
            return {"workflow_runs": state["runs"]}
        if "/runs/50/artifacts?" in endpoint:
            return {"artifacts": state["artifacts"]}
        if endpoint.endswith("/runs/50"):
            return state["run"]
        if "/git/commits/" in endpoint:
            if endpoint.endswith(f"/{base}"):
                return {"sha": base, "tree": {"sha": base_tree}}
            return state["commit"]
        if "/git/trees/" in endpoint:
            return state["trees"][endpoint.split("/git/trees/")[1].split("?")[0]]
        raise AssertionError(endpoint)

    original_command = resolver.command

    def command(*args, **kwargs):
        if args[:2] == ("gh", "api"):
            assert args[2] == f"repos/{REPO}/actions/artifacts/90/zip"
            return zipped(state["raw"], name=state["zip_name"])
        return original_command(*args, **kwargs)

    monkeypatch.setattr(resolver, "api", api)
    monkeypatch.setattr(resolver, "command", command)
    return state


def test_real_producer_to_artifact_consumer_reuses_identical_squash_tree(proof):
    resolver = proof["resolver"]
    assert proof["target"] != proof["receipt"]["source_sha"]
    assert proof["receipt"]["selected"]["compose"] is False
    assert resolver.resolve(proof["target"], REPO) == (0, 50)


@pytest.mark.parametrize("proof", ["runtime"], indirect=True)
def test_runtime_pr_requires_and_reuses_all_source_integrations(proof):
    assert all(proof["receipt"]["selected"].values())
    assert proof["resolver"].resolve(proof["target"], REPO) == (0, 50)


@pytest.mark.parametrize("proof", ["advanced-base"], indirect=True)
def test_producer_preserves_selector_base_when_synthetic_merge_parent_advances(proof):
    assert proof["receipt"]["base_sha"] != proof["receipt"]["merge_base_sha"]
    assert proof["resolver"].resolve(proof["target"], REPO) == (0, 50)


def test_intervening_main_change_runs_source_ci_again(proof):
    target = commit(proof["root"], "docs/other.md", "another main change\n")
    proof["pull"]["merge_commit_sha"] = target
    assert proof["resolver"].resolve(target, REPO) == (2, None)


@pytest.mark.parametrize("missing", ["pulls", "runs", "artifacts"])
def test_absent_proof_falls_back_to_ordinary_ci(proof, missing):
    proof[missing] = []
    assert proof["resolver"].resolve(proof["target"], REPO) == (2, None)


@pytest.mark.parametrize("listing", ["runs", "artifacts"])
def test_unfinished_pagination_runs_ordinary_ci_at_shared_deadline(
    proof, monkeypatch, listing
):
    resolver = proof["resolver"]
    original_api = resolver.api
    elapsed = 0.0
    calls = 0
    monkeypatch.setattr(resolver, "monotonic", lambda: elapsed, raising=False)

    def api(endpoint, *, deadline=None):
        nonlocal elapsed, calls
        matches = (
            "/workflows/ci.yml/runs?" in endpoint
            if listing == "runs"
            else "/artifacts?" in endpoint
        )
        if matches:
            calls += 1
            assert calls <= 3, "evidence pagination exceeded its time budget"
            elapsed += 61.0
            if listing == "runs":
                proof["run"]["conclusion"] = "failure"
                return {"workflow_runs": [proof["run"]] * 100}
            # A match on a partial page must not authorize reuse: an unseen
            # page could contain another matching artifact.
            return {
                "artifacts": [proof["artifact"]]
                + [{"name": "unrelated", "expired": False}] * 99
            }
        return original_api(endpoint, deadline=deadline)

    monkeypatch.setattr(resolver, "api", api)
    monkeypatch.setattr("sys.argv", ["resolve-tested-ci", proof["target"]])
    assert resolver.main() == 2


def test_evidence_subprocess_uses_remaining_lookup_time(monkeypatch):
    resolver = module()
    monkeypatch.setattr(resolver, "monotonic", lambda: 115.0, raising=False)
    timeouts = []

    def run(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        return subprocess.CompletedProcess(args[0], 0, b"{}", b"")

    monkeypatch.setattr(resolver.subprocess, "run", run)
    resolver.command("gh", "api", "repos/owner/repo", deadline=120.0)
    assert timeouts == [5.0]
    with pytest.raises(resolver.UnavailableEvidence, match="deadline"):
        resolver.command("gh", "api", "repos/owner/repo", deadline=115.0)
    assert timeouts == [5.0]


def test_expired_proof_falls_back_to_ordinary_ci(proof):
    proof["artifact"]["expired"] = True
    assert proof["resolver"].resolve(proof["target"], REPO) == (2, None)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path", ".github/workflows/other.yml"),
        ("event", "workflow_dispatch"),
        ("conclusion", "failure"),
        ("status", "in_progress"),
        ("head_sha", "b" * 40),
    ],
)
def test_foreign_or_unsuccessful_run_never_excuses_source_checks(proof, field, value):
    proof["run"][field] = value
    assert proof["resolver"].resolve(proof["target"], REPO) == (2, None)


def test_fork_proof_never_excuses_source_checks(proof):
    proof["pull"]["head"] = dict(proof["pull"]["head"], repo={"full_name": "fork/repo"})
    assert proof["resolver"].resolve(proof["target"], REPO) == (2, None)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("repository", "foreign/repo"),
        ("pr_number", 8),
        ("run_id", 51),
        ("run_attempt", 3),
        ("head_sha", "b" * 40),
        ("tree_sha", "c" * 40),
        ("base_sha", "d" * 40),
        ("merge_base_sha", "e" * 40),
        ("schema_version", 1),
        ("run_id", True),
    ],
)
def test_available_forged_identity_fails_closed(proof, field, value):
    proof["receipt"][field] = value
    proof["raw"] = json.dumps(proof["receipt"]).encode()
    with pytest.raises(proof["resolver"].EvidenceError):
        proof["resolver"].resolve(proof["target"], REPO)


def test_underselected_source_checks_fail_closed(proof):
    proof["receipt"]["selected"]["control"] = False
    proof["raw"] = json.dumps(proof["receipt"]).encode()
    with pytest.raises(proof["resolver"].EvidenceError, match="selection"):
        proof["resolver"].resolve(proof["target"], REPO)


def test_previous_attempt_receipt_is_ineligible_after_retry(proof):
    proof["run"]["run_attempt"] = 3
    assert proof["resolver"].resolve(proof["target"], REPO) == (2, None)


def test_stale_pr_api_base_does_not_discard_identical_tested_tree(proof):
    proof["pull"]["base"] = dict(proof["pull"]["base"], sha="e" * 40)
    assert proof["resolver"].resolve(proof["target"], REPO) == (0, 50)


def test_artifact_cannot_be_borrowed_from_another_run(proof):
    proof["artifact"]["workflow_run"]["id"] = 51
    with pytest.raises(proof["resolver"].EvidenceError, match="artifact"):
        proof["resolver"].resolve(proof["target"], REPO)


@pytest.mark.parametrize("archive_name", ["../receipt.json", "nested/receipt.json"])
def test_archive_is_read_without_extracting_paths(proof, archive_name):
    proof["zip_name"] = archive_name
    with pytest.raises(proof["resolver"].EvidenceError, match="archive"):
        proof["resolver"].resolve(proof["target"], REPO)


def test_duplicate_json_fields_fail_closed(proof):
    proof["raw"] = proof["raw"].rstrip()[:-1] + b',"run_id":50}'
    with pytest.raises(proof["resolver"].EvidenceError, match="duplicate"):
        proof["resolver"].resolve(proof["target"], REPO)


def test_receipt_size_is_bounded_before_reading_zip(proof):
    proof["artifact"]["size_in_bytes"] = proof["resolver"].MAX_ARCHIVE_BYTES + 1
    with pytest.raises(proof["resolver"].EvidenceError, match="byte limit"):
        proof["resolver"].resolve(proof["target"], REPO)


def test_producer_refuses_a_checkout_other_than_the_ci_source(proof, monkeypatch):
    # The fixture now checks out the squash; its event still names the PR merge.
    with pytest.raises(proof["resolver"].EvidenceError, match="tested merge"):
        proof["resolver"].write_receipt(proof["root"] / "another.json")


def test_truncated_remote_tree_runs_normal_ci(proof, monkeypatch):
    proof["trees"][proof["receipt"]["tree_sha"]]["truncated"] = True
    monkeypatch.setattr("sys.argv", ["resolve-tested-ci", proof["target"]])
    assert proof["resolver"].main() == 2


def test_unavailable_api_runs_normal_ci(proof, monkeypatch, capsys):
    def unavailable(endpoint, *, deadline=None):
        raise subprocess.CalledProcessError(1, ["gh", "api", endpoint])

    monkeypatch.setattr(proof["resolver"], "api", unavailable)
    monkeypatch.setattr("sys.argv", ["resolve-tested-ci", proof["target"]])
    assert proof["resolver"].main() == 2
    assert "run CI" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (404, "UnavailableEvidence"),
        (410, "UnavailableEvidence"),
        (401, "EvidenceError"),
        (403, "EvidenceError"),
        (500, "EvidenceError"),
    ],
)
def test_github_absence_does_not_hide_access_denial(monkeypatch, status, error):
    resolver = module()

    def failure(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0], 1, b"", f"gh: error (HTTP {status})".encode()
        )

    monkeypatch.setattr(resolver.subprocess, "run", failure)
    with pytest.raises(getattr(resolver, error)):
        resolver.command("gh", "api", "repos/owner/repo/actions/artifacts/90/zip")
