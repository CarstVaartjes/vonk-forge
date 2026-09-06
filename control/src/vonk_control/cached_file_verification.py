"""Reuse content checks while a cache file's filesystem identity is unchanged.

Authorization and expected digests still come from the caller on every use.
Only the expensive byte scan is cached, in memory, for this process lifetime.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections import OrderedDict
from pathlib import Path
from threading import RLock
from typing import BinaryIO


class CachedFileVerifier:
    def __init__(self, *, capacity: int = 8192) -> None:
        self.capacity = capacity
        self._verified: OrderedDict[tuple[object, ...], None] = OrderedDict()
        self._lock = RLock()

    @staticmethod
    def _identity(value: os.stat_result) -> tuple[int, ...]:
        return (
            value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns,
            value.st_mode, value.st_uid, value.st_nlink,
        )

    def verify(self, source: BinaryIO, digest: str, expected_bytes: int) -> bool:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected_bytes:
            return False
        identity = self._identity(before)
        key = (digest, *identity)
        with self._lock:
            if key in self._verified:
                self._verified.move_to_end(key)
                return True
        source.seek(0)
        hasher = hashlib.sha256()
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
        source.seek(0)
        if self._identity(os.fstat(source.fileno())) != identity or hasher.hexdigest() != digest:
            return False
        with self._lock:
            self._verified[key] = None
            self._verified.move_to_end(key)
            while len(self._verified) > self.capacity:
                self._verified.popitem(last=False)
        return True

    def verify_path(self, path: Path, digest: str, expected_bytes: int) -> bool:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as source:
                return self.verify(source, digest, expected_bytes)
        except OSError:
            return False


verified_files = CachedFileVerifier()
