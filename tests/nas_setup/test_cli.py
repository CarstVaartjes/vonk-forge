"""Exercise the actual private file/argv and unattended CLI boundaries."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest

from vonk_agent_protocol.installer_setup import NasInstallTemplate
from vonk_nas_setup.cli import open_private_answers
from vonk_nas_setup.filesystem import SetupError
from vonk_nas_setup.prompts import PromptIo


def invoke(
    template: NasInstallTemplate, output: Path, *flags: str
) -> subprocess.CompletedProcess[str]:
    document = output / "template.json"
    document.write_text(template.model_dump_json(exclude_none=True))
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "vonk_nas_setup.cli",
            "--template",
            str(document),
            "--output",
            str(output),
            *flags,
        ],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )


def test_private_answers_reject_links_and_public_modes(tmp_path: Path) -> None:
    """Catch unattended secret reads through shared/public input files."""
    answers = tmp_path / "answers"
    answers.write_text("private answer\n")
    answers.chmod(0o644)
    with pytest.raises(SetupError):
        open_private_answers(answers)
    answers.chmod(0o600)
    symlink = tmp_path / "alias"
    symlink.symlink_to(answers)
    with pytest.raises(SetupError):
        open_private_answers(symlink)
    hardlink = tmp_path / "shared"
    os.link(answers, hardlink)
    with pytest.raises(SetupError):
        open_private_answers(answers)
    hardlink.unlink()
    with open_private_answers(answers) as reader:
        assert reader.read() == "private answer\n"


def test_cli_upgrade_uses_no_terminal_and_preserves_private_answers(
    tmp_path: Path, template: NasInstallTemplate
) -> None:
    """Catch piping stdin being treated as secret input or upgrade requiring a TTY."""
    answers = tmp_path / "answers"
    answers.write_text("lab\n192.0.2.5\nprivate-hf-token\n")
    answers.chmod(0o600)
    fresh = invoke(template, tmp_path, "--answers-file", str(answers))
    assert fresh.returncode == 0, fresh.stderr
    assert "private-hf-token" not in fresh.stdout + fresh.stderr
    upgraded = invoke(template, tmp_path, "--upgrade")
    assert upgraded.returncode == 0, upgraded.stderr
    assert "Bundle ready" in upgraded.stdout
    assert (
        tmp_path / "vonk-forge/secrets/hf-token"
    ).read_text() == "private-hf-token\n"


def test_prompt_without_terminal_fails_as_setup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch a headless run surfacing a raw OSError instead of the typed failure."""

    def no_terminal(*_: object, **__: object) -> int:
        raise OSError(errno.ENXIO, "No such device or address")

    monkeypatch.setattr("vonk_nas_setup.prompts.os.open", no_terminal)
    with pytest.raises(SetupError):
        PromptIo().line("value")


def test_cli_rejects_secret_arguments_and_conflicting_flags(
    tmp_path: Path, template: NasInstallTemplate
) -> None:
    """Catch adding an argv secret surface or accepting an unknown option."""
    result = invoke(template, tmp_path, "--answers", "unsafe-value")
    assert result.returncode != 0
    assert not (tmp_path / "vonk-forge").exists()
    assert "unsafe-value" not in result.stdout + result.stderr
    result = invoke(template, tmp_path, "--enable-unknown")
    assert result.returncode != 0
    assert not (tmp_path / "vonk-forge").exists()
