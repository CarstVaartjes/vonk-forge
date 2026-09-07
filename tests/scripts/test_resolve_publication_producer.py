from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "b" * 40
BASE = "a" * 40


def module():
    loader = importlib.machinery.SourceFileLoader(
        "producer_ready", str(ROOT / "scripts/resolve-publication-producer")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec
    result = importlib.util.module_from_spec(spec)
    loader.exec_module(result)
    return result


@pytest.mark.parametrize(
    ("workflow", "changed", "expected"),
    [
        ("installer-setups.yml", "scripts/select-pytest-shard-files", False),
        ("installer-setups.yml", "rust/crates/vonk-nas-setup/src/lib.rs", True),
        ("agent-release.yml", "packaging/debian/postinst", True),
        ("dev-images.yml", "config/execution-harnesses/vllm.json", True),
        ("dev-images.yml", "docs/something.md", False),
    ],
)
def test_ancestor_reuse_checks_actual_producer_filters(
    monkeypatch, workflow, changed, expected
):
    resolver = module()

    def command(*args):
        if args[1] == "show":
            return (ROOT / ".github/workflows" / workflow).read_text()
        assert "--no-renames" in args
        return changed

    monkeypatch.setattr(resolver, "run", command)
    assert (
        resolver.changed_matches(BASE, SOURCE, resolver.paths_at(SOURCE, workflow))
        is expected
    )


@pytest.mark.parametrize(
    ("status", "conclusion", "expected"),
    [
        ("in_progress", "", 2),
        ("completed", "failure", 1),
        ("completed", "success", 0),
    ],
)
@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_exact_producer_cannot_fall_back_while_running_or_failed(
    monkeypatch, status, conclusion, expected, event
):
    resolver = module()

    def command(*args):
        assert "head_sha=" in args[-1]
        assert "event=push" not in args[-1]
        return json.dumps(
            {
                "workflow_runs": [
                    {
                        "id": 5,
                        "run_number": 2,
                        "head_sha": SOURCE,
                        "head_branch": "main",
                        "event": event,
                        "status": status,
                        "conclusion": conclusion,
                    }
                ]
            }
        )

    monkeypatch.setattr(resolver, "run", command)
    actual, evidence = resolver.resolve("dev-images.yml", SOURCE, "owner/repo")
    assert actual == expected
    assert evidence == ((5, 2, SOURCE) if expected == 0 else None)


def test_latest_exact_dispatch_supersedes_older_push(monkeypatch):
    resolver = module()
    runs = [
        {"id": 5, "run_number": 541, "event": "push"},
        {"id": 9, "run_number": 542, "event": "workflow_dispatch"},
        {"id": 10, "run_number": 543, "event": "pull_request"},
    ]
    for item in runs:
        item.update(head_sha=SOURCE, head_branch="main", status="completed", conclusion="success")
    def command(*args):
        assert "head_sha=" in args[-1]  # No ancestor lookup may replace this exact producer.
        return json.dumps({"workflow_runs": runs})
    monkeypatch.setattr(resolver, "run", command)
    assert resolver.resolve("agent-release.yml", SOURCE, "owner/repo") == (0, (9, 542, SOURCE))


def test_renaming_an_input_out_of_its_area_is_a_change(tmp_path, monkeypatch):
    resolver = module()
    monkeypatch.chdir(tmp_path)

    def git(*args):
        return subprocess.check_output(["git", *args], text=True).strip()

    git("init", "-q")
    git("config", "user.name", "CI test")
    git("config", "commit.gpgsign", "false")
    git("config", "user.email", "ci@example.invalid")
    (tmp_path / "packaging").mkdir()
    (tmp_path / "packaging/input").write_text("artifact input\n")
    git("add", ".")
    git("commit", "-qm", "input")
    before = git("rev-parse", "HEAD")
    (tmp_path / "docs").mkdir()
    git("mv", "packaging/input", "docs/input")
    git("commit", "-qm", "move input")
    assert resolver.changed_matches(before, git("rev-parse", "HEAD"), ["packaging/**"])
