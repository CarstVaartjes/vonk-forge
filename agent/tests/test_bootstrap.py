from __future__ import annotations

import json
from pathlib import Path

import pytest

from vonk_agent.bootstrap import (
    BootstrapError,
    BootstrapResponse,
    bootstrap,
    parse_bootstrap_args,
)

NODE = "spk_0123456789abcdef0123456789abcdef"
FINGERPRINT = "a" * 64


def args(tmp_path: Path) -> list[str]:
    return [
        "--token", "A" * 43,
        "--controller-endpoint", "https://controller.example",
        "--enrollment-endpoint", "https://enroll.example",
        "--ca-fingerprint", FINGERPRINT,
        "--config", str(tmp_path / "config.json"),
        "--state-root", str(tmp_path / "state"),
        "--ca-path", str(tmp_path / "ca.pem"),
    ]


def test_parse_rejects_duplicate_explicit_arguments(tmp_path: Path) -> None:
    with pytest.raises(BootstrapError, match="duplicate"):
        parse_bootstrap_args(args(tmp_path) + ["--token", "B" * 43])


def test_parse_rejects_invalid_fingerprint_and_unsafe_paths(tmp_path: Path) -> None:
    with pytest.raises(BootstrapError, match="fingerprint"):
        parse_bootstrap_args([*args(tmp_path)[:-8], "--ca-fingerprint", "not-a-digest"] + args(tmp_path)[-6:])
    with pytest.raises(BootstrapError, match="path"):
        parse_bootstrap_args([*args(tmp_path)[:-6], "--config", "relative.json", *args(tmp_path)[-4:]])


def test_bootstrap_generates_material_config_and_consumes_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parsed = parse_bootstrap_args(args(tmp_path))
    token_path = tmp_path / "token"
    token_path.write_text("A" * 43 + "\n", encoding="ascii")
    token_path.chmod(0o600)
    response = BootstrapResponse(status="pending", expires_at="2099-01-01T00:00:00+00:00")
    monkeypatch.setattr("vonk_agent.bootstrap._read_ca", lambda *_: None)

    result = bootstrap(
        parsed,
        token_path=token_path,
        submit=lambda token, node, csr: response,
        verify_installer=lambda _path: None,
    )

    assert result.response == response
    assert not token_path.exists()
    document = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert document["node_id"].startswith("spk_")
    assert len(document["node_id"]) == 36
    assert (tmp_path / "state" / "credentials" / "pending-csr.pem").exists()


def test_bootstrap_rejects_expired_or_failed_registration(tmp_path: Path) -> None:
    parsed = parse_bootstrap_args(args(tmp_path))
    token_path = tmp_path / "token"
    token_path.write_text("A" * 43, encoding="ascii")
    for response in (
        BootstrapResponse(status="expired"),
        BootstrapResponse(status="failed"),
    ):
        with pytest.raises(BootstrapError):
            bootstrap(parsed, token_path=token_path, submit=lambda *_: response, verify_installer=lambda _: None)
        assert token_path.exists()
