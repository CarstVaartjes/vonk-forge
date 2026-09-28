from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/build-control-wheel"
WHEEL_NAME = "vonk_agent_protocol-3.0.0-py3-none-any.whl"
# A command that runs in the control environment: uv against the control
# project, or scripts/test, which always does.
CONTROL_UV_COMMAND = re.compile(
    r"uv\s+(?:sync|run).*--project\s+(?:control|\.vonk-forge/control)|\bscripts/test\s"
)


def _repository(tmp_path: Path, *, wheel_bytes: bytes) -> tuple[Path, Path]:
    root = tmp_path / "repository"
    (root / "control").mkdir(parents=True)
    (root / "agent_protocol").mkdir()
    wheel_path = root / "inventory/wheels" / WHEEL_NAME
    wheel_path.parent.mkdir(parents=True)
    wheel_path.write_bytes(wheel_bytes)
    expected_hash = hashlib.sha256(wheel_bytes).hexdigest()
    (root / "control/uv.lock").write_text(
        "[[package]]\n"
        'name = "vonk-agent-protocol"\n'
        "wheels = [\n"
        f'  {{ filename = "{WHEEL_NAME}", hash = "sha256:{expected_hash}" }},\n'
        "]\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "assert sys.argv[1:6] == ['build', '--project', 'agent_protocol', '--wheel', '--out-dir']\n"
        "output = pathlib.Path(sys.argv[6])\n"
        "output.mkdir(parents=True, exist_ok=True)\n"
        f"(output / '{WHEEL_NAME}').write_bytes({wheel_bytes!r})\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    return root, fake_bin


def _run(root: Path, fake_bin: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )


def _collect_steps(value: object, ordered_steps: list[dict[str, object]]) -> None:
    if isinstance(value, list):
        for item in value:
            _collect_steps(item, ordered_steps)
    elif isinstance(value, dict):
        if "run" in value or "uses" in value:
            ordered_steps.append(value)
        if "parallel" in value:
            _collect_steps(value["parallel"], ordered_steps)


def _is_control_uv_step(step: dict[str, object]) -> bool:
    run = step.get("run")
    return isinstance(run, str) and CONTROL_UV_COMMAND.search(run) is not None


_PREPARING_ACTIONS = {
    "./.github/actions/prepare-control-wheel",
    "./.github/actions/python-env",
    "./.vonk-forge/.github/actions/prepare-control-wheel",
}


def _prepares_protocol_wheel(step: dict[str, object], same_step: bool) -> bool:
    """The step builds the wheel, before its own control command if it has one."""

    if step.get("uses") in _PREPARING_ACTIONS:
        return True
    run = step.get("run")
    if not isinstance(run, str) or "scripts/build-control-wheel" not in run:
        return False
    if not same_step:
        return True
    control = CONTROL_UV_COMMAND.search(run)
    return (
        control is not None
        and run.index("scripts/build-control-wheel") < control.start()
    )


def test_build_script_verifies_the_frozen_wheel_digest(tmp_path: Path) -> None:
    expected_bytes = b"reproducible protocol wheel"
    root, fake_bin = _repository(tmp_path, wheel_bytes=expected_bytes)
    (root / "inventory/wheels" / WHEEL_NAME).unlink()

    result = _run(root, fake_bin)

    assert result.returncode == 0, result.stderr
    assert (root / "inventory/wheels" / WHEEL_NAME).read_bytes() == expected_bytes
    assert (
        "Verified vonk_agent_protocol-3.0.0-py3-none-any.whl against control/uv.lock"
        in result.stdout
    )


def test_build_script_refuses_a_wheel_with_a_stale_lock_hash(tmp_path: Path) -> None:
    root, fake_bin = _repository(tmp_path, wheel_bytes=b"expected locked wheel")
    (root / "control/uv.lock").write_text(
        "[[package]]\n"
        'name = "vonk-agent-protocol"\n'
        "wheels = [\n"
        f'  {{ filename = "{WHEEL_NAME}", hash = "sha256:{"0" * 64}" }},\n'
        "]\n",
        encoding="utf-8",
    )

    result = _run(root, fake_bin)

    assert result.returncode != 0
    assert "refresh the locked wheel hash" in result.stderr


def test_every_control_workflow_builds_the_wheel_before_uv() -> None:
    import yaml

    workflow_paths = sorted((ROOT / ".github/workflows").glob("*.y*ml"))
    checked_jobs = 0
    for path in workflow_paths:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job in document.get("jobs", {}).values():
            ordered_steps: list[dict[str, object]] = []
            _collect_steps(job.get("steps", []), ordered_steps)
            if not any(_is_control_uv_step(step) for step in ordered_steps):
                continue
            checked_jobs += 1
            first_control_run = next(
                index
                for index, step in enumerate(ordered_steps)
                if _is_control_uv_step(step)
            )
            prepared = [
                index
                for index, step in enumerate(ordered_steps[: first_control_run + 1])
                if _prepares_protocol_wheel(step, index == first_control_run)
            ]
            assert prepared, (
                f"{path}: Controller command runs before the protocol wheel is prepared"
            )
    assert checked_jobs >= 8
