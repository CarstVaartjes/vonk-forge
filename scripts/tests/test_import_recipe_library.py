from __future__ import annotations

import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_source_files_accept_nested_directories(tmp_path: Path) -> None:
    context = tmp_path / "context"
    (context / "vendor" / "nested").mkdir(parents=True)
    (context / "Dockerfile").write_text("FROM scratch\n")
    (context / "vendor" / "nested" / "artifact.txt").write_text("payload\n")

    namespace = runpy.run_path(str(ROOT / "scripts" / "import-recipe-library"))

    assert namespace["_files"](context) == {
        "Dockerfile": b"FROM scratch\n",
        "vendor/nested/artifact.txt": b"payload\n",
    }
