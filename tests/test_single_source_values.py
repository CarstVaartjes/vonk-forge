"""Wrong implementations: handwritten/stale selections and unverified projections."""

from pathlib import Path

import pytest

from tools.single_source_values import violations

ROOT = Path(__file__).resolve().parents[1]


def test_registered_values_have_one_owner() -> None:
    assert violations(ROOT) == []


@pytest.mark.parametrize("suffix", (".py", ".yml", "Dockerfile", ".md", ""))
def test_guard_rejects_copied_policy_in_executable_surfaces(
    tmp_path: Path, suffix: str
) -> None:
    (tmp_path / "scripts").mkdir()
    registry = "scripts/single-source-values.toml"
    (tmp_path / registry).write_text(
        (ROOT / registry).read_text().split("[[values.exceptions]]")[0]
    )
    owner = "scripts/image_policy.py"
    (tmp_path / owner).write_text(
        'FLOATING_TAGS = frozenset({"latest", "main", "edge"})\n'
    )
    consumer = f"consumer{suffix}"
    source = 'tags = {"latest", "main", "edge"}\n'
    if suffix == ".md":
        source = f"```python\n{source}```\n"
    (tmp_path / consumer).write_text(source)
    errors = violations(tmp_path, [registry, owner, consumer])
    assert len(errors) == 1
    assert "images.floating_tags" in errors[0]
    assert f"{consumer}:" in errors[0]
    assert f"owner {owner}:FLOATING_TAGS" in errors[0]


def test_missing_owner_and_duplicate_registration_fail(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    registry = "scripts/single-source-values.toml"
    text = (ROOT / registry).read_text().split("[[values.exceptions]]")[0]
    (tmp_path / registry).write_text(text + text)
    with pytest.raises(ValueError, match="duplicate"):
        violations(tmp_path, [registry])
    (tmp_path / registry).write_text(text)
    (tmp_path / "scripts/image_policy.py").write_text("unrelated = 32\n")
    with pytest.raises(ValueError, match="owner selector"):
        violations(tmp_path, [registry])


def test_mirror_matrix_follows_changed_lock(tmp_path: Path) -> None:
    # Wrong implementation: the workflow mirrors a stale, independently pinned Tailscale.
    import json
    import subprocess
    import sys

    import yaml

    lock_path = tmp_path / "deploy/compose/images.lock.json"
    lock_path.parent.mkdir(parents=True)
    lock = json.loads((ROOT / "deploy/compose/images.lock.json").read_text())
    changed = "example.invalid/tail:changed@sha256:" + "a" * 64
    lock["images"]["tailscale"] = changed
    lock_path.write_text(json.dumps(lock))
    output = subprocess.check_output(
        [sys.executable, str(ROOT / "scripts/ci-image-mirror-inputs"), str(tmp_path)],
        text=True,
    )
    images = json.loads(output)
    assert changed in images
    assert set(images) == {
        ref
        for section in ("images", "build_bases", "ci_tools")
        for ref in lock[section].values()
    }
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci-image-mirror.yml").read_text())[
        "jobs"
    ]
    assert jobs["mirror"]["needs"] == "inputs"
    assert (
        "fromJSON(needs.inputs.outputs.images)"
        in jobs["mirror"]["strategy"]["matrix"]["image"]
    )
    assert "scripts/ci-image-mirror-inputs" in jobs["inputs"]["steps"][-1]["run"]


@pytest.fixture
def registered_tree(tmp_path: Path) -> Path:
    import shutil

    from tools.single_source_values import tracked_files

    files = tracked_files(ROOT)
    for file in files:
        if file.endswith("/single-source-values.toml") or file in (
            "scripts/protocol_wheel.py",
            "scripts/image_policy.py",
            "agent_protocol/pyproject.toml",
            "agent_protocol/uv.lock",
            "deploy/compose/images.lock.json",
            "control/pyproject.toml",
            "control/uv.lock",
            ".dockerignore",
            "tests/test_single_source_values.py",
        ):
            destination = tmp_path / file
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / file, destination)
    return tmp_path


def fixture_files(root: Path) -> list[str]:
    return [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]


@pytest.mark.parametrize(
    "consumer",
    (
        "scripts/example",
        ".github/workflows/example.yml",
        "Dockerfile",
        "docs/example.md",
    ),
)
@pytest.mark.parametrize(
    "filename",
    (
        "vonk_agent_protocol-0.0.1-py3-none-any.whl",
        "vonk_agent_protocol-999.0.0-py3-none-any.whl",
    ),
)
def test_guard_rejects_old_and_future_wheel_filenames(
    registered_tree: Path, consumer: str, filename: str
) -> None:
    # Wrong implementation: matching only the current owner version misses future drift.
    tmp_path = registered_tree
    path = tmp_path / consumer
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f'WHEEL="{filename}"\n'
    path.write_text(f"```sh\n{text}```\n" if consumer.endswith(".md") else text)
    copied = [
        p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()
    ]
    errors = violations(tmp_path, copied)
    assert any(
        f"{consumer}:" in error and "agent_protocol.wheel" in error for error in errors
    ), errors


@pytest.mark.parametrize(
    "fault", ("owner_bump", "stale_projection", "extra_copy", "copied_projection")
)
def test_projections_are_reproduced_and_exempt_only_exact_sites(
    registered_tree: Path, fault: str
) -> None:
    import tomllib

    from scripts.protocol_wheel import protocol_wheel_name
    from tools.single_source_values import project_protocol_lock

    root = registered_tree
    owner = root / "agent_protocol/pyproject.toml"
    projection = root / "control/uv.lock"
    version = tomllib.loads(owner.read_text())["project"]["version"]
    if fault == "owner_bump":
        owner.write_text(owner.read_text().replace(version, "99.2.7"))
    elif fault == "stale_projection":
        projection.write_text(
            projection.read_text().replace(
                protocol_wheel_name(root),
                protocol_wheel_name(root).replace(version, "99.2.7"),
            )
        )
    elif fault == "extra_copy":
        projection.write_text(
            projection.read_text()
            + f"\n# copied filename: {protocol_wheel_name(root)}\n"
        )
    else:
        pin = next(
            line
            for line in projection.read_text().splitlines()
            if protocol_wheel_name(root) in line
        )
        projection.write_text(projection.read_text() + "\n" + pin + "\n")
    errors = violations(root, fixture_files(root))
    assert any(
        "agent_protocol.wheel" in error and "control/uv.lock:" in error
        for error in errors
    ), errors
    if fault in {"owner_bump", "stale_projection"}:
        for file in ("control/uv.lock", "agent_protocol/uv.lock"):
            path = root / file
            path.write_text(project_protocol_lock(root, file, path.read_text()))
        assert violations(root, fixture_files(root)) == []


def test_unrelated_domains_are_not_floating_tag_policy(registered_tree: Path) -> None:
    root = registered_tree
    (root / "unrelated.py").write_text(
        'release_channels = ("dev", "stable")\nnumber = 32\n'
    )
    assert violations(root, fixture_files(root)) == []


@pytest.mark.parametrize("field", ("max_count = 6", "baseline = 6", "debt = 6"))
def test_registry_refuses_debt_allowances(registered_tree: Path, field: str) -> None:
    from pydantic import ValidationError

    root = registered_tree
    registry = root / "scripts/single-source-values.toml"
    registry.write_text(
        registry.read_text().split("[[values.exceptions]]")[0] + field + "\n"
    )
    with pytest.raises(ValidationError, match="Extra inputs"):
        violations(root, fixture_files(root))


def test_registry_refuses_broad_fixture_exemptions(registered_tree: Path) -> None:
    root = registered_tree
    registry = root / "scripts/single-source-values.toml"
    registry.write_text(
        registry.read_text().replace(
            'function = "test_guard_rejects_copied_policy_in_executable_surfaces"',
            'function = "test_*"',
        )
    )
    with pytest.raises(ValueError, match="exact adversarial test"):
        violations(root, fixture_files(root))


def test_all_image_policy_consumers_follow_owner_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Wrong implementation: a packager/verifier retains its own copy when policy changes.
    import importlib
    import importlib.machinery
    import importlib.util
    import json

    from tests.scripts.test_verify_supply_chain import _copy

    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    policy = importlib.import_module("image_policy")
    monkeypatch.setattr(policy, "FLOATING_TAGS", policy.FLOATING_TAGS | {"canary"})
    modules = []
    for script in (
        "render-dev-compose",
        "build-nas-compose-bundle",
        "verify-supply-chain",
    ):
        loader = importlib.machinery.SourceFileLoader(
            script, str(ROOT / "scripts" / script)
        )
        spec = importlib.util.spec_from_loader(script, loader)
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        modules.append(module)
    renderer, bundle, verifier = modules
    with pytest.raises(ValueError):
        renderer._validate("API", "example/api:canary", "pinned")
    assert not bundle._version_pinned("example/api:canary")
    repository = _copy(tmp_path)
    lock = repository / "deploy/compose/images.lock.json"
    document = json.loads(lock.read_text())
    document["images"]["tailscale"] = "example/tail:canary@sha256:" + "a" * 64
    lock.write_text(json.dumps(document))
    assert any(
        "tailscale image uses a floating tag" in error
        for error in verifier.image_errors(repository)
    )


def test_owner_file_cannot_define_another_copy(registered_tree: Path) -> None:
    root = registered_tree
    owner = root / "scripts/image_policy.py"
    source = owner.read_text()
    owner.write_text(source + source.replace("FLOATING_TAGS", "OTHER_POLICY"))
    assert any(
        "images.floating_tags" in error and "scripts/image_policy.py:" in error
        for error in violations(root, fixture_files(root))
    )


def _memory_admission_delegates(source: str) -> bool:
    import ast

    tree = ast.parse(source)
    sites = [
        (target.id, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
        and target.id in {"current_admission", "after_admission"}
    ]
    fits = dict(sites)
    owner_imported = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "vonk_forge_contracts.memory_admission"
        and any(
            alias.name == "evaluate_memory_admission" and alias.asname is None
            for alias in node.names
        )
        for node in tree.body
    )
    owner_shadowed = any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.name == "evaluate_memory_admission"
        for node in ast.walk(tree)
    )
    never_fits = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_never_fits_detail"
    )
    copied_floor_comparison = any(
        isinstance(node, ast.Compare)
        and any(
            isinstance(term, ast.Name) and term.id == "memory_floor_bytes"
            for term in ast.walk(node)
        )
        for node in ast.walk(never_fits)
    )
    return (
        owner_imported
        and not owner_shadowed
        and len(sites) == 2
        and not copied_floor_comparison
        and set(fits) == {"current_admission", "after_admission"}
        and all(
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "evaluate_memory_admission"
            for value in fits.values()
        )
    )


def test_memory_admission_rejects_reimplemented_fit_predicates() -> None:
    # Wrong implementation: a Controller-local predicate silently drifts from
    # declaration validation, even when today's arithmetic happens to agree.
    source = (
        ROOT / "control/src/vonk_control/resource_planning/capacity.py"
    ).read_text()
    assert _memory_admission_delegates(source)
    assert not _memory_admission_delegates(
        source + "\ncurrent_admission = available - occupied - total_bytes\n"
    )

    assert not _memory_admission_delegates(
        source + "\ndef evaluate_memory_admission(*args, **kwargs): return True\n"
    )


def uv_pin_violations(root: Path, files: list[str]) -> list[str]:
    """One owner, including stale and future literals, across executable inputs."""
    import re

    errors = []
    for file in files:
        if file == ".tool-versions":
            continue
        path = root / file
        try:
            text = path.read_text()
        except UnicodeError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if re.search(
                r"(?i)\buv(?:[_-]?version)?[\s`\"'=:@/~<>!-]*(?:==)?\d+\.\d+\.\d+", line
            ):
                errors.append(f"{file}:{number}: uv version belongs in .tool-versions")
        if file.endswith((".yml", ".yaml")) and "setup-uv@" in text:
            import yaml

            document = yaml.safe_load(text)
            groups = (
                [document["runs"]["steps"]]
                if "runs" in document
                else [job.get("steps", []) for job in document.get("jobs", {}).values()]
            )
            for steps in groups:
                for step in steps:
                    if "setup-uv@" not in step.get("uses", ""):
                        continue
                    options = step.get("with", {})
                    owner = options.get("version-file", "")
                    if (
                        "version" in options
                        or owner not in {".tool-versions", ".vonk-forge/.tool-versions"}
                        or not (root / owner.removeprefix(".vonk-forge/")).is_file()
                    ):
                        errors.append(f"{file}: setup-uv must read the version owner")
    return errors


def test_uv_has_one_version_owner() -> None:
    from tools.single_source_values import tracked_files

    files = tracked_files(ROOT)
    assert uv_pin_violations(ROOT, files) == []


@pytest.mark.parametrize(
    "consumer",
    ["Dockerfile", "docs/example.md", "scripts/install", ".github/workflows/test.yml"],
)
def test_uv_guard_rejects_stale_and_future_pins(tmp_path: Path, consumer: str) -> None:
    # Wrong implementation: checking only the current literal misses an older or future pin.
    for version in ("0." + "12.19", "99." + "1.2"):
        path = tmp_path / consumer
        path.parent.mkdir(parents=True, exist_ok=True)
        for pin in (
            f"uv=={version}",
            f"uv>={version}",
            f'UV_VERSION="{version}"',
            f"https://astral.sh/uv/{version}/install.sh",
        ):
            path.write_text(pin + "\n")
            assert uv_pin_violations(tmp_path, [consumer])


@pytest.mark.parametrize("options", ["version: latest", "enable-cache: true"])
def test_uv_guard_rejects_unowned_workflow_versions(
    tmp_path: Path, options: str
) -> None:
    # Wrong implementation: deleting the pin silently selects the action's floating default.
    consumer = ".github/workflows/test.yml"
    path = tmp_path / consumer
    path.parent.mkdir(parents=True)
    path.write_text(
        "jobs:\n  test:\n    steps:\n      - uses: astral-sh/setup-uv@"
        + "a" * 40
        + f"\n        with:\n          {options}\n"
    )
    assert uv_pin_violations(tmp_path, [consumer])
