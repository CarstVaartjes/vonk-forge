"""Exercise the actual private file/argv and unattended CLI boundaries."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from vonk_agent_protocol.installer_setup import NasInstallTemplate
from vonk_nas_setup.cli import open_private_answers
from vonk_nas_setup.filesystem import SetupError


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
    answers.write_text("lab\n192.168.1.5\nprivate-hf-token\n")
    answers.chmod(0o600)
    fresh = invoke(template, tmp_path, "--answers-file", str(answers))
    assert fresh.returncode == 0, fresh.stderr
    assert "private-hf-token" not in fresh.stdout + fresh.stderr
    upgraded = invoke(template, tmp_path, "--upgrade", "--enable-hermes")
    assert upgraded.returncode == 0, upgraded.stderr
    assert "Bundle ready" in upgraded.stdout
    assert (
        tmp_path / "vonk-forge/secrets/hf-token"
    ).read_text() == "private-hf-token\n"


def test_cli_rejects_secret_arguments_and_conflicting_flags(
    tmp_path: Path, template: NasInstallTemplate
) -> None:
    """Catch adding an argv secret surface or accepting ambiguous Hermes intent."""
    result = invoke(template, tmp_path, "--answers", "unsafe-value")
    assert result.returncode != 0 and not (tmp_path / "vonk-forge").exists()
    result = invoke(template, tmp_path, "--enable-hermes", "--disable-hermes")
    assert result.returncode != 0 and not (tmp_path / "vonk-forge").exists()


def test_answers_file_permission_refusal_is_visible(
    tmp_path: Path, template: NasInstallTemplate
) -> None:
    """Catch buffering the OS group refusal away while claiming bundle readiness."""
    import tempfile

    from vonk_agent_protocol.installer_setup import NasGroupReadableSecrets

    template = template.model_copy(
        update={
            "group_readable_secrets": NasGroupReadableSecrets(
                gid=2**32 - 2, files=["hf-token"]
            )
        }
    )
    with tempfile.TemporaryDirectory(prefix="vonk-group-refusal-") as temporary:
        output = Path(temporary)
        output.chmod(0o777)
        document, answers = output / "template.json", output / "answers"
        document.write_text(template.model_dump_json(exclude_none=True))
        answers.write_text("lab\n192.168.1.5\nprivate-answer\n")
        answers.chmod(0o600)
        preexec: Callable[[], None] | None = None
        if os.geteuid() == 0:
            os.chown(answers, 65534, 65534)

            def drop_privileges() -> None:
                os.setgroups([])
                os.setgid(65534)
                os.setuid(65534)

            preexec = drop_privileges

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "vonk_nas_setup.cli",
                "--template",
                str(document),
                "--output",
                str(output),
                "--answers-file",
                str(answers),
            ],
            env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
            preexec_fn=preexec,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert "permission_denied" in result.stdout and "hf-token" in result.stdout
        assert "private-answer" not in result.stdout + result.stderr
        assert (output / "vonk-forge/secrets/hf-token").stat().st_mode & 0o077 == 0
