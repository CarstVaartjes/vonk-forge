from pathlib import Path

import pytest
from vonk_control import dev_cohort
from vonk_control.dev_cohort import build_identity, verify_cohort
from vonk_control.settings import (
    GenerationStartupSettings,
    Settings,
    SettingsError,
    StartupMode,
    WorkerSettings,
)

GENERATION_SHA_A = "a" * 64
GENERATION_SHA_B = "b" * 64
GENERATION_VARIABLES = (
    "VONK_CONTROL_GENERATION_ID",
    "VONK_PLATFORM_RELEASE_DIGEST",
    "VONK_PLATFORM_BUILD_DIGEST",
    "VONK_PLATFORM_VERSION",
    "VONK_CONTROL_PROCESS_IMAGE",
    "VONK_DATABASE_REVISION",
    "VONK_CONTROL_START_NONCE",
)


def _cohort_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    role: str,
    selected_commit: str = "d" * 40,
    embedded_commit: str | None = None,
    embedded_role: str | None = None,
):
    selected = verify_cohort(
        [
            build_identity(role="api", source_commit=selected_commit),
            build_identity(role="worker", source_commit=selected_commit),
        ]
    )
    cohort_root = tmp_path / "cohort"
    cohort_root.mkdir()
    selected_path = cohort_root / "selected.json"
    selected_path.write_bytes(selected.to_bytes())
    identity_path = tmp_path / "development-image-identity.json"
    identity_path.write_bytes(
        build_identity(
            role=embedded_role or role,
            source_commit=embedded_commit or selected_commit,
        ).to_bytes()
    )
    monkeypatch.setattr(
        dev_cohort,
        "DEVELOPMENT_IMAGE_IDENTITY_PATH",
        identity_path,
    )
    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", "development")
    monkeypatch.setenv("VONK_CONTROL_STARTUP_MODE", "selected")
    monkeypatch.setenv("VONK_DATABASE_URL", "postgresql://db/control")
    monkeypatch.delenv("VONK_DATABASE_URL_FILE", raising=False)
    monkeypatch.setenv("VONK_CONTROL_IDENTITY_ROOT", str(tmp_path / "identity"))
    monkeypatch.setenv("VONK_DEV_SELECTED_COHORT_FILE", str(selected_path))
    monkeypatch.setenv("VONK_CONTROL_PROCESS_ROLE", role)
    for name in GENERATION_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    return selected


def _valid_generation_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: StartupMode,
) -> None:
    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("VONK_DATABASE_URL", "postgresql+psycopg://control:test@db/control")
    monkeypatch.delenv("VONK_DATABASE_URL_FILE", raising=False)
    monkeypatch.setenv("VONK_CONTROL_STARTUP_MODE", mode.value)
    if mode is StartupMode.PRESELECTION:
        monkeypatch.setenv("VONK_CONTROL_OPERATION_ID", "operation-1")
    else:
        monkeypatch.delenv("VONK_CONTROL_OPERATION_ID", raising=False)
    monkeypatch.setenv("VONK_CONTROL_GENERATION_ID", "gen-a")
    monkeypatch.setenv(
        "VONK_PLATFORM_RELEASE_DIGEST",
        f"sha256:{GENERATION_SHA_A}",
    )
    monkeypatch.setenv(
        "VONK_PLATFORM_BUILD_DIGEST",
        f"sha256:{GENERATION_SHA_B}",
    )
    monkeypatch.setenv("VONK_PLATFORM_VERSION", "1.2.0")
    monkeypatch.setenv(
        "VONK_CONTROL_PROCESS_IMAGE",
        f"ghcr.io/example/control-api@sha256:{GENERATION_SHA_A}",
    )
    monkeypatch.setenv("VONK_DATABASE_REVISION", "0012_control_process_heartbeats")
    monkeypatch.setenv("VONK_CONTROL_START_NONCE", "c" * 64)
    monkeypatch.setenv("VONK_CONTROL_IDENTITY_ROOT", str(tmp_path / "identity"))


def test_generation_startup_operation_is_required_only_during_preselection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _valid_generation_environment(
        tmp_path,
        monkeypatch,
        mode=StartupMode.PRESELECTION,
    )
    monkeypatch.delenv("VONK_CONTROL_OPERATION_ID")
    with pytest.raises(SettingsError, match="required for preselection"):
        GenerationStartupSettings.from_env_and_secrets()

    monkeypatch.setenv("VONK_CONTROL_STARTUP_MODE", StartupMode.SELECTED.value)
    monkeypatch.setenv("VONK_CONTROL_OPERATION_ID", "operation-1")
    with pytest.raises(SettingsError, match="forbidden in selected mode"):
        GenerationStartupSettings.from_env_and_secrets()


@pytest.mark.parametrize(
    ("environment_name", "invalid_value", "message"),
    (
        (
            "VONK_PLATFORM_RELEASE_DIGEST",
            f"sha256:{'A' * 64}",
            "VONK_PLATFORM_RELEASE_DIGEST",
        ),
        (
            "VONK_PLATFORM_BUILD_DIGEST",
            f"sha512:{GENERATION_SHA_B}",
            "VONK_PLATFORM_BUILD_DIGEST",
        ),
        (
            "VONK_CONTROL_PROCESS_IMAGE",
            "ghcr.io/example/control-api:latest",
            "VONK_CONTROL_PROCESS_IMAGE",
        ),
        ("VONK_CONTROL_START_NONCE", "not-a-nonce", "VONK_CONTROL_START_NONCE"),
        ("VONK_AGENT_PROTOCOL_MAXIMUM", "not-an-integer", "agent protocol range"),
        ("VONK_CONTROL_IDENTITY_ROOT", "relative/identity", "absolute normalized"),
    ),
)
def test_generation_startup_rejects_invalid_identity_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment_name: str,
    invalid_value: str,
    message: str,
) -> None:
    _valid_generation_environment(
        tmp_path,
        monkeypatch,
        mode=StartupMode.SELECTED,
    )
    monkeypatch.setenv(environment_name, invalid_value)

    with pytest.raises(SettingsError, match=message):
        GenerationStartupSettings.from_env_and_secrets()


@pytest.mark.parametrize("deployment_mode", ("test", "production"))
@pytest.mark.parametrize("missing_name", GENERATION_VARIABLES)
def test_production_and_test_generation_startup_still_require_explicit_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deployment_mode: str,
    missing_name: str,
) -> None:
    _valid_generation_environment(
        tmp_path,
        monkeypatch,
        mode=StartupMode.SELECTED,
    )
    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", deployment_mode)
    if deployment_mode == "production":
        database = tmp_path / "database-url"
        database.write_text("postgresql://db/control\n")
        monkeypatch.setenv("VONK_DATABASE_URL_FILE", str(database))
        monkeypatch.delenv("VONK_DATABASE_URL", raising=False)
    monkeypatch.delenv(missing_name)

    with pytest.raises(SettingsError, match=missing_name):
        GenerationStartupSettings.from_env_and_secrets()


def test_development_without_a_cohort_keeps_explicit_local_source_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _valid_generation_environment(
        tmp_path,
        monkeypatch,
        mode=StartupMode.SELECTED,
    )
    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", "development")
    monkeypatch.delenv("VONK_DEV_SELECTED_COHORT_FILE", raising=False)

    settings = GenerationStartupSettings.from_env_and_secrets()

    assert settings.generation_id == "gen-a"
    assert settings.process_image == (
        f"ghcr.io/example/control-api@sha256:{GENERATION_SHA_A}"
    )
    assert (settings.protocol_minimum, settings.protocol_maximum) == (1, 1)


@pytest.mark.parametrize("role", ("api", "worker"))
def test_development_generation_startup_derives_role_identity_from_verified_cohort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    selected = _cohort_environment(
        tmp_path,
        monkeypatch,
        role=role,
    )

    settings = GenerationStartupSettings.from_env_and_secrets()

    assert settings.generation_id == selected.generation_id
    assert settings.release_digest == selected.release_digest
    assert settings.build_digest == selected.build_digest
    assert settings.platform_version == selected.platform_version
    assert settings.database_revision == selected.database_revision
    assert settings.start_nonce == selected.start_nonce
    assert settings.protocol_minimum == selected.protocol_minimum
    assert settings.protocol_maximum == selected.protocol_maximum
    assert settings.process_image == (
        selected.api_image if role == "api" else selected.worker_image
    )


@pytest.mark.parametrize(
    ("role", "embedded_role", "message"),
    (
        ("", "api", "VONK_CONTROL_PROCESS_ROLE"),
        ("signer", "api", "VONK_CONTROL_PROCESS_ROLE"),
        ("worker", "api", "selected cohort"),
    ),
    ids=("missing", "invalid", "embedded-role-mismatch"),
)
def test_cohort_generation_startup_requires_matching_api_or_worker_role(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    embedded_role: str,
    message: str,
) -> None:
    _cohort_environment(
        tmp_path,
        monkeypatch,
        role=role or "api",
        embedded_role=embedded_role,
    )
    if role:
        monkeypatch.setenv("VONK_CONTROL_PROCESS_ROLE", role)
    else:
        monkeypatch.delenv("VONK_CONTROL_PROCESS_ROLE")

    with pytest.raises(SettingsError, match=message):
        GenerationStartupSettings.from_env_and_secrets()


def test_cohort_generation_startup_requires_selected_startup_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _cohort_environment(tmp_path, monkeypatch, role="api")
    monkeypatch.setenv("VONK_CONTROL_STARTUP_MODE", "preselection")
    monkeypatch.setenv("VONK_CONTROL_OPERATION_ID", "operation-1")

    with pytest.raises(SettingsError, match="selected startup mode"):
        GenerationStartupSettings.from_env_and_secrets()


@pytest.mark.parametrize("explicit_name", GENERATION_VARIABLES)
def test_cohort_generation_startup_rejects_every_explicit_identity_variable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit_name: str,
) -> None:
    _cohort_environment(tmp_path, monkeypatch, role="api")
    monkeypatch.setenv(explicit_name, "explicit-conflict")

    with pytest.raises(SettingsError, match="cannot be combined"):
        GenerationStartupSettings.from_env_and_secrets()


@pytest.mark.parametrize("deployment_mode", ("test", "production"))
def test_non_development_generation_startup_rejects_cohort_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deployment_mode: str,
) -> None:
    _cohort_environment(tmp_path, monkeypatch, role="api")
    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", deployment_mode)
    if deployment_mode == "production":
        database = tmp_path / "database-url"
        database.write_text("postgresql://db/control\n")
        monkeypatch.setenv("VONK_DATABASE_URL_FILE", str(database))
        monkeypatch.delenv("VONK_DATABASE_URL", raising=False)

    with pytest.raises(SettingsError, match="development"):
        GenerationStartupSettings.from_env_and_secrets()


def test_database_secret_is_read_from_file(tmp_path: Path, monkeypatch) -> None:
    secret = tmp_path / "database-url"
    secret.write_text("postgresql+psycopg://control:pw@postgres/control\n")
    monkeypatch.setenv("VONK_DATABASE_URL_FILE", str(secret))
    settings = Settings.from_env_and_secrets()
    assert settings.database_host == "postgres"
    assert settings.repository_path == Path("/srv/vonk-forge/repository")
    assert settings.global_catalog_url == "https://vonkforge.ai"


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


def test_management_networks_load_from_a_protected_file(tmp_path: Path, monkeypatch) -> None:
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


def test_platform_tuf_roots_are_explicit_absolute_nonoverlapping_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    metadata = tmp_path / "platform-tuf/metadata"
    targets = tmp_path / "platform-tuf/targets"
    monkeypatch.setenv("VONK_DATABASE_URL", "postgresql://db/control")
    monkeypatch.setenv("VONK_AGENT_TUF_METADATA_ROOT", str(metadata))
    monkeypatch.setenv("VONK_AGENT_TUF_TARGET_ROOT", str(targets))

    settings = Settings.from_env_and_secrets()

    assert settings.agent_tuf_metadata_root == metadata
    assert settings.agent_tuf_target_root == targets

    monkeypatch.setenv("VONK_AGENT_TUF_TARGET_ROOT", "relative/targets")
    with pytest.raises(SettingsError, match="absolute"):
        Settings.from_env_and_secrets()

    monkeypatch.setenv("VONK_AGENT_TUF_TARGET_ROOT", str(metadata / "nested"))
    with pytest.raises(SettingsError, match="distinct"):
        Settings.from_env_and_secrets()


def test_agent_tuf_target_root_default_is_not_concatenated(monkeypatch) -> None:
    monkeypatch.setenv("VONK_DATABASE_URL", "postgresql://db/control")

    settings = Settings.from_env_and_secrets()

    assert settings.agent_tuf_target_root == Path("/state/agent-tuf/targets")


def test_development_defaults_agent_runtime_to_disabled(monkeypatch) -> None:
    monkeypatch.setenv("VONK_DATABASE_URL", "postgresql://db/control")
    monkeypatch.delenv("VONK_AGENT_RUNTIME", raising=False)

    settings = Settings.from_env_and_secrets()

    assert settings.agent_runtime == "disabled"
    assert settings.agent_proxy_auth == b""
    assert settings.worker_api_token == b""


def _configure_agent_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str,
    provider: str,
) -> dict[str, Path]:
    secret_values = {
        "VONK_DATABASE_URL_FILE": "postgresql://db/control\n",
        "VONK_TOKEN_SIGNING_KEY_FILE": "k" * 32,
        "VONK_METRICS_TOKEN_FILE": "m" * 16,
        "VONK_GIT_SIGNING_KEY_FILE": "git-key",
        "VONK_ADMIN_GRANT_PRIVATE_KEY_FILE": "admin-grant-key",
        "VONK_AGENT_CLIENT_CA_FILE": "client-ca",
        "VONK_AGENT_INTERMEDIATE_CERTIFICATE_FILE": "intermediate-certificate",
        "VONK_AGENT_INTERMEDIATE_KEY_FILE": "built-in-key",
        "VONK_AGENT_CA_CREDENTIAL_FILE": "provider-credential",
        "VONK_AGENT_CA_PROVISIONER_PUBLIC_JWK_FILE": "provider-public-jwk",
        "VONK_AGENT_CA_ROOT_FILE": "root-certificate",
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
    if mode == "production":
        for name in (
            "VONK_DATABASE_URL_FILE",
            "VONK_TOKEN_SIGNING_KEY_FILE",
            "VONK_METRICS_TOKEN_FILE",
            "VONK_GIT_SIGNING_KEY_FILE",
            "VONK_ADMIN_GRANT_PRIVATE_KEY_FILE",
        ):
            monkeypatch.setenv(name, str(paths[name]))
    else:
        monkeypatch.setenv("VONK_DATABASE_URL", "postgresql://db/control")
        monkeypatch.setenv(
            "VONK_TOKEN_SIGNING_KEY_FILE",
            str(paths["VONK_TOKEN_SIGNING_KEY_FILE"]),
        )
    if provider == "builtin":
        for name in (
            "VONK_AGENT_CLIENT_CA_FILE",
            "VONK_AGENT_INTERMEDIATE_CERTIFICATE_FILE",
            "VONK_AGENT_INTERMEDIATE_KEY_FILE",
            "VONK_AGENT_PROXY_AUTH_FILE",
            "VONK_WORKER_API_TOKEN_FILE",
        ):
            monkeypatch.setenv(name, str(paths[name]))
    elif provider == "step-ca":
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
    ("mode", "runtime", "provider", "bootstrap", "accepted"),
    [
        ("development", "disabled", "", "", True),
        ("development", "enabled", "builtin", "1", True),
        ("development", "enabled", "step-ca", "", False),
        ("development", "enabled", "", "", False),
        ("production", "enabled", "step-ca", "", True),
        ("production", "enabled", "builtin", "1", True),
        ("production", "enabled", "", "", False),
    ],
)
def test_agent_authority_mode_runtime_provider_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    runtime: str,
    provider: str,
    bootstrap: str,
    accepted: bool,
) -> None:
    paths = _configure_agent_authority(
        tmp_path,
        monkeypatch,
        mode=mode,
        provider=provider,
    )
    monkeypatch.setenv("VONK_AGENT_RUNTIME", runtime)
    if provider:
        monkeypatch.setenv("VONK_AGENT_CA_PROVIDER", provider)
    if bootstrap:
        monkeypatch.setenv("VONK_AGENT_BUILTIN_CA_BOOTSTRAP", bootstrap)

    if not accepted:
        with pytest.raises(SettingsError):
            Settings.from_env_and_secrets()
        return

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
    if provider == "builtin":
        assert settings.agent_intermediate_key_path == paths[
            "VONK_AGENT_INTERMEDIATE_KEY_FILE"
        ]


def test_enabled_development_agent_authority_requires_management_cidrs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_agent_authority(
        tmp_path,
        monkeypatch,
        mode="development",
        provider="builtin",
    )
    monkeypatch.delenv("VONK_MANAGEMENT_CIDRS")
    monkeypatch.setenv("VONK_AGENT_RUNTIME", "enabled")
    monkeypatch.setenv("VONK_AGENT_CA_PROVIDER", "builtin")
    monkeypatch.setenv("VONK_AGENT_BUILTIN_CA_BOOTSTRAP", "1")

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
        provider="builtin",
    )
    monkeypatch.delenv("VONK_TOKEN_SIGNING_KEY_FILE")
    monkeypatch.setenv("VONK_AGENT_RUNTIME", "enabled")
    monkeypatch.setenv("VONK_AGENT_CA_PROVIDER", "builtin")
    monkeypatch.setenv("VONK_AGENT_BUILTIN_CA_BOOTSTRAP", "1")

    with pytest.raises(SettingsError, match="VONK_TOKEN_SIGNING_KEY_FILE"):
        Settings.from_env_and_secrets()


def test_production_agent_runtime_requires_management_networks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "database-url"
    database.write_text("postgresql://db/control")
    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("VONK_AGENT_CA_PROVIDER", "step-ca")
    monkeypatch.setenv("VONK_DATABASE_URL_FILE", str(database))

    with pytest.raises(SettingsError, match="VONK_MANAGEMENT_CIDRS"):
        Settings.from_env_and_secrets()


def test_production_rejects_raw_database_secret(monkeypatch) -> None:
    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("VONK_MANAGEMENT_CIDRS", "10.0.0.0/24")
    monkeypatch.setenv("VONK_AGENT_CA_PROVIDER", "step-ca")
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


def test_git_policy_configuration_uses_key_reference_and_unique_checks(tmp_path: Path, monkeypatch) -> None:
    key = tmp_path / "signing-key"
    key.write_text("fixture")
    monkeypatch.setenv("VONK_DATABASE_URL", "postgresql://db/control")
    monkeypatch.setenv("VONK_GIT_SIGNING_KEY_FILE", str(key))
    monkeypatch.setenv("VONK_DEPLOYMENT_BRANCH", "deploy")
    monkeypatch.setenv("VONK_REQUIRED_CHECKS", "tests,security")
    settings = Settings.from_env_and_secrets()
    assert settings.git_signing_key_path == key
    assert settings.required_checks == ("tests", "security")

    monkeypatch.setenv("VONK_REQUIRED_CHECKS", "tests,tests")
    with pytest.raises(SettingsError, match="unique"):
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


def test_production_agent_boundary_requires_secret_files_and_step_ca(tmp_path: Path, monkeypatch) -> None:
    values = {
        "VONK_DATABASE_URL_FILE": "postgresql://db/control",
        "VONK_TOKEN_SIGNING_KEY_FILE": "k" * 32,
        "VONK_METRICS_TOKEN_FILE": "m" * 16,
        "VONK_GIT_SIGNING_KEY_FILE": "git-key",
        "VONK_AGENT_CLIENT_CA_FILE": "client-ca",
        "VONK_AGENT_INTERMEDIATE_CERTIFICATE_FILE": "intermediate-certificate",
        "VONK_AGENT_CA_CREDENTIAL_FILE": "provider-credential",
        "VONK_AGENT_CA_PROVISIONER_PUBLIC_JWK_FILE": "provider-public-jwk",
        "VONK_AGENT_CA_ROOT_FILE": "root-certificate",
        "VONK_AGENT_PROXY_AUTH_FILE": "p" * 32 + "\r\n",
        "VONK_WORKER_API_TOKEN_FILE": "w" * 32,
        "VONK_AGENT_UPDATE_AUTHORITY_KEY_FILE": "fixture-update-authority-key",
        "VONK_ADMIN_GRANT_PRIVATE_KEY_FILE": "fixture-admin-grant-key",
    }
    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("VONK_MANAGEMENT_CIDRS", "10.0.0.0/24")
    for name, value in values.items():
        path = tmp_path / name
        path.write_text(value)
        monkeypatch.setenv(name, str(path))
    monkeypatch.setenv("VONK_AGENT_CA_PROVIDER", "step-ca")
    monkeypatch.setenv("VONK_AGENT_CA_URL", "https://step-ca:9000")
    monkeypatch.setenv("VONK_AGENT_CA_PROVISIONER_NAME", "vonk-forge-agent")
    monkeypatch.setenv("VONK_AGENT_CA_PROVISIONER_KID", "test-kid")

    settings = Settings.from_env_and_secrets()

    assert settings.agent_ca_provider == "step-ca"
    assert settings.agent_proxy_auth == ("p" * 32).encode()
    assert settings.admin_grant_private_key_path == (
        tmp_path / "VONK_ADMIN_GRANT_PRIVATE_KEY_FILE"
    )


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
        "VONK_GIT_SIGNING_KEY_FILE": "git-key",
        "VONK_AGENT_CLIENT_CA_FILE": "client-ca",
        "VONK_AGENT_INTERMEDIATE_CERTIFICATE_FILE": "intermediate-certificate",
        "VONK_AGENT_CA_CREDENTIAL_FILE": "provider-credential",
        "VONK_AGENT_CA_PROVISIONER_PUBLIC_JWK_FILE": "provider-public-jwk",
        "VONK_AGENT_CA_ROOT_FILE": "root-certificate",
        "VONK_AGENT_PROXY_AUTH_FILE": proxy_auth,
    }
    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("VONK_MANAGEMENT_CIDRS", "10.0.0.0/24")
    monkeypatch.setenv("VONK_AGENT_CA_PROVIDER", "step-ca")
    monkeypatch.setenv("VONK_AGENT_CA_URL", "https://step-ca:9000")
    monkeypatch.setenv("VONK_AGENT_CA_PROVISIONER_NAME", "vonk-forge-agent")
    monkeypatch.setenv("VONK_AGENT_CA_PROVISIONER_KID", "test-kid")
    for name, value in values.items():
        path = tmp_path / name
        path.write_text(value)
        monkeypatch.setenv(name, str(path))

    with pytest.raises(SettingsError, match="base64url-like"):
        Settings.from_env_and_secrets()


@pytest.mark.parametrize(
    ("provider", "conflicting_environment"),
    (
        ("builtin", {"VONK_AGENT_CA_CREDENTIAL_FILE": "/run/secrets/agent-ca-credential"}),
        ("step-ca", {"VONK_AGENT_BUILTIN_CA_BOOTSTRAP": "1"}),
        ("step-ca", {"VONK_AGENT_INTERMEDIATE_KEY_FILE": "/run/secrets/agent-intermediate-key"}),
    ),
)
def test_agent_ca_provider_rejects_other_provider_settings(
    monkeypatch,
    provider: str,
    conflicting_environment: dict[str, str],
) -> None:
    monkeypatch.setenv("VONK_DATABASE_URL", "postgresql://db/control")
    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("VONK_AGENT_CA_PROVIDER", provider)
    if provider == "builtin":
        monkeypatch.setenv("VONK_AGENT_BUILTIN_CA_BOOTSTRAP", "1")
    for name, value in conflicting_environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(SettingsError, match="CA provider settings cannot be combined"):
        Settings.from_env_and_secrets()


def test_agent_proxy_auth_defaults_empty_and_production_rejects_builtin_ca(monkeypatch) -> None:
    monkeypatch.setenv("VONK_DATABASE_URL", "postgresql://db/control")
    settings = Settings.from_env_and_secrets()
    assert settings.agent_proxy_auth == b""

    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("VONK_MANAGEMENT_CIDRS", "10.0.0.0/24")
    monkeypatch.setenv("VONK_AGENT_CA_PROVIDER", "builtin")
    with pytest.raises(SettingsError, match="explicit bootstrap"):
        Settings.from_env_and_secrets()


def test_production_builtin_bootstrap_requires_and_loads_the_mounted_intermediate_key(tmp_path: Path, monkeypatch) -> None:
    values = {
        "VONK_DATABASE_URL_FILE": "postgresql://db/control",
        "VONK_TOKEN_SIGNING_KEY_FILE": "k" * 32,
        "VONK_METRICS_TOKEN_FILE": "m" * 16,
        "VONK_GIT_SIGNING_KEY_FILE": "git-key",
        "VONK_AGENT_CLIENT_CA_FILE": "client-ca",
        "VONK_AGENT_INTERMEDIATE_CERTIFICATE_FILE": "intermediate-certificate",
        "VONK_AGENT_INTERMEDIATE_KEY_FILE": "built-in-key",
        "VONK_AGENT_PROXY_AUTH_FILE": "p" * 32,
        "VONK_WORKER_API_TOKEN_FILE": "w" * 32,
        "VONK_AGENT_UPDATE_AUTHORITY_KEY_FILE": "fixture-update-authority-key",
        "VONK_ADMIN_GRANT_PRIVATE_KEY_FILE": "fixture-admin-grant-key",
    }
    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("VONK_MANAGEMENT_CIDRS", "10.0.0.0/24")
    monkeypatch.setenv("VONK_AGENT_CA_PROVIDER", "builtin")
    monkeypatch.setenv("VONK_AGENT_BUILTIN_CA_BOOTSTRAP", "1")
    for name, value in values.items():
        path = tmp_path / name
        path.write_text(value)
        monkeypatch.setenv(name, str(path))

    settings = Settings.from_env_and_secrets()

    assert settings.agent_ca_provider == "builtin"
    assert settings.agent_intermediate_key_path == tmp_path / "VONK_AGENT_INTERMEDIATE_KEY_FILE"


def test_production_worker_settings_can_explicitly_disable_agent_runtime(tmp_path: Path, monkeypatch) -> None:
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
    assert settings.update_signer_socket_path == Path(
        "/run/vonk-signer/signer.sock"
    )


def test_production_requires_an_explicit_agent_ca_provider(monkeypatch) -> None:
    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", "production")

    with pytest.raises(SettingsError, match="VONK_AGENT_CA_PROVIDER"):
        Settings.from_env_and_secrets()


def test_production_rejects_an_invalid_agent_ca_provider(monkeypatch) -> None:
    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("VONK_AGENT_CA_PROVIDER", "unknown")

    with pytest.raises(SettingsError, match="is invalid"):
        Settings.from_env_and_secrets()


@pytest.mark.parametrize("key_kind", ("symlink", "directory"))
def test_builtin_bootstrap_key_must_be_a_regular_non_symlink_file(tmp_path: Path, monkeypatch, key_kind: str) -> None:
    values = {
        "VONK_DATABASE_URL_FILE": "postgresql://db/control",
        "VONK_TOKEN_SIGNING_KEY_FILE": "k" * 32,
        "VONK_METRICS_TOKEN_FILE": "m" * 16,
        "VONK_GIT_SIGNING_KEY_FILE": "git-key",
        "VONK_AGENT_CLIENT_CA_FILE": "client-ca",
        "VONK_AGENT_INTERMEDIATE_CERTIFICATE_FILE": "intermediate-certificate",
        "VONK_AGENT_PROXY_AUTH_FILE": "p" * 32,
    }
    monkeypatch.setenv("VONK_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("VONK_MANAGEMENT_CIDRS", "10.0.0.0/24")
    monkeypatch.setenv("VONK_AGENT_CA_PROVIDER", "builtin")
    monkeypatch.setenv("VONK_AGENT_BUILTIN_CA_BOOTSTRAP", "1")
    for name, value in values.items():
        path = tmp_path / name
        path.write_text(value)
        monkeypatch.setenv(name, str(path))
    key = tmp_path / "agent-intermediate-key"
    if key_kind == "symlink":
        target = tmp_path / "actual-key"
        target.write_text("key")
        key.symlink_to(target)
    else:
        key.mkdir()
    monkeypatch.setenv("VONK_AGENT_INTERMEDIATE_KEY_FILE", str(key))

    with pytest.raises(SettingsError, match="regular non-symlink"):
        Settings.from_env_and_secrets()
