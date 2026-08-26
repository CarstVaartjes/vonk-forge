from pathlib import Path

import pytest
from vonk_control.settings import Settings, SettingsError, WorkerSettings


def test_database_secret_is_read_from_file(tmp_path: Path, monkeypatch) -> None:
    secret = tmp_path / "database-url"
    secret.write_text("postgresql+psycopg://control:pw@postgres/control\n")
    monkeypatch.setenv("VONK_DATABASE_URL_FILE", str(secret))
    settings = Settings.from_env_and_secrets()
    assert settings.database_host == "postgres"
    assert settings.global_catalog_url == "https://vonkforge.ai"
    assert settings.recipe_library_api_url == "https://api.github.com"


def test_global_catalog_origin_is_https_or_explicit_loopback(monkeypatch) -> None:
    monkeypatch.setenv("VONK_DATABASE_URL", "postgresql://db/control")
    monkeypatch.setenv("VONK_GLOBAL_CATALOG_URL", "http://127.0.0.1:9000")
    assert Settings.from_env_and_secrets().global_catalog_url == "http://127.0.0.1:9000"

    monkeypatch.setenv("VONK_GLOBAL_CATALOG_URL", "http://catalog.example")
    with pytest.raises(SettingsError, match="global catalog URL"):
        Settings.from_env_and_secrets()

    monkeypatch.setenv("VONK_GLOBAL_CATALOG_URL", "https://user:secret@catalog.example")
    with pytest.raises(SettingsError, match="global catalog URL"):
        Settings.from_env_and_secrets()


def test_recipe_library_api_uses_only_github_or_the_internal_relay(monkeypatch) -> None:
    monkeypatch.setenv("VONK_DATABASE_URL", "postgresql://db/control")
    monkeypatch.setenv("VONK_RECIPE_LIBRARY_API_URL", "http://caddy:8083/")
    assert Settings.from_env_and_secrets().recipe_library_api_url == "http://caddy:8083"

    for invalid in ("http://api.github.com", "https://github.example"):
        monkeypatch.setenv("VONK_RECIPE_LIBRARY_API_URL", invalid)
        with pytest.raises(SettingsError, match="recipe library API URL"):
            Settings.from_env_and_secrets()


def test_management_networks_are_explicit_and_policy_validated(monkeypatch) -> None:
    monkeypatch.setenv("VONK_DATABASE_URL", "postgresql://db/control")
    monkeypatch.setenv("VONK_MANAGEMENT_CIDRS", "10.0.0.0/24,2001:db8:42::/64")
    monkeypatch.setenv("VONK_DIRECT_FABRIC_CIDRS", "10.0.0.240/28")

    settings = Settings.from_env_and_secrets()

    assert settings.management_cidrs == "10.0.0.0/24,2001:db8:42::/64"
    assert settings.direct_fabric_cidrs == "10.0.0.240/28"

    monkeypatch.setenv("VONK_MANAGEMENT_CIDRS", "10.0.0.1/24")
    with pytest.raises(SettingsError, match="canonical CIDR"):
        Settings.from_env_and_secrets()


def test_management_networks_load_from_a_protected_file(
    tmp_path: Path, monkeypatch
) -> None:
    management = tmp_path / "management-cidrs"
    management.write_text("10.0.0.0/24,2001:db8:42::/64\n")
    monkeypatch.setenv("VONK_DATABASE_URL", "postgresql://db/control")
    monkeypatch.setenv("VONK_MANAGEMENT_CIDRS_FILE", str(management))

    settings = Settings.from_env_and_secrets()

    assert settings.management_cidrs == "10.0.0.0/24,2001:db8:42::/64"


def test_management_networks_reject_env_and_file_sources_together(
    tmp_path: Path,
    monkeypatch,
) -> None:
    management = tmp_path / "management-cidrs"
    management.write_text("10.0.0.0/24\n")
    monkeypatch.setenv("VONK_DATABASE_URL", "postgresql://db/control")
    monkeypatch.setenv("VONK_MANAGEMENT_CIDRS", "10.0.0.0/24")
    monkeypatch.setenv("VONK_MANAGEMENT_CIDRS_FILE", str(management))

    with pytest.raises(SettingsError, match="cannot be combined"):
        Settings.from_env_and_secrets()


def test_management_networks_reject_symlink_file(tmp_path: Path, monkeypatch) -> None:
    management = tmp_path / "management-cidrs"
    management.write_text("10.0.0.0/24\n")
    link = tmp_path / "management-cidrs-link"
    link.symlink_to(management)
    monkeypatch.setenv("VONK_DATABASE_URL", "postgresql://db/control")
    monkeypatch.setenv("VONK_MANAGEMENT_CIDRS_FILE", str(link))

    with pytest.raises(SettingsError, match="regular non-symlink"):
        Settings.from_env_and_secrets()


def test_management_networks_reject_empty_file(tmp_path: Path, monkeypatch) -> None:
    management = tmp_path / "management-cidrs"
    management.write_text("\n")
    monkeypatch.setenv("VONK_DATABASE_URL", "postgresql://db/control")
    monkeypatch.setenv("VONK_MANAGEMENT_CIDRS_FILE", str(management))

    with pytest.raises(SettingsError, match="must not be empty"):
        Settings.from_env_and_secrets()


def test_management_network_file_rejects_overlap_with_direct_fabric(
    tmp_path: Path,
    monkeypatch,
) -> None:
    management = tmp_path / "management-cidrs"
    management.write_text("10.0.0.240/28\n")
    monkeypatch.setenv("VONK_DATABASE_URL", "postgresql://db/control")
    monkeypatch.setenv("VONK_MANAGEMENT_CIDRS_FILE", str(management))
    monkeypatch.setenv("VONK_DIRECT_FABRIC_CIDRS", "10.0.0.240/28")

    with pytest.raises(SettingsError, match="fully forbidden"):
        Settings.from_env_and_secrets()


def test_workload_tuf_roots_are_explicit_absolute_nonoverlapping_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    metadata = tmp_path / "workload-tuf/metadata"
    targets = tmp_path / "workload-tuf/targets"
    monkeypatch.setenv("VONK_DATABASE_URL", "postgresql://db/control")
    monkeypatch.setenv("VONK_WORKLOAD_TUF_METADATA_ROOT", str(metadata))
    monkeypatch.setenv("VONK_WORKLOAD_TUF_TARGET_ROOT", str(targets))

    settings = Settings.from_env_and_secrets()

    assert settings.workload_tuf_metadata_root == metadata
    assert settings.workload_tuf_target_root == targets

    monkeypatch.setenv("VONK_WORKLOAD_TUF_TARGET_ROOT", "relative/targets")
    with pytest.raises(SettingsError, match="absolute"):
        Settings.from_env_and_secrets()

    monkeypatch.setenv("VONK_WORKLOAD_TUF_TARGET_ROOT", str(metadata / "nested"))
    with pytest.raises(SettingsError, match="distinct"):
        Settings.from_env_and_secrets()


def test_development_defaults_agent_runtime_to_disabled(monkeypatch) -> None:
    monkeypatch.setenv("VONK_DATABASE_URL", "postgresql://db/control")
    monkeypatch.delenv("VONK_AGENT_RUNTIME", raising=False)

    settings = Settings.from_env_and_secrets()

    assert settings.agent_runtime == "disabled"
    assert settings.agent_proxy_auth == b""
    assert settings.worker_api_token == b""


def test_enabled_agent_runtime_loads_distinct_origins_and_public_controller_ca_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_agent_authority(
        tmp_path,
        monkeypatch,
        mode="development",
    )
    controller_ca = tmp_path / "controller-ca.pem"
    controller_ca.write_text("public controller CA\n", encoding="utf-8")
    monkeypatch.setenv("VONK_AGENT_RUNTIME", "enabled")
    monkeypatch.setenv(
        "VONK_AGENT_CONTROLLER_ORIGIN", "https://agents.example.test:8443"
    )
    monkeypatch.setenv(
        "VONK_AGENT_ENROLLMENT_ORIGIN", "https://enroll.example.test:8443"
    )
    monkeypatch.setenv("VONK_AGENT_CONTROLLER_ADDRESS", "192.168.1.231")
    monkeypatch.setenv(
        "VONK_AGENT_SERVICE_HOSTNAMES",
        "control.example.test,enroll.example.test,agents.example.test,registry.example.test",
    )
    monkeypatch.setenv("VONK_CONTROLLER_CA_FILE", str(controller_ca))
    monkeypatch.setenv("VONK_INSTALL_CHANNEL", "dev")

    settings = Settings.from_env_and_secrets()

    assert settings.agent_controller_origin == "https://agents.example.test:8443"
    assert settings.agent_enrollment_origin == "https://enroll.example.test:8443"
    assert settings.controller_ca_path == controller_ca
    assert settings.agent_ca_url == "https://step-ca:9000"
    assert settings.agent_controller_address == "192.168.1.231"
    assert settings.agent_service_hostnames == (
        "control.example.test",
        "enroll.example.test",
        "agents.example.test",
        "registry.example.test",
    )
    assert settings.install_channel == "dev"


def test_install_channel_rejects_unpublished_values(monkeypatch) -> None:
    monkeypatch.setenv("VONK_DATABASE_URL", "postgresql://db/control")
    monkeypatch.setenv("VONK_INSTALL_CHANNEL", "preview")

    with pytest.raises(SettingsError, match="VONK_INSTALL_CHANNEL"):
        Settings.from_env_and_secrets()


def test_enabled_agent_runtime_rejects_non_https_controller_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_agent_authority(
        tmp_path,
        monkeypatch,
        mode="development",
    )
    controller_ca = tmp_path / "controller-ca.pem"
    controller_ca.write_text("public controller CA\n", encoding="utf-8")
    monkeypatch.setenv("VONK_AGENT_RUNTIME", "enabled")
    monkeypatch.setenv("VONK_AGENT_CONTROLLER_ORIGIN", "http://agents.example.test")
    monkeypatch.setenv("VONK_AGENT_ENROLLMENT_ORIGIN", "https://enroll.example.test")
    monkeypatch.setenv("VONK_CONTROLLER_CA_FILE", str(controller_ca))

    with pytest.raises(SettingsError, match="fixed HTTPS origin"):
        Settings.from_env_and_secrets()


def _configure_agent_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str,
) -> dict[str, Path]:
    secret_values = {
        "VONK_DATABASE_URL_FILE": "postgresql://db/control\n",
        "VONK_TOKEN_SIGNING_KEY_FILE": "k" * 32,
        "VONK_METRICS_TOKEN_FILE": "m" * 16,
        "VONK_AGENT_CLIENT_CA_FILE": "client-ca",
        "VONK_AGENT_INTERMEDIATE_CERTIFICATE_FILE": "intermediate-certificate",
        "VONK_AGENT_CA_CREDENTIAL_FILE": "provider-credential",
        "VONK_AGENT_CA_PROVISIONER_PUBLIC_JWK_FILE": "provider-public-jwk",
        "VONK_AGENT_CA_ROOT_FILE": "root-certificate",
        "VONK_CONTROLLER_CA_FILE": "public-controller-ca",
        "VONK_AGENT_PROXY_AUTH_FILE": "p" * 32,
        "VONK_WORKER_API_TOKEN_FILE": "w" * 32,
    }
    paths: dict[str, Path] = {}
    for name, value in secret_values.items():
        path = tmp_path / name
        path.write_text(value)
        paths[name] = path
    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", mode)
    monkeypatch.setenv("VONK_MANAGEMENT_CIDRS", "10.0.0.0/24")
    monkeypatch.setenv(
        "VONK_AGENT_CONTROLLER_ORIGIN", "https://agents.example.test:8443"
    )
    monkeypatch.setenv(
        "VONK_AGENT_ENROLLMENT_ORIGIN", "https://enroll.example.test:8443"
    )
    monkeypatch.setenv("VONK_CONTROLLER_CA_FILE", str(paths["VONK_CONTROLLER_CA_FILE"]))
    if mode == "production":
        for name in (
            "VONK_DATABASE_URL_FILE",
            "VONK_TOKEN_SIGNING_KEY_FILE",
            "VONK_METRICS_TOKEN_FILE",
        ):
            monkeypatch.setenv(name, str(paths[name]))
    else:
        monkeypatch.setenv("VONK_DATABASE_URL", "postgresql://db/control")
        monkeypatch.setenv(
            "VONK_TOKEN_SIGNING_KEY_FILE",
            str(paths["VONK_TOKEN_SIGNING_KEY_FILE"]),
        )
    for name in (
        "VONK_AGENT_CLIENT_CA_FILE",
        "VONK_AGENT_INTERMEDIATE_CERTIFICATE_FILE",
        "VONK_AGENT_CA_CREDENTIAL_FILE",
        "VONK_AGENT_CA_PROVISIONER_PUBLIC_JWK_FILE",
        "VONK_AGENT_CA_ROOT_FILE",
        "VONK_AGENT_PROXY_AUTH_FILE",
        "VONK_WORKER_API_TOKEN_FILE",
    ):
        monkeypatch.setenv(name, str(paths[name]))
    monkeypatch.setenv("VONK_AGENT_CA_URL", "https://step-ca:9000")
    monkeypatch.setenv("VONK_AGENT_CA_PROVISIONER_NAME", "vonk-forge-agent")
    monkeypatch.setenv("VONK_AGENT_CA_PROVISIONER_KID", "test-kid")
    return paths


@pytest.mark.parametrize(
    ("mode", "runtime"),
    [
        ("development", "disabled"),
        ("development", "enabled"),
        ("production", "enabled"),
    ],
)
def test_agent_authority_mode_runtime_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    runtime: str,
) -> None:
    _configure_agent_authority(
        tmp_path,
        monkeypatch,
        mode=mode,
    )
    monkeypatch.setenv("VONK_AGENT_RUNTIME", runtime)

    settings = Settings.from_env_and_secrets()

    assert settings.agent_runtime == runtime
    if runtime == "disabled":
        assert settings.agent_proxy_auth == b""
        assert settings.worker_api_token == b""
        return
    assert settings.agent_client_ca == b"client-ca"
    assert settings.agent_intermediate_certificate == b"intermediate-certificate"
    assert settings.agent_proxy_auth == b"p" * 32
    assert settings.worker_api_token == b"w" * 32


def test_agent_certificate_lifetime_defaults_and_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_agent_authority(tmp_path, monkeypatch, mode="development")
    monkeypatch.setenv("VONK_AGENT_RUNTIME", "enabled")

    assert (
        Settings.from_env_and_secrets().agent_ca_certificate_lifetime_seconds == 86400
    )

    monkeypatch.setenv("VONK_AGENT_CA_CERTIFICATE_LIFETIME_SECONDS", "90")
    assert Settings.from_env_and_secrets().agent_ca_certificate_lifetime_seconds == 90

    for value in ("not-a-number", "89", "86401"):
        monkeypatch.setenv("VONK_AGENT_CA_CERTIFICATE_LIFETIME_SECONDS", value)
        with pytest.raises(SettingsError, match="certificate lifetime") as caught:
            Settings.from_env_and_secrets()
        assert value not in str(caught.value)


def test_enabled_development_agent_authority_requires_management_cidrs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_agent_authority(
        tmp_path,
        monkeypatch,
        mode="development",
    )
    monkeypatch.delenv("VONK_MANAGEMENT_CIDRS")
    monkeypatch.setenv("VONK_AGENT_RUNTIME", "enabled")

    with pytest.raises(SettingsError, match="VONK_MANAGEMENT_CIDRS"):
        Settings.from_env_and_secrets()


def test_enabled_development_agent_authority_requires_token_signing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_agent_authority(
        tmp_path,
        monkeypatch,
        mode="development",
    )
    monkeypatch.delenv("VONK_TOKEN_SIGNING_KEY_FILE")
    monkeypatch.setenv("VONK_AGENT_RUNTIME", "enabled")

    with pytest.raises(SettingsError, match="VONK_TOKEN_SIGNING_KEY_FILE"):
        Settings.from_env_and_secrets()


def test_production_agent_runtime_requires_management_networks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "database-url"
    database.write_text("postgresql://db/control")
    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("VONK_DATABASE_URL_FILE", str(database))

    with pytest.raises(SettingsError, match="VONK_MANAGEMENT_CIDRS"):
        Settings.from_env_and_secrets()


def test_production_rejects_raw_database_secret(monkeypatch) -> None:
    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("VONK_MANAGEMENT_CIDRS", "10.0.0.0/24")
    monkeypatch.setenv("VONK_DATABASE_URL", "postgresql://unsafe")
    with pytest.raises(SettingsError, match="secret file"):
        Settings.from_env_and_secrets()


def test_secret_file_must_not_be_symlink(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "actual"
    target.write_text("postgresql://db/control")
    link = tmp_path / "database-url"
    link.symlink_to(target)
    monkeypatch.setenv("VONK_DATABASE_URL_FILE", str(link))
    with pytest.raises(SettingsError, match="regular non-symlink"):
        Settings.from_env_and_secrets()


def test_compose_is_platform_neutral_and_only_caddy_publishes_ports() -> None:
    root = Path(__file__).resolve().parents[2]
    text = (root / "deploy/compose/compose.yaml").read_text()
    assert "ugreen" not in text.lower()
    assert "192.168." not in text
    assert "node1" not in text.lower() and "node2" not in text.lower()
    assert text.count("ports:") == 1
    assert "control-api:" in text and "control-worker:" in text
    assert "postgres:" in text and "caddy:" in text


def _configure_enrollment_bootstrap_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller_ca = tmp_path / "controller-ca.pem"
    controller_ca.write_text("public controller CA\n", encoding="utf-8")
    monkeypatch.setenv(
        "VONK_AGENT_CONTROLLER_ORIGIN", "https://agents.example.test:8443"
    )
    monkeypatch.setenv(
        "VONK_AGENT_ENROLLMENT_ORIGIN", "https://enroll.example.test:8443"
    )
    monkeypatch.setenv("VONK_CONTROLLER_CA_FILE", str(controller_ca))


def test_production_agent_boundary_requires_secret_files_and_step_ca(
    tmp_path: Path, monkeypatch
) -> None:
    values = {
        "VONK_DATABASE_URL_FILE": "postgresql://db/control",
        "VONK_TOKEN_SIGNING_KEY_FILE": "k" * 32,
        "VONK_METRICS_TOKEN_FILE": "m" * 16,
        "VONK_AGENT_CLIENT_CA_FILE": "client-ca",
        "VONK_AGENT_INTERMEDIATE_CERTIFICATE_FILE": "intermediate-certificate",
        "VONK_AGENT_CA_CREDENTIAL_FILE": "provider-credential",
        "VONK_AGENT_CA_PROVISIONER_PUBLIC_JWK_FILE": "provider-public-jwk",
        "VONK_AGENT_CA_ROOT_FILE": "root-certificate",
        "VONK_AGENT_PROXY_AUTH_FILE": "p" * 32 + "\r\n",
        "VONK_WORKER_API_TOKEN_FILE": "w" * 32,
    }
    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("VONK_MANAGEMENT_CIDRS", "10.0.0.0/24")
    for name, value in values.items():
        path = tmp_path / name
        path.write_text(value)
        monkeypatch.setenv(name, str(path))
    monkeypatch.setenv("VONK_AGENT_CA_URL", "https://step-ca:9000")
    monkeypatch.setenv("VONK_AGENT_CA_PROVISIONER_NAME", "vonk-forge-agent")
    monkeypatch.setenv("VONK_AGENT_CA_PROVISIONER_KID", "test-kid")
    _configure_enrollment_bootstrap_environment(tmp_path, monkeypatch)

    settings = Settings.from_env_and_secrets()

    assert settings.agent_proxy_auth == ("p" * 32).encode()


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
def test_production_rejects_noncanonical_agent_proxy_auth(
    tmp_path: Path,
    monkeypatch,
    proxy_auth: str,
) -> None:
    values = {
        "VONK_DATABASE_URL_FILE": "postgresql://db/control",
        "VONK_TOKEN_SIGNING_KEY_FILE": "k" * 32,
        "VONK_METRICS_TOKEN_FILE": "m" * 16,
        "VONK_AGENT_CLIENT_CA_FILE": "client-ca",
        "VONK_AGENT_INTERMEDIATE_CERTIFICATE_FILE": "intermediate-certificate",
        "VONK_AGENT_CA_CREDENTIAL_FILE": "provider-credential",
        "VONK_AGENT_CA_PROVISIONER_PUBLIC_JWK_FILE": "provider-public-jwk",
        "VONK_AGENT_CA_ROOT_FILE": "root-certificate",
        "VONK_AGENT_PROXY_AUTH_FILE": proxy_auth,
    }
    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("VONK_MANAGEMENT_CIDRS", "10.0.0.0/24")
    monkeypatch.setenv("VONK_AGENT_CA_URL", "https://step-ca:9000")
    monkeypatch.setenv("VONK_AGENT_CA_PROVISIONER_NAME", "vonk-forge-agent")
    monkeypatch.setenv("VONK_AGENT_CA_PROVISIONER_KID", "test-kid")
    _configure_enrollment_bootstrap_environment(tmp_path, monkeypatch)
    for name, value in values.items():
        path = tmp_path / name
        path.write_text(value)
        monkeypatch.setenv(name, str(path))

    with pytest.raises(SettingsError, match="base64url-like"):
        Settings.from_env_and_secrets()


def test_agent_proxy_auth_defaults_empty(monkeypatch) -> None:
    monkeypatch.setenv("VONK_DATABASE_URL", "postgresql://db/control")
    settings = Settings.from_env_and_secrets()
    assert settings.agent_proxy_auth == b""


def test_production_worker_settings_can_explicitly_disable_agent_runtime(
    tmp_path: Path, monkeypatch
) -> None:
    values = {
        "VONK_DATABASE_URL_FILE": "postgresql://db/control",
        "VONK_WORKER_API_TOKEN_FILE": "w" * 32,
    }
    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", "production")
    for name, value in values.items():
        path = tmp_path / name
        path.write_text(value)
        monkeypatch.setenv(name, str(path))

    with pytest.raises(SettingsError, match="VONK_MANAGEMENT_CIDRS"):
        WorkerSettings.from_env_and_secrets()

    monkeypatch.setenv("VONK_MANAGEMENT_CIDRS", "10.0.0.0/24")
    settings = WorkerSettings.from_env_and_secrets()

    assert settings.internal_api_token == b"w" * 32
    assert settings.management_cidrs == "10.0.0.0/24"
