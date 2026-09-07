from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from vonk_control.cached_file_verification import CachedFileVerifier


def test_repeated_reads_reuse_verification_but_same_size_changes_do_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "model.bin"
    original = b"verified model weights"
    path.write_bytes(original)
    digest = hashlib.sha256(original).hexdigest()
    verifier = CachedFileVerifier()
    original_hasher = hashlib.sha256
    scans = []

    def count_scan():
        scans.append(True)
        return original_hasher()

    monkeypatch.setattr("vonk_control.cached_file_verification.hashlib.sha256", count_scan)
    for _ in range(5):
        assert verifier.verify_path(path, digest, len(original))
    assert len(scans) == 1
    timestamp = path.stat().st_mtime_ns
    path.write_bytes(b"x" * len(original))
    os.utime(path, ns=(timestamp, timestamp))
    assert not verifier.verify_path(path, digest, len(original))
    assert len(scans) == 2


def test_replacement_symlink_and_different_digest_cannot_reuse_verification(tmp_path: Path) -> None:
    path = tmp_path / "image.tar"
    original = b"verified archive"
    path.write_bytes(original)
    digest = hashlib.sha256(original).hexdigest()
    verifier = CachedFileVerifier()
    assert verifier.verify_path(path, digest, len(original))
    assert not verifier.verify_path(path, "0" * 64, len(original))
    assert not verifier.verify_path(path, digest, len(original) + 1)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"x" * len(original))
    replacement.replace(path)
    assert not verifier.verify_path(path, digest, len(original))
    path.unlink()
    target = tmp_path / "target"
    target.write_bytes(original)
    path.symlink_to(target)
    assert not verifier.verify_path(path, digest, len(original))


def test_changed_during_verification_is_not_remembered(tmp_path: Path) -> None:
    path = tmp_path / "model.bin"
    content = b"initial model"
    path.write_bytes(content)
    verifier = CachedFileVerifier()
    digest = hashlib.sha256(content).hexdigest()
    with path.open("rb") as stream:
        class ChangingReader:
            def fileno(self):
                return stream.fileno()

            def seek(self, offset):
                return stream.seek(offset)

            def read(self, amount):
                data = stream.read(amount)
                if data:
                    path.write_bytes(b"x" * len(content))
                return data

        assert not verifier.verify(ChangingReader(), digest, len(content))
    assert not verifier.verify_path(path, digest, len(content))
