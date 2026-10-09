"""Shared VM cargo contention and process death must not poison the next hook."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import shlex
import subprocess
import time
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]


def hook(script: str = "check-staged-code") -> ModuleType:
    loader = importlib.machinery.SourceFileLoader(
        "vm_cargo_hook", str(ROOT / "scripts" / script)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.mark.linux_only
def test_vm_shell_bounds_contention_and_preserves_caller_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcntl
    import os
    import shutil
    import sys

    module = hook()
    root = tmp_path / "worktree with spaces"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/vm-cargo-cache", root / "scripts/vm-cargo-cache")
    lock = tmp_path / "cargo.lock"
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module, "VM_CARGO_LOCK", str(lock))
    output = root / "arguments.json"
    hostile = "argument with spaces; $(touch injected)"
    command = [
        sys.executable,
        "-c",
        "import json,sys; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))",
        str(output),
        hostile,
        "",
        "multiline\nargument",
    ]
    script = module.vm_cargo_command(command, 2)[-1]
    with lock.open("w") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX)
        blocked = subprocess.run(
            ["bash", "-lc", script],
            env={**os.environ, "HOME": str(tmp_path)},
            timeout=5,
            check=False,
        )
        assert blocked.returncode != 0
        assert not output.exists()
    fresh = subprocess.run(
        ["bash", "-lc", script],
        env={**os.environ, "HOME": str(tmp_path)},
        timeout=5,
        check=False,
    )
    assert fresh.returncode == 0
    import json

    assert json.loads(output.read_text()) == [hostile, "", "multiline\nargument"]
    assert not (root / "injected").exists()


@pytest.mark.parametrize(
    ("status", "stderr", "code"),
    [
        (75, "", "HOOK_VM_BUSY"),
        (137, "", "HOOK_VM_PROCESS_KILLED"),
        (-9, "", "HOOK_VM_PROCESS_KILLED"),
        (101, "clippy-driver ... (signal: 9, SIGKILL: kill)", "HOOK_VM_PROCESS_KILLED"),
        (124, "", "HOOK_VM_TIMEOUT"),
    ],
)
def test_vm_environment_fault_preserves_diagnostics_and_admits_fresh_hook(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: int,
    stderr: str,
    code: str,
) -> None:
    """Catches OOM mislabeled as compiler failure and persistent busy bookkeeping."""
    module = hook()
    results = iter(
        [
            subprocess.CompletedProcess([], status, "compiler output\n", stderr),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
    )
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: next(results))
    with pytest.raises(SystemExit) as error:
        module.run_vm_cargo(["cargo", "clippy"], time.monotonic() + 30)
    assert error.value.code
    output = capsys.readouterr()
    assert "compiler output" in output.out
    assert stderr in output.err
    module.run_vm_cargo(["cargo", "clippy"], time.monotonic() + 30)


def test_vm_compile_error_is_not_an_environment_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches treating every cargo exit 101 as retryable memory exhaustion."""
    module = hook()
    results = iter(
        [
            subprocess.CompletedProcess([], 101, "", "error[E0308]: type mismatch"),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
    )
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: next(results))
    with pytest.raises(subprocess.CalledProcessError) as error:
        module.run_vm_cargo(["cargo", "clippy"], time.monotonic() + 30)
    assert error.value.returncode == 101
    module.run_vm_cargo(["cargo", "clippy"], time.monotonic() + 30)


def test_vm_host_timeout_and_exhausted_budget_admit_fresh_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches indefinite host observation and dispatch after the budget expires."""
    module = hook()
    calls = []

    def run(command, **kwargs):
        calls.append(kwargs["timeout"])
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(SystemExit):
        module.run_vm_cargo(["cargo", "clippy"], time.monotonic() - 1)
    assert calls == []
    with pytest.raises(SystemExit):
        module.run_vm_cargo(["cargo", "clippy"], time.monotonic() + 30)
    assert 0 < calls[0] <= 30
    module.run_vm_cargo(["cargo", "clippy"], time.monotonic() + 30)


def test_vm_short_budget_bounds_cleanup_and_lock_wait() -> None:
    """Catches a fixed cleanup grace that outlives a nearly exhausted budget."""
    module = hook()
    tokens = shlex.split(module.vm_cargo_command(["cargo", "check"], 2)[-1])
    start = tokens.index("timeout")
    assert tokens[start + 2 : start + 4] == ["--kill-after=1.000s", "1.000s"]
    tokens = shlex.split(module.vm_cargo_command(["cargo", "check"], 600)[-1])
    lock = tokens.index("flock")
    assert tokens[lock + 2] == "60.000"


def test_darwin_dispatch_locks_both_checks_under_one_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a future cargo dispatch bypassing serialization or resetting time."""
    module = hook()
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        module.subprocess, "check_output", lambda *a, **k: b"rust/probe.rs\0"
    )
    monkeypatch.setattr(module, "run", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(
        module,
        "run_vm_cargo",
        lambda command, deadline: calls.append((command, deadline)),
    )
    module.main()
    assert [call[0][4] for call in calls] == ["fmt", "clippy"]
    assert calls[0][1] == calls[1][1]


def test_vm_target_is_checkout_specific_and_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches shared artifacts and inherited target overrides across clones."""
    module = hook()
    targets = []
    for checkout in ("/first clone", "/second clone", "/first clone"):
        monkeypatch.setattr(module, "ROOT", Path(checkout))
        tokens = shlex.split(module.vm_cargo_command(["cargo", "check"], 20)[-1])
        targets.append(
            next(token for token in tokens if token.startswith("CARGO_TARGET_DIR="))
        )
    assert targets[0] != targets[1]
    assert targets[0] == targets[2]
    assert "${CARGO_TARGET_DIR:-" not in targets[0]
    helper = hook("vm-cargo-cache")
    assert targets[0].endswith(helper.target_key(Path("/first clone")))


def test_vm_cleanup_retains_live_unmarked_and_mismatched_targets(
    tmp_path: Path,
) -> None:
    """Catches deleting a live clone or trusting an unrelated/malformed marker."""
    cache = tmp_path / "targets"
    cache.mkdir()
    helper = hook("vm-cargo-cache")
    live = tmp_path / "live clone"
    live.mkdir()
    gone = tmp_path / "gone clone"
    helper.prepare(live, cache)
    stale = cache / helper.target_key(gone)
    stale.mkdir()
    (stale / helper.MARKER).write_bytes(bytes(gone))
    (stale / "artifact").write_text("stale")
    unmarked = cache / "unmarked"
    unmarked.mkdir()
    mismatch = cache / "mismatched"
    mismatch.mkdir()
    (mismatch / helper.MARKER).write_bytes(bytes(gone))
    external = tmp_path / "external"
    external.mkdir()
    link = cache / "symlink"
    link.symlink_to(external, target_is_directory=True)
    helper.prepare(live, cache)
    assert not stale.exists()
    assert (cache / helper.target_key(live)).is_dir()
    assert unmarked.is_dir()
    assert mismatch.is_dir()
    assert link.is_symlink()
    assert external.is_dir()
    # A fresh invocation reuses the live target after reclamation.
    helper.prepare(live, cache)
    assert (cache / helper.target_key(live) / helper.MARKER).read_bytes() == bytes(live)


def test_vm_cleanup_failure_does_not_block_current_or_fresh_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches cleanup errors aborting preparation or poisoning a new checkout."""
    helper = hook("vm-cargo-cache")
    cache = tmp_path / "targets"
    gone = tmp_path / "gone"
    helper.prepare(gone, cache)
    current = tmp_path / "current"
    current.mkdir()

    def denied(target: Path) -> None:
        raise PermissionError(target)

    monkeypatch.setattr(helper.shutil, "rmtree", denied)
    helper.prepare(current, cache)
    assert (cache / helper.target_key(current) / helper.MARKER).read_bytes() == bytes(
        current
    )
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    helper.prepare(fresh, cache)
    assert (cache / helper.target_key(fresh) / helper.MARKER).read_bytes() == bytes(
        fresh
    )
    assert (cache / helper.target_key(gone)).is_dir()
