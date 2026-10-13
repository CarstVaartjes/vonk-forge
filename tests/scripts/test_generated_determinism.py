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


def test_xdist_workers_import_prepared_clients_without_regenerating(monkeypatch):
    """Workers must not overwrite modules while another worker imports them."""
    from tools import generated_contracts

    monkeypatch.delenv("VONK_TEST_CONTRACT_ROOT", raising=False)
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")

    def unexpected_generation(*_args, **_kwargs):
        pytest.fail("worker replaced shared generated consumers")

    monkeypatch.setattr(generated_contracts.subprocess, "run", unexpected_generation)
    generated_contracts.prepare_generated_contracts()


def _stamped_tree(tmp_path: Path, monkeypatch) -> tuple[Path, list[str]]:
    """A tree with one generator input and one output per generator."""
    from tools import generated_contracts

    root = tmp_path / "tree"
    (root / "inputs").mkdir(parents=True)
    (root / "inputs/model.py").write_text("one")
    monkeypatch.setattr(generated_contracts, "INPUTS", ("inputs",))
    monkeypatch.setattr(generated_contracts, "CLIENT_OUTPUTS", ("out/client.py",))
    monkeypatch.setattr(
        generated_contracts, "WIRE_OUTPUTS", ("out/wire.json", "out/generated.rs")
    )
    monkeypatch.setattr(
        generated_contracts, "PREPARED_OUTPUTS", ("out/client.py", "out/wire.json")
    )
    monkeypatch.setattr(
        generated_contracts, "COMMANDS", (("generate-clients",), ("export-wire",))
    )
    monkeypatch.delenv("VONK_TEST_CONTRACT_ROOT", raising=False)
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    calls: list[str] = []

    def generate(command, *, cwd, check, timeout):
        calls.append(Path(command[0]).name)
        (root / "out").mkdir(exist_ok=True)
        source = (root / "inputs/model.py").read_text()
        if command[0].endswith("generate-clients"):
            (root / "out/client.py").write_text(f"client of {source}")
        else:
            (root / "out/wire.json").write_text(f"wire of {source}")

    monkeypatch.setattr(generated_contracts.subprocess, "run", generate)
    return root, calls


def test_preparation_generates_once_until_an_input_changes(tmp_path, monkeypatch):
    """Catches preparation that regenerates on every pytest start, and one
    whose stamp ignores the inputs."""
    from tools import generated_contracts

    root, calls = _stamped_tree(tmp_path, monkeypatch)

    assert generated_contracts.prepare_generated_contracts(root) is True
    assert calls == ["generate-clients", "export-wire"]
    monkeypatch.delenv("VONK_TEST_CONTRACT_ROOT")
    assert generated_contracts.prepare_generated_contracts(root) is False
    assert calls == ["generate-clients", "export-wire"]

    (root / "inputs/model.py").write_text("two")
    monkeypatch.delenv("VONK_TEST_CONTRACT_ROOT")
    assert generated_contracts.prepare_generated_contracts(root) is True
    assert calls == ["generate-clients", "export-wire"] * 2
    assert (root / "out/client.py").read_text() == "client of two"


@pytest.mark.parametrize("damage", ["deleted", "edited"])
def test_preparation_regenerates_damaged_outputs(tmp_path, monkeypatch, damage):
    """Catches a stamp that trusts outputs a tool or a checkout replaced."""
    from tools import generated_contracts

    root, calls = _stamped_tree(tmp_path, monkeypatch)
    generated_contracts.prepare_generated_contracts(root)
    if damage == "deleted":
        (root / "out/wire.json").unlink()
    else:
        (root / "out/client.py").write_text("hand edited")

    monkeypatch.delenv("VONK_TEST_CONTRACT_ROOT")
    assert generated_contracts.prepare_generated_contracts(root) is True
    assert len(calls) == 4
    assert (root / "out/client.py").read_text() == "client of one"
    assert (root / "out/wire.json").read_text() == "wire of one"


def test_generated_outputs_are_not_their_own_inputs(tmp_path, monkeypatch):
    """Catches a fingerprint that hashes outputs living under an input root,
    which would mark every tree stale right after generating it."""
    from tools import generated_contracts

    root, calls = _stamped_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(generated_contracts, "INPUTS", ("inputs", "out"))
    generated_contracts.prepare_generated_contracts(root)
    monkeypatch.delenv("VONK_TEST_CONTRACT_ROOT")
    assert generated_contracts.prepare_generated_contracts(root) is False
    assert len(calls) == 2
