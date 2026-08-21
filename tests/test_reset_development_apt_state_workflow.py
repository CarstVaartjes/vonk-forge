from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/reset-development-apt-state.yml"


def test_reset_is_manual_exact_and_development_scoped() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in text
    assert "push:" not in text
    assert "schedule:" not in text
    assert "environment: apt-development" in text
    assert "github.ref == 'refs/heads/main'" in text
    assert text.count("RESET-vonk-forge-packages-dev-state") == 2
    assert "test \"$R2_APT_STATE_BUCKET\" = vonk-forge-packages-dev-state" in text
    assert 'rclone delete \"r2:$R2_APT_STATE_BUCKET\"' in text
    assert "R2_APT_PUBLIC_BUCKET" not in text
    assert "rclone purge" not in text


def test_reset_verifies_the_bucket_is_empty_without_listing_object_names() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert text.count("--recursive --files-only | wc -l") == 2
    assert 'test "$remaining" = 0' in text
    assert "object_count" in text
