from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "restore-pytest-file-durations"


def _module():
    loader = importlib.machinery.SourceFileLoader("restore_durations", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_restore_merges_latest_matching_artifacts_and_skips_current_run(tmp_path, monkeypatch):
    module = _module()
    now = datetime.now(UTC)
    calls = []

    def fake_gh(*args):
        calls.append(args)
        if args[0] == "run":
            return [{"databaseId": 9}, {"databaseId": 8}]
        return {"artifacts": [{"name": "pytest-file-durations-repository-1"}]}

    def fake_run(args, **kwargs):
        destination = Path(args[args.index("--dir") + 1])
        destination.mkdir(parents=True)
        (destination / "durations.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": now.isoformat().replace("+00:00", "Z"),
                    "files": {"tests/a.py": 2.0, "tests/b.py": 3.0},
                }
            )
        )
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(module, "_gh", fake_gh)
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    output = tmp_path / "out.json"
    assert module.restore("repository", output, repo="o/r", current_run_id="9", now=now)
    assert json.loads(output.read_text())["files"] == {
        "tests/a.py": 2.0,
        "tests/b.py": 3.0,
    }
    assert any("8" in " ".join(call) for call in calls if call and call[0] == "api")


def test_restore_fails_open_on_unsupported_or_stale_input(tmp_path):
    module = _module()
    now = datetime.now(UTC)
    assert module._valid_files(
        {
            "schema_version": 99,
            "generated_at": now.isoformat(),
            "files": {"tests/a.py": 1},
        },
        now=now,
    ) == {}
    assert module._valid_files(
        {
            "schema_version": 1,
            "generated_at": (now - timedelta(days=31)).isoformat(),
            "files": {"tests/a.py": 1, "tests/b.py": "nan", "tests/c.py": "inf"},
        },
        now=now,
    ) == {}

    output = tmp_path / "empty.json"
    # A failed GitHub query still leaves a valid empty document for the selector.
    module._gh = lambda *args: (_ for _ in ()).throw(
        subprocess.CalledProcessError(1, args)
    )
    assert not module.restore("control", output, repo="o/r", now=now)
    assert json.loads(output.read_text())["files"] == {}


def test_restore_cli_uses_supported_gh_commands_and_system_tempdir(tmp_path: Path):
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        """#!/usr/bin/env python3
import json, pathlib, sys
args = sys.argv[1:]
if args[0] == 'api':
    if '--repo' in args:
        raise SystemExit(3)
    print(json.dumps({'artifacts': [{'name': 'pytest-file-durations-control-1'}]}))
elif args[0:2] == ['run', 'list']:
    print('[{"databaseId": "41"}]')
elif args[0:3] == ['run', 'download', '--repo']:
    directory = pathlib.Path(args[args.index('--dir') + 1])
    directory.mkdir(parents=True)
    (directory / 'timings.json').write_text(json.dumps({
        'schema_version': 1,
        'generated_at': __NOW__,
        'files': {'tests/example.py': 1.5},
    }))
else:
    raise SystemExit(4)
""".replace("__NOW__", repr(now))
    )
    fake_gh.chmod(0o755)
    output = tmp_path / "restored.json"
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--suite",
            "control",
            "--repo",
            "owner/repo",
            "--current-run-id",
            "99",
            "--output",
            str(output),
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert json.loads(output.read_text())["files"] == {"tests/example.py": 1.5}
