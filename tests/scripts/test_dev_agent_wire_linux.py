"""Keep the local Linux wire lane honest about mirroring the CI job.

``scripts/dev-agent-wire-linux`` runs the ``controller-spark-wire`` command, but
a developer convenience that silently runs a different toolchain or a
paraphrased command produces evidence nobody can trust. These tests fail when
the workflow and the lane drift apart, so a CI change that forgets the lane is
caught by the ordinary repository suite rather than discovered later.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "dev_agent_wire_linux.py"


def _module():
    loader = importlib.machinery.SourceFileLoader("dev_agent_wire_linux", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


def _lane_root(tmp_path: Path, module) -> Path:
    for relative in (module.CI_WORKFLOW, module.DOCKERFILE):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text((ROOT / relative).read_text(encoding="utf-8"), encoding="utf-8")
    return tmp_path


def _mutate(root: Path, relative: Path, old: str, new: str) -> None:
    path = root / relative
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{relative.as_posix()} no longer contains {old!r}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_local_lane_agrees_with_the_ci_wire_job() -> None:
    module = _module()

    assert module.check_lane_consistency(ROOT) == []


def test_local_lane_refuses_a_drifted_rust_toolchain(tmp_path: Path) -> None:
    module = _module()
    root = _lane_root(tmp_path, module)
    _mutate(
        root,
        module.CI_WORKFLOW,
        f"rustup toolchain install {module.RUST_TOOLCHAIN} --profile minimal",
        "rustup toolchain install 1.98.0 --profile minimal",
    )

    problems = module.check_lane_consistency(root)

    assert any("1.98.0" in problem and module.RUST_TOOLCHAIN in problem for problem in problems), (
        problems
    )


def test_local_lane_refuses_a_paraphrased_wire_command(tmp_path: Path) -> None:
    module = _module()
    root = _lane_root(tmp_path, module)
    _mutate(
        root,
        module.CI_WORKFLOW,
        "python scripts/tests/run_agent_wire_contracts.py -- -q",
        "python scripts/tests/run_agent_wire_contracts.py -- -q -x",
    )

    problems = module.check_lane_consistency(root)

    assert any("-q -x" in problem for problem in problems), problems


def test_local_lane_refuses_a_drifted_base_image(tmp_path: Path) -> None:
    module = _module()
    root = _lane_root(tmp_path, module)
    _mutate(
        root,
        module.DOCKERFILE,
        f"FROM {module.BASE_IMAGE}",
        f"FROM ubuntu:22.04@sha256:{'a' * 64}",
    )

    problems = module.check_lane_consistency(root)

    assert any("ubuntu:22.04" in problem for problem in problems), problems


def test_local_lane_refuses_a_base_image_pinned_by_tag_alone(tmp_path: Path) -> None:
    module = _module()
    root = _lane_root(tmp_path, module)
    _mutate(root, module.DOCKERFILE, f"FROM {module.BASE_IMAGE}", f"FROM {module.CI_RUNNER}")

    problems = module.check_lane_consistency(root)

    assert any(module.CI_RUNNER in problem for problem in problems), problems
