from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs/runbooks/development-nas-installation.md"


def test_development_nas_runbook_defines_pull_only_immutable_install() -> None:
    text = RUNBOOK.read_text()

    for name in (
        "docker-compose.dev.yml",
        "docker-compose.production.yml",
        "docker-compose.pinned.yml",
    ):
        assert name in text
    assert "docker-compose.yml" in text
    assert "dev-sha-<commit>@sha256:<digest>" in text
    assert "docker compose -f docker-compose.yml pull" in text
    assert "docker compose -f docker-compose.yml up -d --wait" in text
    assert "There is no build context" in text
    assert "registry login" in text
    assert "Neither `dev` nor `latest`\nis deployment authority" in text


def test_development_nas_runbook_documents_only_runtime_secret_inputs() -> None:
    text = RUNBOOK.read_text()

    for name in ("postgres-password", "database-url", "git-signing-key"):
        assert name in text
    assert "openssl rand -hex 32" in text
    assert "ssh-keygen -q -t ed25519 -N ''" in text
    assert "refusing to overwrite %s" in text
    assert "chmod 0400" in text
    assert "chown 999:999" in text
    assert "chown 10001:10001" in text
    assert "Windows ACLs on an SMB drive do not establish" in text
    assert "must\nnot write directly to SMB" in text
    assert "$LASTEXITCODE -ne 0" in text
    assert (
        "Copy-Item -LiteralPath $tempKey -Destination $stagedKey -ErrorAction Stop"
        in text
    )
    assert "[IO.File]::Move($stagedKey, $key)" in text
    assert "Never commit a backup" in text


def test_rollback_discovers_volume_and_requires_matching_database_state() -> None:
    text = RUNBOOK.read_text()

    assert "docker compose -f docker-compose.yml ps -q control-api" in text
    assert "com.docker.compose.volume" in text
    assert "Type the exact volume name to confirm" in text
    assert "set -eu\ncd /volume1/docker/vonk-forge" in text
    assert "Never treat a repository-volume\nreset as a database or runtime-state rollback" in text
    assert "identity, control state, route publications, supervisor state" in text
    assert "docker volume rm vonk-forge-dev_dev-repository" not in text


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
