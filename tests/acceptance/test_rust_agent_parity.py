"""Release-gate map for the recipe-native GPU node agent."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _rust_claim_capabilities() -> tuple[str, ...]:
    """Read the capabilities sent in every Rust agent claim.

    Keep this a source-level check: the release gate runs before a Rust binary
    is necessarily available, and compiling a second capability list in the
    test would only create another place for the contract to drift.
    """
    source = _source("rust/crates/vonk-agent/src/main.rs")
    match = re.search(
        r"let capabilities = \[(?P<body>.*?)\];",
        source,
        flags=re.DOTALL,
    )
    assert match is not None, "Rust agent claim capability list is missing"
    return tuple(re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', match.group("body")))


def _controller_known_capabilities() -> frozenset[str]:
    """Evaluate the small, literal capability set in ``agent_jobs.py``.

    Importing the Controller here would make this package-level release gate
    depend on SQLAlchemy and its database drivers.  A deliberately narrow AST
    evaluator keeps the test dependency-free while still following the source
    declarations (including ``AgentOperation.*.value`` references).
    """
    protocol = ast.parse(_source("agent_protocol/src/vonk_agent_protocol/contracts.py"))
    operation_values = {
        item.targets[0].id: item.value.value
        for node in protocol.body
        if isinstance(node, ast.ClassDef) and node.name == "AgentOperation"
        for item in node.body
        if isinstance(item, ast.Assign)
        and len(item.targets) == 1
        and isinstance(item.targets[0], ast.Name)
        and isinstance(item.value, ast.Constant)
        and isinstance(item.value.value, str)
    }
    source = ast.parse(_source("control/src/vonk_control/agent_jobs.py"))
    assignments: dict[str, ast.AST] = {}
    for node in source.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = node.value

    def evaluate(node: ast.AST, seen: frozenset[str] = frozenset()) -> set[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.Name):
            assert node.id not in seen, f"cyclic capability declaration: {node.id}"
            assert node.id in assignments, f"unknown capability declaration: {node.id}"
            return evaluate(assignments[node.id], seen | {node.id})
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "value"
            and isinstance(node.value, ast.Attribute)
            and node.value.attr in operation_values
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "AgentOperation"
        ):
            return {operation_values[node.value.attr]}
        if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
            values: set[str] = set()
            for element in node.elts:
                values.update(evaluate(element, seen))
            return values
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"frozenset", "set"}
            and len(node.args) == 1
        ):
            return evaluate(node.args[0], seen)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return evaluate(node.left, seen) | evaluate(node.right, seen)
        raise AssertionError(f"unsupported capability declaration: {ast.dump(node)}")

    return frozenset(evaluate(assignments["_KNOWN_CAPABILITIES"]))


def test_rust_claim_capabilities_have_a_safe_controller_intersection() -> None:
    """Ensure the required runtime contract exists on both sides.

    The Controller deliberately negotiates the intersection: a newer agent may
    advertise capabilities an older Controller does not know yet, and those
    optional capabilities are ignored for that session.  Only the required
    runtime capabilities must overlap; Controller-only orchestration remains
    an intentional boundary.
    """
    advertised = _rust_claim_capabilities()
    known = _controller_known_capabilities()
    assert advertised, "Rust agent must advertise at least one capability"
    required_overlap = {
        "agent.runtime.rust.v1",
        "runtime.vonk.v1",
    }
    assert required_overlap <= set(advertised) & known
    assert known - set(advertised) == {
        "node.probe",
        "release.install",
        "workload.health",
        "workload.prepare",
        "workload.start",
        "workload.stop",
        "workload.verify",
    }


def test_production_rust_capabilities_are_exact_and_python_agent_is_not_packaged() -> (
    None
):
    main = _source("rust/crates/vonk-agent/src/main.rs")
    for capability in (
        "agent.runtime.rust.v1",
        "runtime.vonk.v1",
        "recipe.install",
        "recipe.start",
        "recipe.stop",
        "recipe.uninstall",
        "recipe.model-uninstall.v1",
    ):
        assert f'"{capability}"' in main
    assert '"package.prepare"' not in main

    package_builder = _source("scripts/build-agent-deb")
    assert 'EGRESS_BINARY = "vonk-build-egress"' in package_builder
    assert "vonk_agent" not in package_builder


def test_debian_package_is_the_only_agent_installer_authority() -> None:
    package_builder = _source("scripts/build-agent-deb")
    for binary in ("vonk-agent", "vonk-agent-helper", "vonk-build-egress", "oras"):
        assert binary in package_builder
    for unit in (
        "vonk-forge-agent.service",
        "vonk-forge-docker-firewall.service",
        "vonk-forge-package-helper.service",
        "vonk-forge-package-helper.socket",
    ):
        assert (ROOT / "packaging/systemd" / unit).is_file()

    process = _source("rust/crates/vonk-agent/src/process.rs")
    assert 'Self::Oras => "/usr/lib/vonk-forge/oras"' in process


def test_release_workflow_runs_every_agent_owner_before_publication() -> None:
    orchestrator = _source(".github/workflows/agent-release.yml")
    apt_publisher = _source(".github/workflows/agent-apt-development.yml")
    package_builder = _source(".github/actions/agent-package-build/action.yml")
    package_security = _source(".github/actions/agent-package-security/action.yml")

    assert "uses: ./.github/actions/agent-package-build" in orchestrator
    assert "needs: [package-metadata, build-test-sign]" in orchestrator
    assert "uses: ./.github/actions/agent-apt-publish" not in orchestrator
    assert "workflows: [Rust Vonk Forge agent development]" in apt_publisher
    assert "uses: ./.github/actions/agent-apt-publish" in apt_publisher
    for gate in (
        "Build, sign, and lifecycle-test ARM64 Spark agent",
        "Run Rust and package security gates",
        "Lifecycle-test accepted ARM64 package on ARM64",
    ):
        assert f"'{gate}'" in apt_publisher
    assert "uses: ./.github/actions/agent-package-security" in package_builder
    assert "uses: ./.github/actions/agent-package-security" in orchestrator
    assert "cargo test --workspace --locked" in package_security
    assert "tests/acceptance/test_rust_agent_parity.py" in package_security
    assert "test_agent_deb.py" in package_security
