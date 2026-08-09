"""Code-host abstraction; production credentials stay behind provider references."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")
_COMMIT = re.compile(r"[0-9a-f]{40}")


class CodeHost(Protocol):
    def create_change(self, branch: str, base_commit: str, patch: bytes, message: str, *, signed: bool) -> str: ...
    def open_pull_request(self, branch: str, commit: str, title: str) -> str: ...
    def reachable_from(self, commit: str, branch: str) -> bool: ...
    def check_state(self, commit: str, check: str) -> str | None: ...


@dataclass(frozen=True)
class HostedChange:
    commit: str
    branch: str
    pull_request: str | None


class RepositoryCodeHost:
    """Signed local Git mutation used before the protected-PR transition.

    The repository must be a dedicated clean checkout. Release PR-only mode
    deliberately requires a separate protected code-host provider.
    """

    def __init__(self, root: Path, *, signing_key: Path, lock_path: Path) -> None:
        self._root = root.resolve()
        self._key = signing_key.resolve()
        self._lock = lock_path
        if (
            root.is_symlink() or not (self._root / ".git").is_dir()
            or signing_key.is_symlink() or not self._key.is_file()
            or lock_path.is_symlink()
        ):
            raise ValueError("managed repository, signing key, or lock path is unsafe")
        self._environment = os.environ | {"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}

    def _git(
        self, *arguments: str, cwd: Path | None = None, input: bytes | None = None,
        limit: int = 1_048_576,
    ) -> bytes:
        completed = subprocess.run(
            ("git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=never", "-C", str(cwd or self._root), *arguments),
            input=input, capture_output=True,
            timeout=30, check=False, shell=False, env=self._environment,
        )
        if completed.returncode != 0 or len(completed.stdout) > limit:
            raise RuntimeError("managed Git operation failed")
        return completed.stdout

    def _head(self, branch: str) -> str | None:
        completed = subprocess.run(
            ("git", "-C", str(self._root), "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
            timeout=10, shell=False, env=self._environment,
        )
        value = completed.stdout.decode().strip()
        return value if completed.returncode == 0 and _COMMIT.fullmatch(value) else None

    def create_change(self, branch: str, base_commit: str, patch: bytes, message: str, *, signed: bool) -> str:
        if not signed:
            raise ValueError("managed repository changes must be signed")
        if (
            _BRANCH.fullmatch(branch) is None or ".." in branch or "//" in branch
            or _COMMIT.fullmatch(base_commit) is None or not patch or len(patch) > 4_194_304
            or not message.strip() or len(message) > 16_384 or "\x00" in message
        ):
            raise ValueError("managed repository change parameters are invalid")
        self._lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self._lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            if self._git("status", "--porcelain").strip():
                raise RuntimeError("managed repository checkout is not clean")
            existing = self._head(branch)
            if existing is not None and existing != base_commit:
                parent = self._git("rev-parse", f"{existing}^").decode().strip()
                existing_message = self._git("show", "-s", "--format=%B", existing).decode().strip()
                if parent == base_commit and existing_message == message.strip():
                    return existing
                raise RuntimeError("managed branch advanced from the proposal base")
            temporary_root = Path(tempfile.mkdtemp(prefix="vonk-git-change-"))
            worktree = temporary_root / "worktree"
            try:
                self._git("worktree", "add", "--detach", str(worktree), base_commit)
                self._git("apply", "--index", "--binary", "--whitespace=nowarn", "-", cwd=worktree, input=patch)
                self._git(
                    "-c", "user.name=Vonk Forge Control",
                    "-c", "user.email=control@vonk-forge.invalid",
                    "-c", "gpg.format=ssh", "-c", "gpg.ssh.program=ssh-keygen",
                    "-c", f"user.signingKey={self._key}",
                    "commit", "-S", "-m", message, cwd=worktree,
                )
                commit = self._git("rev-parse", "HEAD", cwd=worktree).decode().strip()
                if _COMMIT.fullmatch(commit) is None or b"gpgsig" not in self._git("cat-file", "commit", commit):
                    raise RuntimeError("managed commit signature is missing")
                ref = f"refs/heads/{branch}"
                self._git("update-ref", ref, commit, existing or "0" * 40)
                if self._git("symbolic-ref", "-q", "HEAD").decode().strip() == ref:
                    self._git("reset", "--hard", commit)
                return commit
            finally:
                subprocess.run(("git", "-C", str(self._root), "worktree", "remove", "--force", str(worktree)), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, env=self._environment)
                shutil.rmtree(temporary_root, ignore_errors=True)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def open_pull_request(self, branch: str, commit: str, title: str) -> str:
        raise RuntimeError("release PR-only mode requires a protected code-host provider")

    def reachable_from(self, commit: str, branch: str) -> bool:
        if _COMMIT.fullmatch(commit) is None or _BRANCH.fullmatch(branch) is None:
            return False
        completed = subprocess.run(
            ("git", "-C", str(self._root), "merge-base", "--is-ancestor", commit, branch),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            timeout=10, shell=False, env=self._environment,
        )
        return completed.returncode == 0

    def check_state(self, commit: str, check: str) -> str | None:
        return None


class InMemoryCodeHost:
    """Deterministic policy fake; never represents a production credential store."""

    def __init__(self, *, required_checks: tuple[str, ...]) -> None:
        self._branches: dict[str, tuple[str, bytes, str, str]] = {}
        self._pull_requests: dict[str, str] = {}
        self._merged: set[str] = set()
        self._checks: dict[str, dict[str, str]] = {}
        self.required_checks = required_checks
        self.submission_count = 0
        self.last_message = ""

    def create_change(self, branch: str, base_commit: str, patch: bytes, message: str, *, signed: bool) -> str:
        if not signed:
            raise ValueError("control-plane commits must be signed")
        existing = self._branches.get(branch)
        identity = (base_commit, patch, message)
        if existing:
            if existing[:3] != identity:
                raise ValueError("refusing force update of an existing control branch")
            return existing[3]
        commit = hashlib.sha1(base_commit.encode() + b"\0" + patch + b"\0" + message.encode()).hexdigest()
        self._branches[branch] = (*identity, commit)
        self._checks.setdefault(commit, {})
        self.submission_count += 1
        self.last_message = message
        return commit

    def open_pull_request(self, branch: str, commit: str, title: str) -> str:
        if branch not in self._branches or self._branches[branch][3] != commit:
            raise ValueError("pull request branch does not contain the proposed commit")
        return self._pull_requests.setdefault(branch, f"pr://{branch}")

    def reachable_from(self, commit: str, branch: str) -> bool:
        return commit in self._merged

    def check_state(self, commit: str, check: str) -> str | None:
        return self._checks.get(commit, {}).get(check)

    def seed_commit(self, *, merged: bool, checks: dict[str, str]) -> str:
        commit = hashlib.sha1(repr((len(self._checks), checks)).encode()).hexdigest()
        self._checks[commit] = dict(checks)
        if merged:
            self._merged.add(commit)
        return commit

    def set_check(self, commit: str, check: str, state: str) -> None:
        self._checks.setdefault(commit, {})[check] = state
