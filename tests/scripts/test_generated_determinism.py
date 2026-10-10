"""Reject changed bytes or file membership across generator invocations."""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("change", ["none", "bytes", "membership"])
def test_determinism_compares_output_bytes_and_membership(
    tmp_path, monkeypatch, capsys, change
):
    loader = importlib.machinery.SourceFileLoader(
        "generated_determinism", str(ROOT / "scripts/check-generated-determinism")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "CLIENT_OUTPUTS", ("generated",))
    monkeypatch.setattr("sys.argv", ["check-generated-determinism"])
    directory = tmp_path / "generated"
    directory.mkdir()
    attempts = 0

    def generate(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        (directory / "model.py").write_bytes(
            b"changed" if change == "bytes" and attempts == 2 else b"canonical"
        )
        if change == "membership" and attempts == 2:
            (directory / "added.py").write_bytes(b"extra model")

    monkeypatch.setattr(module.subprocess, "run", generate)
    if change == "none":
        module.main()
        assert "Deterministic" in capsys.readouterr().out
    else:
        with pytest.raises(SystemExit, match="Nondeterministic generation"):
            module.main()
