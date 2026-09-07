from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/verify-controller-agent-protocol"


def test_agent_protocol_image_verifier_uses_installed_wheel_without_source_mount() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "--interactive" in source
    assert "--read-only" in source
    assert "--user 10001:10001" in source
    assert "site-packages" in source
    assert "max_tokens" in source
    assert "token_budget" in source
    assert "tokenizer" in source
    assert "apiKey" in source
    assert "accessToken" in source
    assert "github_token" in source
    assert "hf_token" in source
    assert "--volume" not in source
    assert "agent_protocol/src" not in source
