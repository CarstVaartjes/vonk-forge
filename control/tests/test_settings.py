import json
import logging
from pathlib import Path

import pytest
from vonk_control import settings as settings_module
from vonk_control.settings import Settings, SettingsError

_ENVIRONMENT = (
    "VONK_DEPLOYMENT_MODE",
    "VONK_CONTROL_HOSTNAME",
    "VONK_NAS_LAN_IP",
    "VONK_MANAGEMENT_CIDRS",
    "VONK_DIRECT_FABRIC_CIDRS",
    "VONK_INSTALL_CHANNEL",
    "VONK_RECIPE_LIBRARY_RELEASE",
)


@pytest.fixture
def secrets_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "secrets"
    root.mkdir()
    (root / "database-url").write_text("postgresql+psycopg://control:pw@postgres/db\n")
    monkeypatch.setattr(settings_module, "SECRETS_ROOT", root)
    for name in _ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VONK_CONTROL_HOSTNAME", "vonk-forge.tail1234.ts.net")
    monkeypatch.setenv("VONK_NAS_LAN_IP", "192.168.1.231")
    return root


def test_production_defaults_and_derived_service_names(secrets_root: Path) -> None:
    settings = Settings.from_env_and_secrets()

    assert settings.deployment_mode == "production"
    assert settings.agent_runtime_enabled
    assert settings.database_host == "postgres"
    assert settings.secrets_root == secrets_root
    assert settings.management_cidrs == "192.168.1.0/24"
    assert settings.install_channel == "stable"
    assert settings.recipe_library_release == "latest"
    assert settings.agent_service_hostnames == (
        "vonk-forge.tail1234.ts.net",
        "enroll.vonk-forge.tail1234.ts.net",
        "agents.vonk-forge.tail1234.ts.net",
        "registry.vonk-forge.tail1234.ts.net",
    )
    assert (
        settings.agent_controller_origin
        == "https://agents.vonk-forge.tail1234.ts.net:8443"
    )
    assert (
        settings.agent_enrollment_origin
        == "https://enroll.vonk-forge.tail1234.ts.net:8443"
    )
    assert settings.controller_ca_path == secrets_root / "controller-ca"


def test_production_requires_the_control_hostname(
    secrets_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VONK_CONTROL_HOSTNAME")
    with pytest.raises(SettingsError, match="VONK_CONTROL_HOSTNAME"):
        Settings.from_env_and_secrets()

    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", "test")
    assert not Settings.from_env_and_secrets().agent_runtime_enabled


@pytest.mark.parametrize("database_url", [None, "", "mysql://db/control"])
def test_database_url_fails_closed(
    secrets_root: Path, database_url: str | None
) -> None:
    secret = secrets_root / "database-url"
    if database_url is None:
        secret.unlink()
    else:
        secret.write_text(database_url)
    with pytest.raises(SettingsError):
        Settings.from_env_and_secrets()


def test_database_url_must_not_be_a_symlink(secrets_root: Path) -> None:
    secret = secrets_root / "database-url"
    target = secrets_root / "actual"
    secret.rename(target)
    secret.symlink_to(target)
    with pytest.raises(SettingsError, match="regular non-symlink"):
        Settings.from_env_and_secrets()


def test_network_lists_self_heal(
    secrets_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv(
        "VONK_MANAGEMENT_CIDRS", "192.168.1.5/24, not-a-cidr 192.168.1.0/24,fd00::1/64"
    )
    monkeypatch.setenv("VONK_DIRECT_FABRIC_CIDRS", "10.10.0.7/24,192.168.1.240/28")

    with caplog.at_level(logging.WARNING):
        settings = Settings.from_env_and_secrets()

    assert settings.management_cidrs == "192.168.1.0/24,fd00::/64"
    # The fabric entry inside a management network is dropped, not fatal.
    assert settings.direct_fabric_cidrs == "10.10.0.0/24"
    assert "not-a-cidr" in caplog.text


def test_management_networks_default_to_the_nas_lan(
    secrets_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VONK_MANAGEMENT_CIDRS", "garbage")
    assert Settings.from_env_and_secrets().management_cidrs == "192.168.1.0/24"

    monkeypatch.delenv("VONK_NAS_LAN_IP")
    with pytest.raises(SettingsError, match="VONK_MANAGEMENT_CIDRS"):
        Settings.from_env_and_secrets()


def test_invalid_operator_choices_fall_back_to_safe_defaults(
    secrets_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VONK_INSTALL_CHANNEL", "dev")
    monkeypatch.setenv("VONK_RECIPE_LIBRARY_RELEASE", "v2.1.0")
    settings = Settings.from_env_and_secrets()
    assert (settings.install_channel, settings.recipe_library_release) == (
        "dev",
        "v2.1.0",
    )

    monkeypatch.setenv("VONK_INSTALL_CHANNEL", "preview")
    # Another contract major cannot be pinned.
    monkeypatch.setenv("VONK_RECIPE_LIBRARY_RELEASE", "v1.1.0")
    settings = Settings.from_env_and_secrets()
    assert (settings.install_channel, settings.recipe_library_release) == (
        "stable",
        "latest",
    )


def test_worker_never_reads_api_only_secrets(secrets_root: Path) -> None:
    # No token, metrics or proxy secrets exist: loading must still succeed.
    settings = Settings.from_env_and_secrets()
    assert settings.huggingface_token_path is None

    with pytest.raises(SettingsError, match="token-signing-key"):
        _ = settings.token_signing_key


def test_api_secrets_are_validated_on_first_use(secrets_root: Path) -> None:
    (secrets_root / "token-signing-key").write_text("k" * 32)
    (secrets_root / "metrics-token").write_text("m" * 16 + "\n")
    (secrets_root / "agent-proxy-auth").write_text("p" * 32 + "\r\n")
    (secrets_root / "hf-token").write_text("hf_test_secret\n")
    settings = Settings.from_env_and_secrets()

    assert settings.token_signing_key == b"k" * 32
    assert settings.metrics_token == "m" * 16
    assert settings.agent_proxy_auth == b"p" * 32
    assert settings.huggingface_token_path == secrets_root / "hf-token"
    assert "hf_test_secret" not in repr(settings)

    (secrets_root / "token-signing-key").write_text("short")
    with pytest.raises(SettingsError, match="32 bytes"):
        _ = Settings.from_env_and_secrets().token_signing_key


def test_development_uses_ephemeral_api_secrets(
    secrets_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", "development")
    settings = Settings.from_env_and_secrets()

    assert len(settings.token_signing_key) == 32
    assert settings.metrics_token
    assert settings.agent_proxy_auth == b""


@pytest.mark.parametrize(
    "proxy_auth",
    (
        "p" * 31 + "\n",
        "p" * 32 + " ",
        " " + "p" * 32,
        "p" * 16 + " " + "p" * 16,
        "p" * 31 + "=",
        "p" * 16 + "\n" + "p" * 16,
        "p" * 16 + "\x00" + "p" * 16,
    ),
)
def test_noncanonical_agent_proxy_auth_is_refused(
    secrets_root: Path, proxy_auth: str
) -> None:
    (secrets_root / "agent-proxy-auth").write_text(proxy_auth)
    with pytest.raises(SettingsError, match="base64url-like"):
        _ = Settings.from_env_and_secrets().agent_proxy_auth


def test_provisioner_key_id_comes_from_the_public_jwk(secrets_root: Path) -> None:
    jwk = secrets_root / "agent-ca-provisioner-public-jwk"
    jwk.write_text(json.dumps({"kty": "EC", "kid": "thumbprint-kid"}))
    assert Settings.from_env_and_secrets().agent_ca_provisioner_kid == "thumbprint-kid"

    jwk.write_text(json.dumps({"kty": "EC"}))
    with pytest.raises(SettingsError, match="kid"):
        _ = Settings.from_env_and_secrets().agent_ca_provisioner_kid


@pytest.mark.parametrize(
    ("duration", "seconds"),
    [("90s", 90), ("720h", 2_592_000), ("1h30m", 5400), ("1ms", 2_592_000)],
)
def test_certificate_lifetime_follows_the_step_ca_provisioner(
    secrets_root: Path, duration: str, seconds: int
) -> None:
    (secrets_root / "step-ca").mkdir()
    (secrets_root / "step-ca" / "ca.json").write_text(
        json.dumps(
            {
                "authority": {
                    "provisioners": [
                        {"name": "other", "claims": {"defaultTLSCertDuration": "1h"}},
                        {
                            "name": "vonk-forge-agent",
                            "claims": {"defaultTLSCertDuration": duration},
                        },
                    ]
                }
            }
        )
    )
    settings = Settings.from_env_and_secrets()
    assert settings.agent_ca_certificate_lifetime_seconds == seconds


def test_certificate_lifetime_defaults_to_thirty_days_without_config(
    secrets_root: Path,
) -> None:
    settings = Settings.from_env_and_secrets()
    assert settings.agent_ca_certificate_lifetime_seconds == 30 * 24 * 60 * 60


def test_compose_is_platform_neutral_and_only_caddy_publishes_ports() -> None:
    root = Path(__file__).resolve().parents[2]
    text = (root / "deploy/compose/compose.yaml").read_text()
    assert "ugreen" not in text.lower()
    assert "192.168." not in text
    assert "node1" not in text.lower() and "node2" not in text.lower()
    assert text.count("ports:") == 1
    assert "control-api:" in text and "control-worker:" in text
    assert "postgres:" in text and "caddy:" in text
