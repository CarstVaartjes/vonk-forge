"""Pin the image-pin checker's fail/warn boundary.

The checker resolves registry references, so the rule that decides whether it
blocks matters more than the reading itself: a digest that is gone breaks every
image build and must fail, while a rolling tag that moved upstream, or a
registry that could not be reached, must not.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/check-image-pins"


def _module() -> ModuleType:
    specification = importlib.util.spec_from_loader(
        "check_image_pins", importlib.machinery.SourceFileLoader("check_image_pins", str(SCRIPT))
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _root(tmp_path: Path, pins: dict[str, str]) -> Path:
    """A minimal repository holding one inventory entry per supplied pin."""

    root = tmp_path / "repository"
    (root / "deploy/compose").mkdir(parents=True)
    (root / "deploy/compose/images.lock.json").write_text(
        json.dumps({"schema_version": 1, "images": pins, "build_bases": {}}),
        encoding="utf-8",
    )
    for relative in _module().SCAN_PATHS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    return root


def test_a_moved_tag_is_reported_without_blocking(monkeypatch, tmp_path) -> None:
    module = _module()
    recorded = "sha256:" + "a" * 64
    elsewhere = "sha256:" + "b" * 64
    monkeypatch.setattr(
        module, "resolve", lambda reference, timeout: (elsewhere, None, "")
    )

    payload = module.audit(
        _root(tmp_path, {"caddy": f"caddy:2.11.4@{recorded}"}), 1.0
    )

    assert [finding["kind"] for finding in payload["findings"]] == ["moved"]
    assert payload["ok"] is True


def test_a_missing_digest_blocks(monkeypatch, tmp_path) -> None:
    module = _module()
    recorded = "sha256:" + "a" * 64
    monkeypatch.setattr(
        module,
        "resolve",
        lambda reference, timeout: (None, "manifest unknown", "missing"),
    )

    payload = module.audit(
        _root(tmp_path, {"caddy": f"caddy:2.11.4@{recorded}"}), 1.0
    )

    assert [finding["kind"] for finding in payload["findings"]] == ["missing"]
    assert payload["ok"] is False


def test_an_unreadable_registry_warns_without_blocking(monkeypatch, tmp_path) -> None:
    module = _module()
    recorded = "sha256:" + "a" * 64
    monkeypatch.setattr(
        module,
        "resolve",
        lambda reference, timeout: (None, "dial tcp: i/o timeout", "unverified"),
    )

    payload = module.audit(
        _root(tmp_path, {"caddy": f"caddy:2.11.4@{recorded}"}), 1.0
    )

    assert [finding["kind"] for finding in payload["findings"]] == ["unverified"]
    assert payload["ok"] is True


def test_a_pinned_image_absent_from_the_lock_blocks(monkeypatch, tmp_path) -> None:
    module = _module()
    monkeypatch.setattr(module, "resolve", lambda reference, timeout: ("sha256:" + "a" * 64, None, ""))
    root = _root(tmp_path, {})
    (root / "control/Dockerfile").write_text(
        "FROM postgres:18.6@sha256:" + "c" * 64 + "\n", encoding="utf-8"
    )

    payload = module.audit(root, 1.0)

    assert [finding["kind"] for finding in payload["findings"]] == ["unlisted"]
    assert payload["ok"] is False


@pytest.mark.parametrize(
    ("stderr", "expected"),
    (
        ("ERROR: manifest unknown", "missing"),
        ("ERROR: not found", "missing"),
        ("dial tcp 1.2.3.4:443: i/o timeout", "unverified"),
        ("Get https://ghcr.io: no such host", "unverified"),
        ("net/http: TLS handshake timeout", "unverified"),
    ),
)
def test_registry_errors_are_classified_as_gone_or_unreachable(
    monkeypatch, stderr: str, expected: str
) -> None:
    module = _module()
    monkeypatch.setattr(module.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=[], returncode=1, stdout=b"", stderr=stderr.encode()
        ),
    )

    _, _, kind = module.resolve("caddy:2.11.4@sha256:" + "a" * 64, 1.0)

    assert kind == expected


def test_a_successful_read_returns_the_manifest_digest(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(module.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"manifest-bytes", stderr=b""
        ),
    )

    digest, error, kind = module.resolve("caddy:2.11.4@sha256:" + "a" * 64, 1.0)

    assert error is None and kind == ""
    assert digest == "sha256:" + hashlib.sha256(b"manifest-bytes").hexdigest()
