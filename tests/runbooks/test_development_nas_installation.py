from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs/runbooks/development-nas-installation.md"
COMPOSE_README = ROOT / "deploy/compose/README.md"


def _normalized_text(path: Path) -> str:
    return " ".join(path.read_text().split())


def test_development_nas_runbook_leads_with_the_mutable_two_item_project() -> None:
    text = _normalized_text(RUNBOOK)

    assert "Its contents must be exactly:" in text
    assert "├── docker-compose.yml" in text
    assert "└── secrets/" in text
    assert "docker-compose.dev.yml` as the bare mutable `:dev`" in text
    assert "pull/redeploy the unchanged `docker-compose.yml`" in text
    assert "not restart" in text


def test_development_nas_runbook_explains_mixed_cohort_retry_before_migration() -> None:
    text = _normalized_text(RUNBOOK)

    assert "mixed pull" in text
    assert "cohort gate exits" in text
    assert "before `migrate`" in text
    assert "Do not delete `secrets/` or named volumes" in text


def test_development_nas_runbook_documents_actual_startup_dependency_order() -> None:
    text = _normalized_text(RUNBOOK).split(
        "After the UI reports the deployment", maxsplit=1
    )[1]
    order = (
        "cohort reset",
        "API and worker cohort reporters",
        "cohort verifier",
        "PostgreSQL",
        "`dev-init`",
        "`migrate`",
        "long-running API and worker",
    )

    positions = [text.index(item) for item in order]
    assert positions == sorted(positions)


def test_production_channel_uses_the_trusted_host_updater_and_latest_is_evaluation_only() -> None:
    text = _normalized_text(COMPOSE_README)

    assert "signed `stable` channel through the trusted host updater" in text
    assert "`:latest` is evaluation/discovery only" in text


def test_pinned_rollback_requires_schema_compatible_repository_reset_or_full_state_restore() -> None:
    text = _normalized_text(RUNBOOK)

    assert "repository-volume reset only" in text
    assert "target schema is compatible" in text
    assert "matching full-state restore" in text
    assert "clean development reinstall" in text


def test_development_nas_runbook_keeps_pull_only_runtime_constraints() -> None:
    text = RUNBOOK.read_text()

    assert "there is no build context" in text
    assert "registry login" in text
    assert "does not clone\nthis repository" in text


def test_normal_install_and_update_path_is_ui_only_before_guarded_recovery() -> None:
    text = RUNBOOK.read_text()
    assert "## Advanced guarded recovery" in text
    supported_path, guarded_recovery = text.split(
        "## Advanced guarded recovery", maxsplit=1
    )
    _, normal_nas_path = supported_path.split(
        "## Create and redeploy the Compose project", maxsplit=1
    )

    for marker in (
        "```bash",
        "```shell",
        "```powershell",
        "sudo ",
        "ssh.exe ",
        "`ssh-keygen`",
    ):
        assert marker not in normal_nas_path
    assert "docker compose -f " not in normal_nas_path
    assert "scripts/dev-runtime-secrets.py" in supported_path
    assert "scripts/dev-runtime-project" in supported_path
    assert "```bash" in guarded_recovery


def test_development_nas_runbook_documents_only_runtime_secret_inputs() -> None:
    text = _normalized_text(RUNBOOK)

    for name in ("postgres-password", "database-url", "git-signing-key"):
        assert name in text
    assert "64 lowercase hexadecimal characters followed by one newline" in text
    assert "postgresql+psycopg://control:<postgres-password>@postgres:5432/control" in text
    assert "unencrypted Ed25519 OpenSSH private key" in text
    assert "SMB share or NAS file manager" in text
    assert "Windows ACLs on an SMB drive do not establish" in text
    assert "Never commit a backup" in text


def test_rollback_discovers_volume_and_requires_matching_database_state() -> None:
    text = RUNBOOK.read_text()
    normalized = _normalized_text(RUNBOOK)
    recovery_block = next(
        block
        for block in text.split("```bash\n")[1:]
        if "expected_commit=REPLACE_WITH_PINNED_40_CHARACTER_COMMIT" in block
    ).split("```", maxsplit=1)[0]

    assert "docker compose -f docker-compose.yml ps -q control-api" in text
    assert "com.docker.compose.volume" in text
    assert "Type the exact volume name to confirm" in text
    assert "NAS_PROJECT_DIRECTORY='<NAS_PROJECT_DIRECTORY>'" in recovery_block
    assert 'case "$NAS_PROJECT_DIRECTORY" in' in recovery_block
    assert 'test -d "$NAS_PROJECT_DIRECTORY"' in recovery_block
    assert 'test -f "$NAS_PROJECT_DIRECTORY/docker-compose.yml"' in recovery_block
    assert 'test -d "$NAS_PROJECT_DIRECTORY/secrets"' in recovery_block
    assert 'cd -- "$NAS_PROJECT_DIRECTORY"' in recovery_block
    assert "/volume1/" not in recovery_block
    assert "Never treat a repository-volume\nreset as a database or runtime-state rollback" in text
    assert "identity, control state, route publications, supervisor state" in normalized
    assert "docker volume rm vonk-forge-dev_dev-repository" not in text
    assert "replace `docker-compose.yml` with that exact pinned artifact" in normalized
    assert "expected_commit=REPLACE_WITH_PINNED_40_CHARACTER_COMMIT" in text
    assert "docker compose -f docker-compose.yml config --images" in text
    assert "git -C /repository rev-parse refs/heads/main" in text
    assert "git -C /repository merge-base --is-ancestor" in text
    assert "dev-sha-$expected_commit@sha256:" in text


def test_design_recovery_link_targets_the_guarded_runbook_section() -> None:
    design = (
        ROOT
        / "docs/superpowers/specs/2026-08-10-mutable-compose-channels-design.md"
    ).read_text()

    assert "development-nas-installation.md#advanced-guarded-recovery" in design
    assert "#updating-to-a-newer-accepted-main-commit" not in design


def test_operator_entry_points_link_to_development_nas_runbook() -> None:
    assert "docs/runbooks/development-nas-installation.md" in (
        ROOT / "README.md"
    ).read_text()
    assert "runbooks/development-nas-installation.md" in (
        ROOT / "docs/README.md"
    ).read_text()
    assert "../../docs/runbooks/development-nas-installation.md" in (
        ROOT / "deploy/compose/README.md"
    ).read_text()


def test_runtime_secret_directory_is_ignored_from_git() -> None:
    assert "/secrets/" in (ROOT / ".gitignore").read_text().splitlines()
