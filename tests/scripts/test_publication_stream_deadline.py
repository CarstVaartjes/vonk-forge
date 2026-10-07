"""Real transfer subprocesses cannot pin publication after producing partial bytes."""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path

import pytest


def _publication(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "scripts"))
    loader = importlib.machinery.SourceFileLoader(
        "publication_stream", str(root / "scripts/install-release-publication")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.mark.timeout(3, method="signal")
@pytest.mark.parametrize("reader", ["digest", "bytes"])
def test_partial_silent_transfer_is_reaped_and_fresh_read_recovers(
    tmp_path, monkeypatch, reader
):
    publication = _publication(monkeypatch)
    payload = tmp_path / "accepted-remote-object"
    pidfile = tmp_path / "transfer.pid"
    child = tmp_path / "transfer.py"
    child.write_text(
        "import os,pathlib,sys,time\n"
        "pathlib.Path(sys.argv[2]).write_text(str(os.getpid()))\n"
        "payload=pathlib.Path(sys.argv[1])\n"
        "if payload.exists():\n"
        "    sys.stdout.buffer.write(payload.read_bytes())\n"
        "else:\n"
        "    sys.stdout.buffer.write(b'partial observation')\n"
        "    sys.stdout.buffer.flush()\n"
        "    time.sleep(60)\n"
    )
    transfer_timeout = publication.RCLONE_TRANSFER_TIMEOUT
    monkeypatch.setattr(publication, "RCLONE_TRANSFER_TIMEOUT", 0.15)
    popen = publication.subprocess.Popen
    children = []

    def spawn(*args, **kwargs):
        assert args[0][0] == "rclone"
        args = ([sys.executable, str(child), str(payload), str(pidfile)], *args[1:])
        process = popen(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(publication.subprocess, "Popen", spawn)
    monkeypatch.setattr(publication, "_rclone_object_exists", lambda *_: True)

    def read():
        if reader == "digest":
            return publication._rclone_digest("remote", "object")
        return publication._rclone_bytes("remote", "object", 128)

    try:
        with pytest.raises(publication.PublicationError, match="incomplete remote"):
            read()
        pid = int(pidfile.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        # The transfer fault has cleared. A fresh Python process may need more
        # than the injected stall budget merely to start on a busy CI worker.
        monkeypatch.setattr(publication, "RCLONE_TRANSFER_TIMEOUT", transfer_timeout)
        complete = b"the complete accepted remote observation"
        payload.write_bytes(complete)
        assert read() == (
            hashlib.sha256(complete).hexdigest() if reader == "digest" else complete
        )
        if reader == "bytes":
            payload.write_bytes(b"x" * 129)
            with pytest.raises(
                publication.PublicationError, match="manifest is invalid"
            ):
                read()
            with pytest.raises(ProcessLookupError):
                os.kill(int(pidfile.read_text()), 0)
    finally:
        # A broken implementation must not leave the counterexample child running.
        for process in children:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
