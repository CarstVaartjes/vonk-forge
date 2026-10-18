from __future__ import annotations

import ast
import fnmatch
import importlib.machinery
import importlib.util
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
REPO = "owner/repo"


def module(name="producer_evidence", path="scripts/resolve-publication-producer"):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / path))
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
    shas = {"base": commit(tmp_path, "packaging/debian/input", "one\n")}
    shas["docs"] = commit(tmp_path, "docs/guide.md", "words\n")
    shas["changed"] = commit(tmp_path, "packaging/debian/input", "two\n")
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
        ("setups", "nas_setup/src/vonk_nas_setup/cli.py", True),
        ("agent", "packaging/debian/postinst", True),
        ("agent", ".github/actions/agent-apt-publish/action.yml", True),
        ("images", "control/src/vonk_control/harnesses/canonical_metadata.py", True),
        ("images", "control/src/x.py", True),
        ("images", "control/tests/x.py", False),
        ("images", "control/migrations/versions/x.py", True),
        ("images", "deploy/compose/tests/test_x.py", False),
        ("images", "deploy/compose/tests/test.env", True),
        ("images", "tools/openapi-client/package.json", True),
        ("images", "docs/something.md", False),
        ("images", ".github/workflows/installer-publication.yml", True),
        ("setups", "scripts/resolve-publication-producer", True),
        ("agent", "tests/nodes/test_agent_package_native_lifecycle.sh", True),
        ("agent", "rust/crates/vonk-spark-setup/src/lib.rs", True),
        ("agent", "control/src/vonk_control/contract_graph.py", True),
        ("agent", "control/tests/fixtures/compiled_workload_v2.json", True),
        ("agent", "control/tests/test_x.py", False),
        ("agent", "docs/x.md", False),
        ("agent", "schemas/example.json", False),
        ("agent", "src/cluster_profiles/cli.py", False),
        ("agent", "tools/openapi-client/package.json", False),
        ("setups", "schemas/example.json", False),
        ("setups", "src/cluster_profiles/cli.py", False),
        ("setups", "tools/openapi-client/package.json", False),
        ("setups", "control/src/vonk_control/resources/site-ports.json", True),
        ("setups", "schemas/install-release-manifest.schema.json", False),
        ("setups", "control/packaging/public-contracts.lock", False),
        ("setups", "tests/nodes/test_agent_package_native_lifecycle.sh", False),
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
    git("mv", "packaging/debian/input", "docs/input", cwd=tmp_path)
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


def image_job_inputs() -> set[str]:
    """The path patterns the images job reads, derived from its own sources."""
    images = module("dev_image_inputs", "scripts/dev-image-inputs")

    def pattern(path: str) -> str:
        path = path.rstrip("/")
        return f"{path}/**" if (ROOT / path).is_dir() else path

    wheel = re.search(
        r"^wheel_name=(\S+)$",
        (ROOT / "scripts/build-control-wheel").read_text(),
        re.MULTILINE,
    )
    assert wheel
    inputs: set[str] = set()
    # The Controller images read their COPY sources from the repository root.
    dockerfile = (ROOT / "control/Dockerfile").read_text()
    for line in re.findall(r"^COPY\s+(.+)$", dockerfile, re.MULTILINE):
        words = line.split()
        assert words[-1] != "\\", "continued COPY lines need a parser"
        if any(word.startswith("--from=") for word in words):
            continue
        for source in [word for word in words if not word.startswith("--")][:-1]:
            # The job builds the protocol wheel from agent_protocol sources.
            if source != f"inventory/wheels/{wheel[1]}":
                assert (ROOT / source).exists(), source
                inputs.add(pattern(source))
    # Hermes and LiteLLM build from isolated contexts under their policy.
    for path in (*sum(images.CONTEXTS.values(), ()), *images.POLICY):
        inputs.add(pattern(path))
    # Files and local actions the workflow itself names, comments aside.
    workflow = "\n".join(
        re.sub(r"(^|\s)#.*", "", line)
        for line in (ROOT / ".github/workflows/dev-images.yml").read_text().splitlines()
    )
    for token in re.findall(r"[\w.-]+(?:/[\w.-]+)+", workflow):
        parts = token.split("/")
        for start in range(len(parts) - 1):
            if (ROOT / "/".join(parts[start:])).is_file() and not local_output(
                "/".join(parts[start:])
            ):
                inputs.add("/".join(parts[start:]))
    pending = re.findall(r"uses:\s*\./(\S+)", workflow)
    while pending:
        action = pending.pop()
        if pattern(action) in inputs:
            continue
        inputs.add(pattern(action))
        pending += re.findall(
            r"uses:\s*\./(\S+)", (ROOT / action / "action.yml").read_text()
        )
    # A file inside a directory input is already covered by that directory.
    return {
        path
        for path in inputs
        if not any(
            other.endswith("/**") and other != path and path.startswith(other[:-2])
            for other in inputs
        )
    }


def test_image_producer_paths_are_exactly_the_image_job_inputs():
    resolver = module()
    paths = set(resolver.PRODUCERS["images"].paths)
    inputs = image_job_inputs() | set(resolver.ORCHESTRATION)
    # A missing input reuses a stale image; an extra one (such as control/**
    # with its tests) rebuilds images whose bytes did not change.
    assert sorted(inputs - paths) == [], "image inputs the producer ignores"
    assert sorted(paths - inputs) == [], "producer paths no image job reads"


# Cargo reads the workspace manifest, lock and toolchain pin from the root.
CARGO_WORKSPACE = ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml")
PATH_TOKEN = re.compile(r"[\w.@-]+(?:/[\w.@-]+)*")
IMPORT = re.compile(r"^\s*from\s+([\w.]+)\s+import\s+([\w, ]+)$", re.MULTILINE)
RUST_READS = (
    # include_str!/include_bytes! resolve against the including file ...
    (re.compile(r'include_(?:str|bytes)!\(\s*"([^"]+)"'), False),
    # ... and env!("CARGO_MANIFEST_DIR") joins against the crate directory.
    (
        re.compile(r'env!\("CARGO_MANIFEST_DIR"\)\)?\s*(?:\.join\(|,)\s*"/?([^"]+)"'),
        True,
    ),
)


def input_pattern(path: Path) -> str | None:
    """A repository file or directory as a producer path pattern."""
    path = path.resolve()
    if path == ROOT or not path.is_relative_to(ROOT) or not path.exists():
        return None
    relative = path.relative_to(ROOT).as_posix()
    return f"{relative}/**" if path.is_dir() else relative


def crate_closure(crates: set[str]) -> set[str]:
    """The crates and every crate they reach through a path dependency."""
    found: set[str] = set()
    pending = sorted(crates)
    while pending:
        crate = pending.pop()
        if crate in found:
            continue
        found.add(crate)
        manifest = tomllib.loads((ROOT / crate / "Cargo.toml").read_text())
        for table in [manifest, *manifest.get("target", {}).values()]:
            for kind in ("dependencies", "build-dependencies", "dev-dependencies"):
                for dependency in table.get(kind, {}).values():
                    if isinstance(dependency, dict) and "path" in dependency:
                        target = (ROOT / crate / dependency["path"]).resolve()
                        pending.append(target.relative_to(ROOT).as_posix())
    return found


def crate_inputs(crate: str) -> set[str]:
    """What building and testing the crate reads outside its own directory."""
    inputs: set[str | None] = set()
    directory = ROOT / crate
    for source in directory.rglob("*.rs"):
        text = source.read_text()
        for expression, from_crate in RUST_READS:
            for literal in expression.findall(text):
                base = directory if from_crate else source.parent
                inputs.add(input_pattern(base / literal))
        if source.name == "build.rs":
            # Build scripts name their declared inputs from the repository root.
            for literal in re.findall(r'"([\w.-]+/[\w./-]+)"', text):
                inputs.add(input_pattern(ROOT / literal))
    return {path for path in inputs if path and not path.startswith("rust/crates/")}


def local_output(path: str) -> bool:
    """Whether .gitignore names the path as a local output (generated, .state/)."""
    for line in (ROOT / ".gitignore").read_text().splitlines():
        # Name patterns such as *.pem would also match tracked inputs.
        if "/" not in line or line.startswith(("#", "!")):
            continue
        pattern = line.strip("/")
        parts = path.split("/")
        for start in range(1 if line.startswith("/") else len(parts)):
            candidate = "/".join(parts[start:])
            if fnmatch.fnmatchcase(candidate, pattern) or candidate.startswith(
                f"{pattern}/"
            ):
                return True
    return False


def named_inputs(path: str, *, text: str | None = None) -> set[str]:
    """Repository files a workflow, action or script names by path or import."""
    source = ROOT / path
    if text is None:
        text = source.read_text()
    text = "\n".join(re.sub(r"(^|\s)#.*", "", line) for line in text.splitlines())
    names: set[str] = set()
    for token in PATH_TOKEN.findall(text):
        parts = token.removeprefix("./").split("/")
        # A bare word names only a root file such as uv.lock or Cargo.lock.
        for start in range(max(len(parts) - 1, 1)):
            name = "/".join(parts[start:])
            if (len(parts) > 1 or "." in name) and (ROOT / name).is_file():
                names.add(name)
    for module, members in IMPORT.findall(text):
        stem = module.replace(".", "/")
        for base in (source.parent, ROOT):
            for candidate in (
                f"{stem}.py",
                *(f"{stem}/{member.strip()}.py" for member in members.split(",")),
            ):
                if (base / candidate).is_file():
                    names.add((base / candidate).relative_to(ROOT).as_posix())
    names = {name for name in names if not local_output(name)}
    for action in re.findall(r"uses:\s*\./([\w./-]+)", text):
        if (ROOT / action).is_dir():
            names.add(f"{action.rstrip('/')}/**")
    # A path template such as f"packaging/systemd/{name}" reads its directory.
    for directory in re.findall(r"([\w.-]+(?:/[\w.-]+)+)/\{", text):
        if (ROOT / directory).is_dir():
            names.add(f"{directory}/**")
    for project in re.findall(r"uv build --project (\S+)", text):
        names.add(f"{project}/**")
    return names


def declared_contract_inputs() -> set[str]:
    """What the contract generators read, as tools/generated_contracts.py declares."""
    tree = ast.parse((ROOT / "tools/generated_contracts.py").read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and getattr(node.target, "id", "") == "INPUTS"
        ):
            patterns = {
                input_pattern(ROOT / path) for path in ast.literal_eval(node.value)
            }
            return {pattern for pattern in patterns if pattern}
    raise AssertionError("tools/generated_contracts.py declares no INPUTS")


def job_inputs(sources: tuple[str, ...], crates: set[str]) -> set[str]:
    """The path patterns a producer's jobs read, derived from their own sources."""
    crates = crate_closure(crates)
    inputs = {*sources, *CARGO_WORKSPACE, *(f"{crate}/**" for crate in crates)}
    for crate in crates:
        inputs |= crate_inputs(crate)
    # The workflows and every local action they use ...
    pending = [
        f"{path[:-3]}/action.yml" if path.endswith("/**") else path for path in sources
    ]
    test_cutoffs: dict[str, Path] = {}
    python_action = ".github/actions/python-env/action.yml"
    while pending:
        path = pending.pop()
        if path == python_action:
            continue
        named = named_inputs(path)
        # Follow only enabled branches of the environment action, with the
        # caller's options. Counting its disabled generators rebuilds artifacts
        # whose actual inputs never changed.
        document = (
            yaml.safe_load((ROOT / path).read_text()) if path.endswith(".yml") else {}
        )
        jobs = document.get("jobs", {"action": document.get("runs", {})})
        for job in jobs.values():
            for step in job.get("steps", []):
                run = step.get("run", "")
                if cutoff := re.search(r"--confcutdir=(\S+)", run):
                    for test in re.findall(r"tests/[\w/]+[.]py", run):
                        test_cutoffs[test] = Path(cutoff[1])
                if step.get("uses") != "./.github/actions/python-env":
                    continue
                options = step.get("with", {})
                action = yaml.safe_load((ROOT / python_action).read_text())
                enabled = [
                    step
                    for step in action["runs"]["steps"]
                    if "if" not in step
                    or step["if"]
                    in (
                        f"inputs.contracts == '{options.get('contracts', 'full')}'",
                        f"inputs.cli-dependencies == '{options.get('cli-dependencies', 'true')}'",
                    )
                ]
                named |= named_inputs(python_action, text=yaml.safe_dump(enabled))
        pending += [
            f"{path[:-3]}/action.yml"
            for path in named - inputs
            if path.startswith(".github/actions/")
        ]
        inputs |= named
    # ... and one level into the scripts and tests they and build scripts run.
    for path in sorted(inputs):
        if path.startswith(("scripts/", "tests/")) and not path.endswith("/**"):
            inputs |= named_inputs(path)
            # pytest also loads every conftest.py above the test it runs.
            if path.startswith("tests/") and path.endswith(".py"):
                for parent in Path(path).parents:
                    cutoff = test_cutoffs.get(path)
                    if cutoff and parent != cutoff and cutoff not in parent.parents:
                        continue
                    if (ROOT / parent / "conftest.py").is_file():
                        conftest = (parent / "conftest.py").as_posix()
                        inputs |= {conftest, *named_inputs(conftest)}
    if "tools/generated_contracts.py" in inputs:
        inputs |= declared_contract_inputs()
    # A file inside a directory input is already covered by that directory.
    return {
        path
        for path in inputs
        if not any(
            other.endswith("/**") and other != path and path.startswith(other[:-2])
            for other in inputs
        )
    }


def producer_job_inputs(producer: str) -> set[str]:
    if producer == "agent":
        # agent-package-security runs cargo test and clippy over the workspace;
        # the apt job publishes this producer's package (installer-publication).
        workspace = tomllib.loads((ROOT / "Cargo.toml").read_text())["workspace"]
        return job_inputs(
            (
                ".github/workflows/agent-release.yml",
                ".github/actions/agent-apt-publish/**",
            ),
            set(workspace["members"]),
        )
    # installer-setups tests and builds only the native Spark setup crate.
    return job_inputs(
        (".github/workflows/installer-setups.yml",),
        {"rust/crates/vonk-spark-setup"},
    )


@pytest.mark.parametrize("producer", ["agent", "setups"])
def test_producer_paths_are_exactly_the_job_inputs(producer):
    resolver = module()
    paths = set(resolver.PRODUCERS[producer].paths)
    inputs = producer_job_inputs(producer) | set(resolver.ORCHESTRATION)
    # A missing input reuses a stale artifact; an extra one rebuilds an
    # artifact whose inputs did not change.
    assert sorted(inputs - paths) == [], f"{producer} inputs the producer ignores"
    assert sorted(paths - inputs) == [], f"{producer} paths no job reads"


def test_cargo_wire_export_reads_only_declared_cli_inputs(tmp_path):
    """Catches pruning real transitive inputs or letting Cargo need generated
    CLI clients again: deny every undeclared read in the excluded directories."""
    program = r"""
import fnmatch, importlib.util, runpy, sys
from pathlib import Path
root = Path.cwd()
spec = runpy.run_path("scripts/resolve-publication-producer")
paths = spec["PRODUCERS"][spec["ProducerName"].AGENT].paths
watched = ("schemas/", "src/cluster_profiles/", "tools/openapi-client/")
def audit(event, args):
    if event != "open" or not isinstance(args[0], str):
        return
    path = Path(args[0]).resolve()
    if not path.is_relative_to(root):
        return
    if path.suffix == ".pyc":
        path = Path(importlib.util.source_from_cache(str(path)))
    relative = path.relative_to(root).as_posix()
    if relative.startswith(watched) and not any(fnmatch.fnmatchcase(relative, pattern) for pattern in paths):
        raise AssertionError(f"undeclared Cargo schema input: {relative}")
sys.addaudithook(audit)
sys.path.insert(0, str(root / "scripts"))
sys.argv = ["scripts/export-agent-wire-schema", "--output", sys.argv[1]]
runpy.run_path("scripts/export-agent-wire-schema", run_name="__main__")
"""
    subprocess.run(
        [sys.executable, "-c", program, str(tmp_path / "wire.json")],
        cwd=ROOT,
        check=True,
        timeout=60,
        env={**os.environ, "UV_PROJECT_ENVIRONMENT": sys.prefix},
    )
