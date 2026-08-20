from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs/runbooks/agent-pki.md"


def _text() -> str:
    return RUNBOOK.read_text()


def test_agent_pki_runbook_covers_offline_root_rotation_backup_recovery_and_migration() -> None:
    text = " ".join(_text().lower().split())
    for required in (
        "offline root", "chmod 600", "backup", "restore", "intermediate rotation",
        "overlap", "revocation", "remote ca revocation is uncertain", "certificate loss",
        "fresh enrollment grant", "must not copy", "built-in", "step-ca", "migration",
        "issuing", "manual reconciliation", "24 hours", "clock skew",
    ):
        assert required in text


def test_runbook_shell_blocks_are_syntactically_executable_and_never_mount_root_private_key(tmp_path: Path) -> None:
    text = _text()
    blocks = re.findall(r"```sh\n(.*?)```", text, re.DOTALL)
    assert len(blocks) >= 5
    for index, block in enumerate(blocks):
        script = tmp_path / f"block-{index}.sh"
        script.write_text("set -eu\n" + block)
        result = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
    forbidden = re.compile(r"(?:cp|install|mv|mount|volumes?:).*root(?:_ca)?(?:\.key|[-_]key)", re.IGNORECASE)
    assert forbidden.search(text) is None


def test_compose_and_public_template_keep_root_key_out_and_provider_private_key_separate() -> None:
    compose = ROOT / "deploy/compose"
    result = subprocess.run([
        "docker", "compose", "--env-file", str(compose / "tests/test.env"),
        "-f", str(compose / "compose.yaml"),
        "config", "--format", "json",
    ], check=True, capture_output=True, text=True)
    rendered = json.loads(result.stdout)
    serialized = json.dumps(rendered).lower()
    assert "root_ca_key" not in serialized and "root-ca-key" not in serialized
    assert "ports" not in rendered["services"]["step-ca"]
    assert set(rendered["services"]["step-ca"]["networks"]) == {"ca"}
    assert "ca" in rendered["services"]["control-api"]["networks"]
    assert rendered["services"]["control-api"].get("secrets", []) == []
    assert "agent-ca-credential" in {
        value["source"]
        for value in rendered["services"]["control-bootstrap"]["secrets"]
    }
    assert "agent-ca-credential" not in json.dumps(rendered["services"]["step-ca"])
    provisioner = json.loads((ROOT / "deploy/compose/step-ca/ca.json").read_text())["authority"]["provisioners"][0]
    assert "encryptedKey" not in provisioner and "d" not in provisioner["key"]


def test_public_provisioner_config_bootstrap_executes_in_disposable_fixture(tmp_path: Path) -> None:
    kid = "fixture-provisioner-kid"
    public = {
        "kty": "EC", "crv": "P-256", "use": "sig", "alg": "ES256", "kid": kid,
        "x": "NTgTNOnQHzF1BD0MWqZ09QpZyWoshtsnf5FgMbW7k24",
        "y": "UXNDV6LlUGcWRsPfYaf3noyY-1FvR9fvRztaW2j_rX0",
    }
    private_jwk = public | {"d": "g1Y9_uHz3D8W0zdRO1lulzvuA9nTgNbSiXA1abOvwk0"}
    public_path = tmp_path / "agent-ca-public.jwk"
    private_path = tmp_path / "agent-ca-credential"
    config_path = tmp_path / "ca.json"
    public_path.write_text(json.dumps(public))
    private_path.write_text(json.dumps(private_jwk))
    result = subprocess.run(
        ["bash", "-eu", "-c", "jq --slurpfile key \"$1\" '.authority.provisioners[0].key=$key[0]' \"$2\" > \"$3\"", "bootstrap", str(public_path), str(ROOT / "deploy/compose/step-ca/ca.json"), str(config_path)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    generated = json.loads(config_path.read_text())
    configured = generated["authority"]["provisioners"][0]["key"]
    assert configured == public and "d" not in configured
    stored_private = json.loads(private_path.read_text())
    assert stored_private["kid"] == kid and stored_private["x"] == configured["x"]
    assert stored_private["y"] == configured["y"] and "d" in stored_private


def test_pinned_step_thumbprint_command_is_documented() -> None:
    runbook = _text()
    assert "step crypto jwk thumbprint <" in runbook
    assert "step crypto jwk fingerprint" not in runbook


def test_pinned_step_image_supports_documented_jwk_thumbprint_command() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is unavailable")
    if subprocess.run(["docker", "info"], capture_output=True, check=False).returncode:
        pytest.skip("Docker daemon is unavailable")
    public = {
        "kty": "EC", "crv": "P-256", "use": "sig", "alg": "ES256",
        "x": "NTgTNOnQHzF1BD0MWqZ09QpZyWoshtsnf5FgMbW7k24",
        "y": "UXNDV6LlUGcWRsPfYaf3noyY-1FvR9fvRztaW2j_rX0",
    }
    result = subprocess.run([
        "docker", "run", "--rm", "-i", "--entrypoint", "step",
        "smallstep/step-ca:0.30.2@sha256:a2b17872915c193259b75a5474c398326f41bd199f0842093e52cf4182bc8270",
        "crypto", "jwk", "thumbprint",
    ], input=json.dumps(public), capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}\n?", result.stdout)


def test_recovery_requires_explicit_node_bound_grant_and_never_identity_copy() -> None:
    text = _text().lower()
    recovery = text[text.index("expiry and identity-loss recovery"):]
    assert "fresh" in recovery and "enrollment grant" in recovery
    assert "node-bound" in recovery
    assert "must not copy another gpu node's certificate or private identity" in recovery
    assert "existing mtls identity" in text


def test_operator_entry_points_link_to_pki_runbook() -> None:
    assert "docs/runbooks/agent-pki.md" in (ROOT / "README.md").read_text()
    threat_model = (ROOT / "docs/security/threat-model.md").read_text().lower()
    assert "smallstep" in threat_model and "offline root" in threat_model
