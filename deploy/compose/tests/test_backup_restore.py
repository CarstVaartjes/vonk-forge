from pathlib import Path


def test_runtime_exposes_no_mutable_host_backup_program() -> None:
    root = Path(__file__).resolve().parents[3]
    forbidden = (
        "bin/backup-control-plane",
        "bin/restore-control-plane",
    )

    for path in forbidden:
        assert not (root / "deploy/compose" / path).exists()
