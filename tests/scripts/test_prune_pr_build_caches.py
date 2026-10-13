"""The pull-request build-cache pruner deletes only what no open PR reads."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/prune-pr-build-caches"


def _module():
    loader = importlib.machinery.SourceFileLoader("prune_pr_build_caches", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _fake_gh(tmp_path: Path, versions: list[dict], states: dict[int, str]) -> Path:
    """A ``gh`` that answers the package and pull request reads and records deletes."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "versions.json").write_text(json.dumps(versions))
    (tmp_path / "states.json").write_text(
        json.dumps({str(k): v for k, v in states.items()})
    )
    script = bin_dir / "gh"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"root = {str(tmp_path)!r}\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['api', '--method'] and args[2] == 'DELETE':\n"
        "    open(root + '/deleted.txt', 'a').write(args[3] + '\\n')\n"
        "    sys.exit(0)\n"
        "endpoint = args[-1]\n"
        "if endpoint.endswith('/versions'):\n"
        "    print(open(root + '/versions.json').read()); sys.exit(0)\n"
        "if '/pulls/' in endpoint:\n"
        "    number = endpoint.rsplit('/', 1)[1]\n"
        "    states = json.load(open(root + '/states.json'))\n"
        "    print(json.dumps({'state': states[number]})); sys.exit(0)\n"
        "sys.exit(64)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def _version(identifier: int, *tags: str) -> dict:
    return {"id": identifier, "metadata": {"container": {"tags": list(tags)}}}


def test_only_closed_pull_request_caches_are_deleted(tmp_path, monkeypatch) -> None:
    """Catches a pruner that deletes an open pull request's cache, main's
    cache, or a version that also carries another tag."""
    bin_dir = _fake_gh(
        tmp_path,
        [
            _version(1, "buildcache"),
            _version(2, "buildcache-pr-10"),
            _version(3, "buildcache-pr-11"),
            _version(4, "buildcache-pr-12", "keep-me"),
            _version(5),
        ],
        {10: "closed", 11: "open", 12: "closed"},
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    module = _module()

    assert module.stale_versions("owner", "owner/repo") == [(2, 10)]

    result = subprocess.run(
        [str(SCRIPT), "--owner", "owner", "--repository", "owner/repo"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "deleted.txt").read_text().splitlines() == [
        "/users/owner/packages/container/vonk-forge-api/versions/2"
    ]
