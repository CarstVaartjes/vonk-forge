"""Immutable, hook-free reads of allowlisted repository objects."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType

_COMMIT = re.compile(r"[0-9a-f]{40}")
_ROOTS = (
    "inventory/",
    # Workload definitions and promoted release locks are Git authority for
    # the generic package plane.  Keep them in the same immutable, read-only
    # repository boundary as the existing model/profile documents; they must
    # never be read from a mutable checkout path.
    "config/package-families/",
    "config/workload-deployments/",
    "locks/",
    "manifests/",
    "docs/audits/",
)
_MAX_DOCUMENT = 1_048_576
_MAX_TREE = 4_194_304
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")


class RepositoryPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class TypedDocument:
    commit: str
    path: str
    content: bytes
    sha256: str
    parsed: object


@dataclass(frozen=True)
class RepositorySnapshot:
    commit: str
    documents: Mapping[str, str]
    dependencies: Mapping[str, tuple[str, ...]]


class RepositoryService:
    def __init__(self, root: Path) -> None:
        if root.is_symlink() or not root.is_dir() or not (root / ".git").exists():
            raise RepositoryPolicyError("repository root is invalid")
        self._root = root.resolve()
        if "\n" in str(self._root):
            raise RepositoryPolicyError("repository root is invalid")
        self._object_store = self._resolve_object_store()
        if self._object_store.is_symlink() or not self._object_store.is_dir():
            raise RepositoryPolicyError("repository object store is invalid")
        self._environment = os.environ | {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }

    @property
    def root(self) -> Path:
        return self._root

    @property
    def object_store(self) -> Path:
        return self._object_store

    def _resolve_object_store(self) -> Path:
        marker = self._root / ".git"
        if marker.is_symlink():
            raise RepositoryPolicyError("repository object store is invalid")
        if marker.is_dir():
            common_directory = marker
        elif marker.is_file():
            git_directory = self._read_git_path(marker, prefix="gitdir: ", relative_to=self._root)
            if git_directory.is_symlink() or not git_directory.is_dir():
                raise RepositoryPolicyError("repository object store is invalid")
            back_reference = self._read_git_path(
                git_directory / "gitdir", prefix="", relative_to=git_directory
            )
            if back_reference != marker.resolve():
                raise RepositoryPolicyError("repository object store is invalid")
            common_file = git_directory / "commondir"
            common_directory = (
                self._read_git_path(common_file, prefix="", relative_to=git_directory)
                if common_file.is_file() and not common_file.is_symlink()
                else git_directory
            )
            if common_directory.is_symlink() or not common_directory.is_dir():
                raise RepositoryPolicyError("repository object store is invalid")
        else:
            raise RepositoryPolicyError("repository object store is invalid")
        object_store = common_directory / "objects"
        if object_store.is_symlink() or not object_store.is_dir():
            raise RepositoryPolicyError("repository object store is invalid")
        return object_store.resolve()

    @staticmethod
    def _read_git_path(path: Path, *, prefix: str, relative_to: Path) -> Path:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
            raise RepositoryPolicyError("repository object store is invalid")
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise RepositoryPolicyError("repository object store is invalid") from error
        line = content.rstrip("\r\n")
        if not line.startswith(prefix) or not line.removeprefix(prefix) or "\n" in line or "\x00" in line:
            raise RepositoryPolicyError("repository object store is invalid")
        target = Path(line.removeprefix(prefix))
        if not target.is_absolute():
            target = relative_to / target
        return target.resolve()

    def validate_path(self, path: str) -> str:
        return self._path(path)

    def head(self, branch: str = "HEAD") -> str:
        if branch != "HEAD" and (_BRANCH.fullmatch(branch) is None or ".." in branch or "//" in branch):
            raise RepositoryPolicyError("repository branch name is invalid")
        raw = self._run(("rev-parse", "--verify", f"{branch}^{{commit}}"), limit=41, action="resolve branch head")
        return self._commit(raw.decode().strip())

    def _run(self, arguments: tuple[str, ...], *, limit: int, action: str) -> bytes:
        command = (
            "git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.file.allow=never",
            "-C", str(self._root), *arguments,
        )
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=self._environment,
            shell=False,
        )
        assert process.stdout is not None
        output = process.stdout.read(limit + 1)
        if len(output) > limit:
            process.kill()
            process.wait()
            raise RepositoryPolicyError(f"{action} output exceeds the safety limit")
        returncode = process.wait(timeout=10)
        if returncode != 0:
            raise RepositoryPolicyError(f"Git could not {action}")
        return output

    def _commit(self, commit: str) -> str:
        if _COMMIT.fullmatch(commit) is None:
            raise RepositoryPolicyError("commit must be a full lowercase 40-hex object ID")
        self._run(("cat-file", "-e", f"{commit}^{{commit}}"), limit=0, action="resolve commit")
        return commit

    @staticmethod
    def _path(path: str) -> str:
        if not isinstance(path, str) or "\\" in path or "\x00" in path:
            raise RepositoryPolicyError("managed document path is invalid")
        pure = PurePosixPath(path)
        if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
            raise RepositoryPolicyError("managed document path escapes its allowlisted root")
        normalized = pure.as_posix()
        if not any(normalized.startswith(root) for root in _ROOTS):
            raise RepositoryPolicyError("managed document path is not allowlisted")
        return normalized

    def _entry(self, commit: str, path: str) -> tuple[str, str, str]:
        raw = self._run(("ls-tree", "-z", commit, "--", path), limit=4096, action="inspect document")
        entries = [entry for entry in raw.split(b"\x00") if entry]
        if len(entries) != 1:
            raise RepositoryPolicyError("managed document does not exist as one object")
        metadata, encoded_path = entries[0].split(b"\t", 1)
        mode, kind, object_id = metadata.decode().split(" ")
        if encoded_path.decode() != path:
            raise RepositoryPolicyError("managed document path is ambiguous")
        return mode, kind, object_id

    def read_document(self, commit: str, path: str) -> TypedDocument:
        resolved = self._commit(commit)
        normalized = self._path(path)
        mode, kind, object_id = self._entry(resolved, normalized)
        if mode == "120000":
            raise RepositoryPolicyError("managed document must not be a symlink")
        if mode == "160000" or kind != "blob" or not mode.startswith("100"):
            raise RepositoryPolicyError("managed document must be a regular blob")
        size_raw = self._run(("cat-file", "-s", object_id), limit=32, action="size document")
        try:
            size = int(size_raw)
        except ValueError:
            raise RepositoryPolicyError("managed document has an invalid object size") from None
        if size > _MAX_DOCUMENT:
            raise RepositoryPolicyError("managed document exceeds the safety limit")
        content = self._run(("cat-file", "blob", object_id), limit=_MAX_DOCUMENT, action="read document")
        try:
            if normalized.endswith(".toml"):
                parsed: object = tomllib.loads(content.decode("utf-8"))
            elif normalized.endswith(".json"):
                parsed = json.loads(content)
            else:
                parsed = None
        except (UnicodeDecodeError, tomllib.TOMLDecodeError, json.JSONDecodeError) as error:
            raise RepositoryPolicyError("managed document is not valid UTF-8 typed content") from error
        return TypedDocument(resolved, normalized, content, hashlib.sha256(content).hexdigest(), parsed)

    def inspect(self, commit: str) -> RepositorySnapshot:
        resolved = self._commit(commit)
        raw = self._run(
            ("ls-tree", "-r", "-z", "--full-tree", resolved, "--", *_ROOTS),
            limit=_MAX_TREE,
            action="inspect repository tree",
        )
        documents: dict[str, str] = {}
        for entry in (item for item in raw.split(b"\x00") if item):
            metadata, encoded_path = entry.split(b"\t", 1)
            mode, kind, object_id = metadata.decode().split(" ")
            path = encoded_path.decode("utf-8")
            self._path(path)
            if mode == "120000":
                raise RepositoryPolicyError(f"managed document is a symlink: {path}")
            if mode == "160000" or kind != "blob" or not mode.startswith("100"):
                raise RepositoryPolicyError(f"managed document is not a regular blob: {path}")
            documents[path] = object_id
        ordered = dict(sorted(documents.items()))
        return RepositorySnapshot(
            resolved,
            MappingProxyType(ordered),
            MappingProxyType({path: () for path in ordered}),
        )
