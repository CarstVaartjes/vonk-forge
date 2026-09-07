from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/promote-image-aliases"
ROLES = ("api", "worker", "hermes", "litellm")
IMAGES = tuple(f"ghcr.io/carstvaartjes/vonk-forge-{role}" for role in ROLES)
DIGESTS = tuple(f"sha256:{letter * 64}" for letter in "abcd")
OLD = tuple(f"sha256:{letter * 64}" for letter in "1234")


def _setup(tmp_path: Path, *, missing: bool = False):
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps({role: ("" if missing else OLD[i]) for i, role in enumerate(ROLES)})
    )
    log = tmp_path / "log"
    log.touch()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "skopeo").write_text(
        """#!/usr/bin/env python3
import json, os, pathlib, sys
state = pathlib.Path(os.environ['STATE']); log = pathlib.Path(os.environ['LOG'])
args = sys.argv[1:]
log.open('a').write('skopeo ' + ' '.join(args) + '\\n')
data = json.loads(state.read_text())
def role(value): return value.split('vonk-forge-', 1)[1].split('@', 1)[0].split(':', 1)[0]
if args[0] == 'inspect':
    name = role(args[-1])
    if not data[name]: print('manifest unknown', file=sys.stderr); raise SystemExit(1)
    print(data[name])
elif args[0] == 'copy':
    name = role(args[-2])
    if name == os.environ.get('COPY_FAILURE'): raise SystemExit(1)
    data[name] = args[-2].split('@', 1)[1]; state.write_text(json.dumps(data))
else: raise SystemExit(2)
"""
    )
    (fake_bin / "skopeo").chmod(0o755)
    (fake_bin / "commit-hook").write_text(
        '#!/bin/sh\necho commit >> "$LOG"\ntest "${COMMIT_FAILURE:-}" != 1\n'
    )
    (fake_bin / "commit-hook").chmod(0o755)
    return state, log, fake_bin


def _run(tmp_path: Path, *extra: str, missing: bool = False, copy_failure: str = ""):
    state, log, fake_bin = _setup(tmp_path, missing=missing)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "STATE": str(state),
        "LOG": str(log),
        "COPY_FAILURE": copy_failure,
    }
    base = [
        IMAGES[0],
        DIGESTS[0],
        IMAGES[1],
        DIGESTS[1],
        "dev",
        IMAGES[2],
        DIGESTS[2],
        IMAGES[3],
        DIGESTS[3],
    ]
    result = subprocess.run(
        [str(SCRIPT), *base, *extra],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, json.loads(state.read_text()), log.read_text().splitlines()


def test_commit_runs_after_all_four_copies(tmp_path: Path):
    result, state, log = _run(tmp_path, "--commit", "commit-hook")
    assert result.returncode == 0
    assert all(state.values()) and log[-1] == "commit"
    copies = [line for line in log if line.startswith("skopeo copy")]
    assert all(image in line for image, line in zip(IMAGES, copies))


def test_commit_failure_rolls_back_available_old_aliases(tmp_path: Path):
    state, log, fake_bin = _setup(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "STATE": str(state),
        "LOG": str(log),
        "COMMIT_FAILURE": "1",
    }
    base = [
        IMAGES[0],
        DIGESTS[0],
        IMAGES[1],
        DIGESTS[1],
        "dev",
        IMAGES[2],
        DIGESTS[2],
        IMAGES[3],
        DIGESTS[3],
        "--commit",
        "commit-hook",
    ]
    result = subprocess.run(
        [str(SCRIPT), *base], env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode != 0 and json.loads(state.read_text()) == dict(
        zip(ROLES, OLD)
    )


def test_missing_first_publication_does_not_run_commit_before_copies(tmp_path: Path):
    result, _, log = _run(tmp_path, "--commit", "commit-hook", missing=True)
    assert result.returncode == 0
    assert log[-1] == "commit"


def test_copy_failure_does_not_run_commit(tmp_path: Path):
    result, _, log = _run(tmp_path, "--commit", "commit-hook", copy_failure="worker")
    assert result.returncode != 0 and "commit" not in log
