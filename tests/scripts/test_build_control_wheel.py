from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

from scripts.release.protocol_wheel import protocol_wheel_name

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/release/build-control-wheel"
WHEEL_NAME = protocol_wheel_name(ROOT)
_PREPARING_ACTIONS = {
    "./.github/actions/prepare-control-wheel",
    "./.vonk-forge/.github/actions/prepare-control-wheel",
}


def test_build_script_only_builds_the_protocol_wheel(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    (root / "agent_protocol").mkdir(parents=True)
    (root / "agent_protocol/pyproject.toml").write_bytes(
        (ROOT / "agent_protocol/pyproject.toml").read_bytes()
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
        f"(output / '{WHEEL_NAME}').write_bytes(b'wheel')\n",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)

    result = subprocess.run(
        [str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    assert result.returncode == 0, result.stderr
    assert (root / "inventory/wheels" / WHEEL_NAME).read_bytes() == b"wheel"


def test_every_job_that_builds_the_controller_image_builds_the_wheel_first() -> None:
    """The image copies the wheel from the context; nothing else builds it."""

    checked = set()
    for path in sorted((ROOT / ".github/workflows").glob("*.y*ml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for name, job in document.get("jobs", {}).items():
            steps = job.get("steps", [])
            if "control/Dockerfile" not in yaml.safe_dump(job):
                continue
            builds = [
                index
                for index, step in enumerate(steps)
                if str(step.get("uses", "")).startswith("docker/build-push-action")
            ]
            if not builds:
                continue
            checked.add(f"{path.name}:{name}")
            assert any(
                "scripts/release/build-control-wheel" in str(step.get("run", ""))
                or step.get("uses") in _PREPARING_ACTIONS
                for step in steps[: builds[0]]
            ), f"{path.name}:{name} builds the Controller image without the wheel"
    assert "ci.yml:nas-install" in checked
