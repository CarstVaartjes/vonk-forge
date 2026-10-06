"""Expired publication effects are observed before retrying the upload."""

import hashlib
import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_upload_timeout_adopts_exact_completed_object_without_reupload(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    loader = importlib.machinery.SourceFileLoader(
        "bounded_publication", str(ROOT / "scripts/install-release-publication")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    monkeypatch.setattr(module, "RCLONE_TRANSFER_TIMEOUT", 1)
    target = tmp_path / "published"
    pid = tmp_path / "upload-pid"
    executable = tmp_path / "rclone"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys, time\n"
        f"target = pathlib.Path({str(target)!r})\n"
        f"pid = pathlib.Path({str(pid)!r})\n"
        "if sys.argv[1] == 'lsjson':\n"
        "    print(json.dumps({'IsDir': False, 'Size': target.stat().st_size}) if target.exists() else 'null')\n"
        "elif sys.argv[1] == 'cat':\n"
        "    sys.stdout.buffer.write(target.read_bytes())\n"
        "else:\n"
        "    if pid.exists(): sys.exit(91)\n"
        "    pid.write_text(str(os.getpid()))\n"
        "    target.write_bytes(pathlib.Path(sys.argv[-2]).read_bytes())\n"
        "    time.sleep(3600)\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    source = tmp_path / "source"
    source.write_bytes(b"exact authorized bytes")
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    module._rclone_copy(
        source,
        "r2:authorized",
        {
            "key": "immutable/artifact",
            "phase": "immutable",
            "sha256": expected,
        },
    )
    assert target.read_bytes() == source.read_bytes()
    assert "checking the exact object before retrying" in capsys.readouterr().err
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)
