from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "a" * 40


def _fixture(tmp_path: Path) -> tuple[list[str], dict[str, str], Path]:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("promote-accepted-channel", "promote-image-aliases"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    binary = tmp_path / "bin"
    binary.mkdir()
    log = tmp_path / "log"
    # These stand-ins record boundaries; image copy/rollback has its own tests.
    helpers = {
        scripts / "install-release-publication": """#!/usr/bin/env bash
if [[ " $* " == *" --preflight-only "* ]]; then
  echo preflight >> "$LOG"
  [[ ${FAIL_PREFLIGHT:-0} != 1 ]]
else
  echo pointer >> "$LOG"
fi
""",
        scripts / "verify-production-alias-postconditions": """#!/usr/bin/env bash
echo stable-authority >> "$LOG"
[[ ${FAIL_AUTHORITY:-0} != 1 ]]
""",
        scripts / "promote-image-aliases": """#!/usr/bin/env bash
echo "aliases:$5" >> "$LOG"
[[ $5 == "$EXPECTED_ALIAS" ]]
shift 9
[[ $1 == --commit ]]
shift
"$@"
""",
        binary / "git": """#!/usr/bin/env bash
if [[ $1 == rev-parse ]]; then echo "$CURRENT_SHA"; fi
""",
    }
    for path, content in helpers.items():
        path.write_text(content)
        path.chmod(0o755)
    bundle = tmp_path / "bundle"
    generation = "b" * 64
    release = bundle / "objects/artifacts/dev/releases" / generation / "release.json"
    release.parent.mkdir(parents=True)
    release.write_text(
        json.dumps(
            {
                "images": {
                    role: f"ghcr.io/carstvaartjes/vonk-forge-{role}:dev-sha-{SOURCE}@sha256:{'c' * 64}"
                    for role in ("api", "worker", "hermes", "litellm")
                }
            }
        )
    )
    (bundle / "publication-plan.json").write_text(
        json.dumps(
            {
                "channel": "dev",
                "source_sha": SOURCE,
                "generation": generation,
            }
        )
    )
    command = [
        str(scripts / "promote-accepted-channel"),
        "run",
        str(bundle),
        str(tmp_path / "acceptance"),
        "d" * 64,
        "r2:bucket",
        "dev",
        "refs/heads/main",
        SOURCE,
        "",
    ]
    environment = {
        **os.environ,
        "PATH": f"{binary}:{os.environ['PATH']}",
        "LOG": str(log),
        "CURRENT_SHA": SOURCE,
        "EXPECTED_ALIAS": "dev",
    }
    return command, environment, log


def test_dev_aliases_advance_after_preflight_and_before_pointer(tmp_path: Path) -> None:
    command, environment, log = _fixture(tmp_path)
    result = subprocess.run(
        command,
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == ["preflight", "aliases:dev", "pointer"]


@pytest.mark.parametrize("failure", ["preflight", "stale_source"])
def test_invalid_candidate_never_advances_aliases(tmp_path: Path, failure: str) -> None:
    command, environment, log = _fixture(tmp_path)
    environment.update(
        {"FAIL_PREFLIGHT": "1"}
        if failure == "preflight"
        else {
            "CURRENT_SHA": "f" * 40,
        }
    )
    result = subprocess.run(
        command,
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert log.read_text().splitlines() == ["preflight"]


def test_production_uses_latest_and_checks_release_authority_twice(
    tmp_path: Path,
) -> None:
    command, environment, log = _fixture(tmp_path)
    bundle = Path(command[2])
    plan_path = bundle / "publication-plan.json"
    plan = json.loads(plan_path.read_text())
    plan["channel"] = "stable"
    plan_path.write_text(json.dumps(plan))
    (bundle / "objects/artifacts/dev").rename(bundle / "objects/artifacts/stable")
    command[6:8] = ["stable", "refs/tags/v1.2.3"]
    command[9] = "e" * 40
    environment["EXPECTED_ALIAS"] = "latest"
    result = subprocess.run(
        command,
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == [
        "preflight",
        "stable-authority",
        "aliases:latest",
        "stable-authority",
        "pointer",
    ]
