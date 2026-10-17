"""Real publisher death/storage faults catch stale ownership and partial replay."""

from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from vonk_agent_protocol.installer_setup import NasInstallTemplate
from vonk_nas_setup import publication
from vonk_nas_setup.environment import parse_environment
from vonk_nas_setup.filesystem import ObservationUnavailable, atomic_replace
from vonk_nas_setup.prompts import PromptIo
from vonk_nas_setup.setup import prepare

CHILD = """
import os, sys, time
from pathlib import Path
from vonk_agent_protocol.installer_setup import NasInstallTemplate
from vonk_nas_setup import publication
from vonk_nas_setup.filesystem import acquire_owner
root, template_path, ready, mode = map(Path, sys.argv[1:])
payload = NasInstallTemplate.model_validate_json(template_path.read_bytes())
with acquire_owner(root):
    if str(mode) == "publish":
        original = publication.write_member
        def interrupted(target, name, content):
            original(target, name, content)
            ready.write_text(name)
            time.sleep(10)
        publication.write_member = interrupted
        publication.complete_replay(root / "vonk-forge")
    else:
        ready.write_text("owned")
        time.sleep(10)
"""


def wait_ready(path: Path, child: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 30
    while not path.exists():
        if child.poll() is not None:
            raise AssertionError(
                f"publisher exited early: {child.communicate()[1].decode()}"
            )
        if time.monotonic() >= deadline:
            raise AssertionError("publisher did not reach fault boundary")
        time.sleep(0.01)


def child_publisher(
    tmp_path: Path, template: NasInstallTemplate, mode: str
) -> tuple[subprocess.Popen[bytes], Path]:
    document = tmp_path / "template.json"
    document.write_text(template.model_dump_json(exclude_none=True))
    ready = tmp_path / "publisher-ready"
    process = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(tmp_path), str(document), str(ready), mode],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return process, ready


def test_process_death_mid_publication_replays_exact_bytes_and_releases_owner(
    installed: Path, template: NasInstallTemplate
) -> None:
    """Catch validating partial active groups before replay or rotating on restart."""
    environment = parse_environment(installed / ".env")
    members = publication.load_members(installed / "secrets", template)
    members["hf-token"] = b"candidate exact bytes\n"
    environment["VONK_HERMES_TIMEZONE"] = "Europe/Amsterdam"
    staging = publication.stage_intent(template, installed, members, environment)
    staging.rename(installed / publication.INTENT)
    child, ready = child_publisher(installed.parent, template, "publish")
    try:
        wait_ready(ready, child)
        child.send_signal(signal.SIGKILL)
        child.communicate(timeout=30)
        assert (installed / publication.INTENT).is_dir()
        prepare(template, installed.parent, PromptIo(io.StringIO("")), upgrade=True)
        assert publication.load_members(installed / "secrets", template) == members
        assert (
            parse_environment(installed / ".env")["VONK_HERMES_TIMEZONE"]
            == "Europe/Amsterdam"
        )
        assert not (installed / publication.INTENT).exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=30)


def test_busy_owner_returns_bounded_and_next_request_resumes(
    installed: Path, template: NasInstallTemplate
) -> None:
    """Catch stale busy bookkeeping surviving executor death or blocking forever."""
    child, ready = child_publisher(installed.parent, template, "lock")
    try:
        wait_ready(ready, child)
        with pytest.raises(BlockingIOError):
            prepare(template, installed.parent, PromptIo(io.StringIO("")), upgrade=True)
        # The owner still holds the lock: a blocking wait would never return.
        assert child.poll() is None
        child.send_signal(signal.SIGKILL)
        child.communicate(timeout=30)
        prepare(template, installed.parent, PromptIo(io.StringIO("")), upgrade=True)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=30)


def test_incomplete_intent_rolls_back_only_from_verified_group_and_fresh_attempt_recovers(
    installed: Path, template: NasInstallTemplate
) -> None:
    """Catch trusting incomplete intent or permanently poisoning the next request."""
    before = publication.load_members(installed / "secrets", template)
    environment = parse_environment(installed / ".env")
    candidate = dict(before)
    candidate["hf-token"] = b"partly exposed candidate\n"
    staging = publication.stage_intent(template, installed, candidate, environment)
    staging.rename(installed / publication.INTENT)
    atomic_replace(installed / "secrets/hf-token", candidate["hf-token"])
    (installed / publication.INTENT / "secrets/signing-key").unlink()
    prepare(template, installed.parent, PromptIo(io.StringIO("")), upgrade=True)
    assert publication.load_members(installed / "secrets", template) == before
    assert any(
        path.is_dir() for path in installed.glob(".vonk-forge.setup-*/retained-intent")
    )


def test_lost_storage_restores_exact_group_without_new_authority(
    installed: Path, template: NasInstallTemplate
) -> None:
    """Catch a historical success making lost active storage look complete."""
    before = publication.load_members(installed / "secrets", template)
    for member in (installed / "secrets").iterdir():
        if member.is_file():
            member.unlink()
    prepare(template, installed.parent, PromptIo(io.StringIO("")), upgrade=True)
    assert publication.load_members(installed / "secrets", template) == before


def test_failure_without_complete_verified_snapshot_preserves_damaged_intent(
    installed: Path, template: NasInstallTemplate
) -> None:
    """Catch incomplete rollback restoring an unverified credential subset."""
    before = publication.load_members(installed / "secrets", template)
    staging = publication.stage_intent(
        template, installed, before, parse_environment(installed / ".env")
    )
    staging.rename(installed / publication.INTENT)
    (installed / publication.INTENT / "secrets/signing-key").unlink()
    (installed / publication.VERIFIED / "signing-key").unlink()
    (installed / "secrets/signing-key").unlink()
    with pytest.raises(ObservationUnavailable):
        prepare(template, installed.parent, PromptIo(io.StringIO("")), upgrade=True)
    assert publication.load_members(installed / "secrets", template) == {
        name: value for name, value in before.items() if name != "signing-key"
    }
    assert any(
        path.is_dir() for path in installed.glob(".vonk-forge.setup-*/retained-intent")
    )


def test_verified_snapshot_survives_failed_generation_swap(
    installed: Path, template: NasInstallTemplate, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch deleting the last verified group in a failed publication's cleanup."""
    before = publication.load_members(installed / publication.VERIFIED, template)
    replace = publication.os.replace

    def fail_new_generation(source: Path, target: Path) -> None:
        if target == installed / publication.VERIFIED and source.name == "secrets":
            raise OSError("injected NAS rename fault")
        replace(source, target)

    monkeypatch.setattr(publication.os, "replace", fail_new_generation)
    with pytest.raises(OSError, match="injected NAS rename fault"):
        publication.remember_verified(template, installed)
    assert (
        publication.load_members(installed / publication.VERIFIED, template) == before
    )
    monkeypatch.undo()
    prepare(template, installed.parent, PromptIo(io.StringIO("")), upgrade=True)
    assert publication.load_members(installed / "secrets", template) == before


@pytest.mark.parametrize("damage", ["key", "environment", "url"])
def test_complete_poisoned_intent_cannot_overwrite_verified_authority(
    installed: Path, template: NasInstallTemplate, damage: str
) -> None:
    """Catch trusting journal completeness as proof of usable credentials/config."""
    before = publication.load_members(installed / "secrets", template)
    retained = publication.load_members(installed / publication.VERIFIED, template)
    environment = parse_environment(installed / ".env")
    candidate = dict(before)
    if damage == "key":
        candidate["signing-key"] = b"malformed private key\n"
    elif damage == "url":
        candidate["database-url"] = (
            b"postgresql+psycopg://vonk:wrong@postgres:5432/vonk\n"
        )
    else:
        environment["NAS_LAN_IP"] = "invalid-address"
    staging = publication.stage_intent(template, installed, candidate, environment)
    staging.rename(installed / publication.INTENT)
    prepare(template, installed.parent, PromptIo(io.StringIO("")), upgrade=True)
    assert publication.load_members(installed / "secrets", template) == before
    assert (
        publication.load_members(installed / publication.VERIFIED, template) == retained
    )


def test_interrupted_older_intent_replays_before_new_template_inputs(
    installed: Path, template: NasInstallTemplate
) -> None:
    """Catch applying a newer kit's secret inventory to an older complete journal."""
    from vonk_agent_protocol.installer_setup import NasSecretPrompt

    members = publication.load_members(installed / "secrets", template)
    environment = parse_environment(installed / ".env")
    members["hf-token"] = b"old accepted generation\n"
    staging = publication.stage_intent(template, installed, members, environment)
    staging.rename(installed / publication.INTENT)
    child, ready = child_publisher(installed.parent, template, "publish")
    try:
        wait_ready(ready, child)
        child.kill()
        child.communicate(timeout=30)
        newer = template.model_copy(
            update={
                "secrets": [
                    *template.secrets,
                    NasSecretPrompt(file="new-generation-input", prompt="New input"),
                ]
            }
        )
        prepare(
            newer, installed.parent, PromptIo(io.StringIO("new answer\n")), upgrade=True
        )
        assert (installed / "secrets/hf-token").read_bytes() == members["hf-token"]
        assert (
            installed / "secrets/new-generation-input"
        ).read_bytes() == b"new answer\n"
        assert (installed / "secrets/signing-key").read_bytes() == members[
            "signing-key"
        ]
        assert not (installed / publication.INTENT).exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=30)


def test_large_template_lineage_permits_new_inputs(
    installed: Path, template: NasInstallTemplate
) -> None:
    """Catch reusing a 256KiB secret-read cap for allowed 16MiB template lineage."""
    from vonk_agent_protocol.installer_setup import NasSecretPrompt

    large = template.model_copy(
        update={"docker_compose_yaml": "#" + "a" * (300 * 1024) + "\nservices: {}\n"}
    )
    prepare(large, installed.parent, PromptIo(io.StringIO("")), upgrade=True)
    newer = large.model_copy(
        update={
            "secrets": [
                *large.secrets,
                NasSecretPrompt(file="new-input", prompt="New input"),
            ]
        }
    )
    prepare(
        newer,
        installed.parent,
        PromptIo(io.StringIO("new private input\n")),
        upgrade=True,
    )
    assert (installed / "secrets/new-input").read_bytes() == b"new private input\n"
