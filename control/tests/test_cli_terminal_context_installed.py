"""The installed CLI respects terminal width and keeps redirected JSON exact."""

from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import struct
import subprocess
import termios
import time
import unicodedata
from pathlib import Path

import pytest

from .test_profile_load_installed_cli import (
    _https_api_peer,
    _process_environment,
)
from .test_profile_load_submission import _profile_api

pytest_plugins = ("tests.test_profile_load_installed_cli",)

PROFILE_NAME = "Mía 東京 — long operational profile for 模型 recipes"


def _terminal_width(value: str) -> int:
    return sum(
        0
        if unicodedata.combining(char)
        else 2
        if unicodedata.east_asian_width(char) in {"W", "F"}
        else 1
        for char in value
    )


def _run_tty(
    executable: Path,
    environment: dict[str, str],
    cwd: Path,
    *,
    width: int,
) -> tuple[int, str]:
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, width, 0, 0))
    process = subprocess.Popen(
        [str(executable), "profile", "list"],
        stdin=subprocess.DEVNULL,
        stdout=slave,
        stderr=slave,
        env=environment,
        cwd=cwd,
    )
    os.close(slave)
    transcript = bytearray()
    deadline = time.monotonic() + 30
    try:
        while True:
            if time.monotonic() >= deadline:
                process.kill()
                raise TimeoutError("installed CLI did not finish its terminal read")
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b""
                if not chunk and process.poll() is not None:
                    break
                transcript.extend(chunk)
            elif process.poll() is not None:
                break
        status = process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        os.close(master)
    return status, transcript.decode("utf-8").replace("\r\n", "\n")


@pytest.mark.lane
@pytest.mark.parametrize("width", [60, 80, 120])
def test_installed_cli_profile_read_respects_terminal_and_redirected_contexts(
    installed_vonkctl: Path,
    postgres_engine,
    tmp_path: Path,
    width: int,
) -> None:
    _sessions, api, _codec, headers, _preview = _profile_api(postgres_engine)
    changed = api.put(
        "/api/profile/1",
        headers=headers,
        json={"name": PROFILE_NAME, "expected_revision": 1},
    )
    assert changed.status_code == 200, changed.text

    with _https_api_peer(tmp_path, api, headers) as (url, certificate, state):
        environment = {
            **_process_environment(tmp_path, url, certificate, headers),
            "TERM": "dumb",
            "NO_COLOR": "1",
            "COLUMNS": str(width),
            "PYTHONUTF8": "1",
        }
        status, terminal = _run_tty(
            installed_vonkctl, environment, tmp_path, width=width
        )
        assert status == 0
        assert "\x1b" not in terminal
        assert PROFILE_NAME in terminal
        assert max(map(_terminal_width, terminal.splitlines())) <= width
        if width < 80:
            assert "PROFILE: 1" in terminal
            assert f"NAME: {PROFILE_NAME}" in terminal
        else:
            assert "PROFILE  NAME" in terminal

        redirected = subprocess.run(
            [str(installed_vonkctl), "--json", "profile", "list"],
            env=environment,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    assert redirected.returncode == 0, redirected.stderr
    assert redirected.stdout.endswith("\n")
    assert redirected.stdout.count("\n") == 1
    assert redirected.stderr == ""
    document = json.loads(redirected.stdout)
    profiles = document.get("profiles")
    assert isinstance(profiles, list) and profiles
    assert profiles[0]["name"] == PROFILE_NAME
    assert "\x1b" not in redirected.stdout
    assert [(method, path) for method, path, _ in state.calls] == [
        ("GET", "/api/profile"),
        ("GET", "/api/profile"),
    ]
