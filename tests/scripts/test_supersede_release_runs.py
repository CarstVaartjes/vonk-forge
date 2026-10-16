"""A newer main release cancels only obsolete, uncommitted, non-oldest releases."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/supersede-release-runs"
APT = "Publish accepted ARM64 Spark agent package to apt dev"
AUTHORITY = "Accept one complete source generation"


def _fake_gh(tmp_path: Path, runs: list[dict], jobs: dict, ancestors: set, fail=False):
    """A ``gh`` that answers run, job and compare reads and records cancels."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = {"runs": runs, "jobs": jobs, "ancestors": sorted(ancestors), "fail": fail}
    (tmp_path / "state.json").write_text(json.dumps(state))
    script = bin_dir / "gh"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"root = {str(tmp_path)!r}\n"
        "state = json.load(open(root + '/state.json'))\n"
        "args = sys.argv[1:]\n"
        "if state['fail']:\n"
        "    sys.exit(1)\n"
        "if '--method' in args:\n"
        "    open(root + '/cancelled.txt', 'a').write(args[-1] + '\\n')\n"
        "    sys.exit(0)\n"
        "endpoint = args[-1]\n"
        "if '/workflows/' in endpoint:\n"
        "    print(json.dumps({'workflow_runs': state['runs']})); sys.exit(0)\n"
        "if '/compare/' in endpoint:\n"
        "    older = endpoint.rsplit('/', 1)[1].split('...')[0]\n"
        "    ahead = older in state['ancestors']\n"
        "    print(json.dumps({'status': 'ahead' if ahead else 'diverged'}))\n"
        "    sys.exit(0)\n"
        "if '/jobs' in endpoint:\n"
        "    run_id = endpoint.split('/runs/')[1].split('/')[0]\n"
        "    print('\\n'.join(state['jobs'].get(run_id, []))); sys.exit(0)\n"
        "sys.exit(64)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def _run(identifier: int, number: int, sha: str, **fields) -> dict:
    return {
        "id": identifier,
        "run_number": number,
        "head_sha": sha,
        "head_branch": "main",
        "event": "push",
        "status": "in_progress",
        **fields,
    }


def _newer_release(
    tmp_path: Path, bin_dir: Path
) -> tuple[subprocess.CompletedProcess, list]:
    result = subprocess.run(
        [str(SCRIPT), "--repository", "o/r", "--run-number", "20", "--sha", "new"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )
    cancelled = tmp_path / "cancelled.txt"
    return result, cancelled.read_text().splitlines() if cancelled.exists() else []


def test_only_obsolete_uncommitted_releases_behind_the_oldest_are_cancelled(
    tmp_path,
) -> None:
    """Catches cancelling the oldest release (starvation), a release that is
    publishing, one whose source the new release does not contain, a tag or
    completed run, or a newer release."""
    runs = [
        _run(1, 10, "oldest"),
        _run(2, 11, "obsolete", status="queued"),
        _run(3, 12, "apt"),
        _run(4, 13, "accepted"),
        _run(5, 14, "other-line"),
        _run(6, 15, "dispatched", event="workflow_dispatch"),
        _run(7, 16, "done", status="completed"),
        _run(8, 17, "tag", head_branch="v1.2.3"),
        _run(9, 21, "newer"),
    ]
    jobs = {"3": ["Agent package", APT], "4": [AUTHORITY], "2": ["CI / Ruff"]}
    ancestors = {"oldest", "obsolete", "apt", "accepted", "dispatched", "done", "tag"}
    bin_dir = _fake_gh(tmp_path, runs, jobs, ancestors)

    result, cancelled = _newer_release(tmp_path, bin_dir)

    assert result.returncode == 0, result.stderr
    assert cancelled == [
        "/repos/o/r/actions/runs/2/cancel",
        "/repos/o/r/actions/runs/6/cancel",
    ]


def test_a_single_older_release_always_finishes(tmp_path) -> None:
    bin_dir = _fake_gh(tmp_path, [_run(1, 19, "older")], {}, {"older"})

    result, cancelled = _newer_release(tmp_path, bin_dir)

    assert result.returncode == 0, result.stderr
    assert cancelled == []


def test_an_unavailable_github_api_never_blocks_the_release(tmp_path) -> None:
    bin_dir = _fake_gh(tmp_path, [], {}, set(), fail=True)

    result, cancelled = _newer_release(tmp_path, bin_dir)

    assert result.returncode == 0
    assert "::warning::" in result.stdout
    assert cancelled == []
