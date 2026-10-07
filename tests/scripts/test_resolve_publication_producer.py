from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REPO = "owner/repo"


def module():
    loader = importlib.machinery.SourceFileLoader(
        "producer_evidence", str(ROOT / "scripts/resolve-publication-producer")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec
    result = importlib.util.module_from_spec(spec)
    loader.exec_module(result)
    return result


def git(*args: str, cwd: Path) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=cwd, text=True, timeout=10
    ).strip()


def commit(root: Path, path: str, content: str) -> str:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git("add", "-A", cwd=root)
    git("commit", "-qm", path, cwd=root)
    return git("rev-parse", "HEAD", cwd=root)


@pytest.fixture
def history(tmp_path, monkeypatch):
    """main: base -> docs -> packaging change; plus an unrelated branch."""
    monkeypatch.chdir(tmp_path)
    git("init", "-q", "-b", "main", cwd=tmp_path)
    git("config", "user.name", "CI test", cwd=tmp_path)
    git("config", "user.email", "ci@example.invalid", cwd=tmp_path)
    git("config", "commit.gpgsign", "false", cwd=tmp_path)
    shas = {"base": commit(tmp_path, "packaging/input", "one\n")}
    shas["docs"] = commit(tmp_path, "docs/guide.md", "words\n")
    shas["changed"] = commit(tmp_path, "packaging/input", "two\n")
    git("checkout", "-q", "-b", "side", shas["base"], cwd=tmp_path)
    shas["side"] = commit(tmp_path, "docs/side.md", "elsewhere\n")
    git("checkout", "-q", "main", cwd=tmp_path)
    return shas


class FakeGitHub:
    """Serves the three REST reads the resolver makes."""

    def __init__(self, resolver) -> None:
        self.resolver = resolver
        self.markers: list[dict] = []
        self.runs: dict[int, dict] = {}
        self.artifacts: dict[int, list[dict]] = {}
        self.calls: list[str] = []

    def release(
        self,
        run_id: int,
        sha: str,
        producer: str,
        *,
        artifacts: bool = True,
        path: str | None = None,
        branch: str = "main",
    ) -> None:
        self.runs[run_id] = {
            "id": run_id,
            "run_number": run_id + 1000,
            "path": path or self.resolver.RELEASE_WORKFLOW,
            "head_branch": branch,
            "head_sha": sha,
            "event": "push",
        }
        spec = self.resolver.PRODUCERS[producer]
        names = spec.artifact_names(sha) if artifacts else set()
        self.artifacts[run_id] = [
            {"name": name, "expired": False} for name in sorted(names)
        ]
        # The artifacts API lists the newest markers first.
        self.markers.insert(
            0,
            {
                "name": self.resolver.marker_name(producer),
                "expired": False,
                "workflow_run": {"id": run_id, "head_sha": sha, "head_branch": branch},
            },
        )

    def __call__(self, endpoint: str) -> dict:
        self.calls.append(endpoint)
        path, _, query = endpoint.partition("?")
        parameters = dict(item.split("=", 1) for item in query.split("&") if item)
        page = slice(
            (int(parameters.get("page", "1")) - 1) * 100,
            int(parameters.get("page", "1")) * 100,
        )
        if path == f"repos/{REPO}/actions/artifacts":
            matching = [m for m in self.markers if m["name"] == parameters["name"]]
            return {"artifacts": matching[page]}
        run_id = int(path.split("/")[5])
        if path.endswith("/artifacts"):
            return {"artifacts": self.artifacts[run_id][page]}
        return self.runs[run_id]


@pytest.fixture
def github(monkeypatch):
    resolver = module()
    fake = FakeGitHub(resolver)
    monkeypatch.setattr(resolver, "api", fake)
    return resolver, fake


@pytest.mark.parametrize(
    ("producer", "changed", "expected"),
    [
        ("setups", "scripts/select-pytest-shard-files", False),
        ("setups", "rust/crates/vonk-nas-setup/src/lib.rs", True),
        ("agent", "packaging/debian/postinst", True),
        ("agent", ".github/actions/agent-apt-publish/action.yml", True),
        ("images", "control/src/vonk_control/harnesses/canonical_metadata.py", True),
        ("images", "docs/something.md", False),
        ("images", ".github/workflows/installer-publication.yml", True),
        ("setups", "scripts/resolve-publication-producer", True),
    ],
)
def test_reuse_checks_each_producer_input_filter(
    monkeypatch, producer, changed, expected
):
    resolver = module()

    def command(*args):
        assert "--no-renames" in args
        return changed

    monkeypatch.setattr(resolver, "run", command)
    patterns = resolver.PRODUCERS[producer].paths
    assert resolver.changed_matches("a" * 40, "b" * 40, patterns) is expected


def test_renaming_an_input_out_of_its_area_is_a_change(history, tmp_path):
    resolver = module()
    before = git("rev-parse", "HEAD", cwd=tmp_path)
    git("mv", "packaging/input", "docs/input", cwd=tmp_path)
    git("commit", "-qm", "move input", cwd=tmp_path)
    assert resolver.changed_matches(
        before, git("rev-parse", "HEAD", cwd=tmp_path), ("packaging/**",)
    )


def test_unchanged_producer_reuses_the_newest_ancestor_release(
    history, github, tmp_path
):
    resolver, fake = github
    fake.release(10, history["base"], "agent")
    fake.release(11, history["docs"], "agent")
    assert resolver.resolve("agent", history["docs"], REPO) == (
        0,
        (11, 1011, history["docs"]),
    )
    # A documentation-only child keeps reusing the package built for docs.
    git("checkout", "-q", "-b", "later", history["docs"], cwd=tmp_path)
    child = commit(Path.cwd(), "docs/more.md", "more\n")
    assert resolver.resolve("agent", child, REPO) == (0, (11, 1011, history["docs"]))


def test_changed_inputs_build_even_when_an_older_release_matched(history, github):
    resolver, fake = github
    fake.release(10, history["base"], "agent")
    assert resolver.resolve("agent", history["changed"], REPO) == (2, None)
    # A producer whose inputs did not change still reuses the same ancestor.
    fake.release(12, history["base"], "setups")
    assert resolver.resolve("setups", history["changed"], REPO) == (
        0,
        (12, 1012, history["base"]),
    )


def test_missing_evidence_falls_back_to_an_older_complete_release(history, github):
    resolver, fake = github
    fake.release(10, history["base"], "setups")
    fake.release(11, history["docs"], "setups", artifacts=False)
    assert resolver.resolve("setups", history["docs"], REPO) == (
        0,
        (10, 1010, history["base"]),
    )


def test_expired_foreign_and_non_ancestor_markers_are_ignored(history, github):
    resolver, fake = github
    fake.release(10, history["side"], "images")
    fake.release(11, history["base"], "images", branch="feature")
    fake.release(12, history["base"], "images", path=".github/workflows/other.yml")
    fake.release(13, history["base"], "images")
    fake.markers[0]["expired"] = True
    assert resolver.resolve("images", history["docs"], REPO) == (2, None)


def test_no_recorded_producer_means_build_here(history, github):
    resolver, fake = github
    assert resolver.resolve("images", history["docs"], REPO) == (2, None)
    assert fake.calls == [
        f"repos/{REPO}/actions/artifacts?name=release-producer-images&per_page=100&page=1"
    ]


def test_markers_are_read_page_by_page_until_a_decision(history, github):
    resolver, fake = github
    fake.release(10, history["base"], "images")
    for run_id in range(100, 200):
        fake.release(run_id, history["side"], "images")
    assert resolver.resolve("images", history["docs"], REPO) == (
        0,
        (10, 1010, history["base"]),
    )
    assert fake.calls[1].endswith("&page=2")


@pytest.mark.parametrize(
    ("arguments", "status"),
    [
        (["unknown", "a" * 40], 1),
        (["agent", "not-a-sha"], 1),
        (["agent"], 64),
    ],
)
def test_invalid_requests_are_rejected(monkeypatch, arguments, status):
    resolver = module()
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setattr("sys.argv", ["resolve-publication-producer", *arguments])
    assert resolver.main() == status


def test_release_workflow_records_every_producer_the_resolver_reads():
    resolver = module()
    workflow = (ROOT / resolver.RELEASE_WORKFLOW).read_text()
    assert f"for producer in {' '.join(resolver.PRODUCERS)}; do" in workflow
    for producer in resolver.PRODUCERS:
        assert f"name: {resolver.marker_name(producer)}\n" in workflow
