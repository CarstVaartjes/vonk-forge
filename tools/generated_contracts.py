"""Prepare contract consumers before pytest imports test modules."""

import os
import subprocess
from pathlib import Path


def prepare_generated_contracts() -> None:
    root = Path(__file__).resolve().parents[1]
    if os.environ.get("VONK_TEST_CONTRACT_ROOT") == str(root):
        return
    subprocess.run(
        [str(root / "scripts/generate-control-clients")],
        cwd=root,
        check=True,
        timeout=1200,
    )
    subprocess.run(
        [
            str(root / "scripts/export-agent-wire-schema"),
            "--output",
            str(root / "rust/crates/vonk-agent-protocol/schema/wire.json"),
        ],
        cwd=root,
        check=True,
        timeout=1200,
    )
    os.environ["VONK_TEST_CONTRACT_ROOT"] = str(root)
