"""Small canonical setup documents; PKI fixtures are owned by test_pki."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from vonk_agent_protocol.installer_setup import NasInstallTemplate
from vonk_nas_setup.prompts import PromptIo
from vonk_nas_setup.setup import prepare


@pytest.fixture
def template() -> NasInstallTemplate:
    return NasInstallTemplate.model_validate_json("""{
      "schema_version": 2,
      "docker_compose_yaml": "services: {}\\n",
      "internal_values": [{"env":"VONK_CONTROLLER_IMAGE","value":"controller:new"}],
      "required_values": [{"env":"NAS_LAN_IP","prompt":"NAS IP","validation":"ipv4"},
                          {"env":"VONK_MANAGEMENT_CIDRS","prompt":"Management CIDRs","validation":"cidr_list"}],
      "optional_values": ["VONK_HERMES_TIMEZONE"],
      "secrets": [{"file":"hf-token","prompt":"HF token","optional":true},
                  {"file":"tailscale-key","prompt":"Tailscale key","secure_remote_only":true}],
      "install_modes": {"prompt":"Install mode","lab_value":"lab","secure_remote_value":"secure-remote",
                        "lab_values": []},
      "hermes": {"env":"COMPOSE_PROFILES","prompt":"Enable Hermes","enabled_value":"hermes","disabled_value":""},
      "generated_secrets": {
        "random_text": [{"file":"postgres-password","bytes":32}, {"file":"litellm-key","bytes":32,"prefix":"sk-"}],
        "ed25519_pkcs8_pem": [{"file":"signing-key"}],
        "postgres_urls": [{"file":"database-url","password_file":"postgres-password","scheme":"postgresql+psycopg",
                           "username":"vonk","host":"postgres","port":5432,"database":"vonk"}]}
    }""")


@pytest.fixture
def installed(tmp_path: Path, template: NasInstallTemplate) -> Path:
    return prepare(
        template, tmp_path, PromptIo(io.StringIO("lab\n192.168.1.5\n\n"))
    ).root
