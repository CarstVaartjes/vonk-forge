"""Bounded JSON input and exclusive private exports for the operator CLI."""

from __future__ import annotations

import json
import os
import stat
import sys
from contextlib import suppress
from pathlib import Path

from .control_client import MAX_CONTROL_DOCUMENT_BYTES


class PrivateOutput:
    """Reserve an exclusive file and keep its descriptor through remote work."""

    def __init__(self, destination: Path):
        self.path = destination.absolute()
        descriptor = os.open(
            self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        try:
            self.identity = os.fstat(descriptor)
            os.fchmod(descriptor, 0o600)
            self.stream = os.fdopen(descriptor, "w+b")
        except BaseException:
            with suppress(OSError):
                if hasattr(self, "identity") and self._owns_path():
                    self.path.unlink()
            with suppress(OSError):
                os.close(descriptor)
            raise
        self.retain_on_failure = False

    def __enter__(self):
        return self

    def _owns_path(self) -> bool:
        try:
            current = self.path.lstat()
        except FileNotFoundError:
            return False
        return (current.st_dev, current.st_ino) == (
            self.identity.st_dev,
            self.identity.st_ino,
        ) and stat.S_ISREG(current.st_mode)

    def write(self, document: object) -> None:
        if not self._owns_path() or os.fstat(self.stream.fileno()).st_mode & 0o077:
            raise OSError("private output changed; delivery refused")
        content = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()
        self.stream.seek(0)
        self.stream.write(content)
        self.stream.truncate()
        self.stream.flush()
        os.fsync(self.stream.fileno())
        directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        if not self._owns_path():
            raise OSError("private output moved; delivery could not be confirmed")

    def __exit__(self, kind, value, traceback) -> None:
        try:
            self.stream.close()
        except OSError:
            if kind is None:
                raise
        if kind is not None and not self.retain_on_failure and self._owns_path():
            self.path.unlink()


def read_json_document(source: str) -> object:
    if source == "-":
        content = sys.stdin.buffer.read(MAX_CONTROL_DOCUMENT_BYTES + 1)
    else:
        descriptor = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("JSON input must be a regular non-symlink file")
            content = stream.read(MAX_CONTROL_DOCUMENT_BYTES + 1)
    if len(content) > MAX_CONTROL_DOCUMENT_BYTES:
        raise ValueError(
            f"JSON input exceeds the {MAX_CONTROL_DOCUMENT_BYTES}-byte document budget; "
            f"read at least {len(content)} bytes"
        )

    def unique_members(pairs: list[tuple[str, object]]) -> dict[str, object]:
        document: dict[str, object] = {}
        for key, value in pairs:
            if key in document:
                raise ValueError("JSON input contains a duplicate object key")
            document[key] = value
        return document

    def reject_constant(value: str) -> object:
        raise ValueError("JSON input contains a non-finite number")

    return json.loads(
        content, object_pairs_hook=unique_members, parse_constant=reject_constant
    )


def write_private_document(destination: Path, document: object) -> None:
    with PrivateOutput(destination) as output:
        output.write(document)
