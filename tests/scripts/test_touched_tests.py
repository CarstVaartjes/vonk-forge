from __future__ import annotations

from pathlib import Path

from tools.touched_tests import select, touched_paths


def _tree(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _patch(*paths: str) -> str:
    return "".join(
        f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-old\n+new\n"
        for p in paths
    )


def test_selection_follows_edits_imports_and_support_modules(tmp_path: Path) -> None:
    _tree(
        tmp_path,
        {
            "control/src/vonk/engine.py": "",
            "control/src/vonk/other.py": "",
            "control/src/vonk/pkg/__init__.py": "",
            "control/src/vonk/pkg/inner.py": "",
            "control/tests/__init__.py": "",
            "control/tests/support.py": "from vonk import engine\n",
            "control/tests/test_edited.py": "",
            "control/tests/test_direct.py": "from vonk.engine import run\n",
            "control/tests/test_via_support.py": "from tests.support import x\n",
            "control/tests/test_unrelated.py": "from vonk import other\n",
            "tests/test_in_package.py": "import vonk.pkg.inner\n",
        },
    )

    def chosen(*paths: str) -> set[str]:
        return {p for _, p in select(list(paths), tmp_path)}

    assert chosen("control/tests/test_edited.py") == {"control/tests/test_edited.py"}
    assert chosen("control/src/vonk/engine.py") == {
        "control/tests/test_direct.py",
        "control/tests/test_via_support.py",
    }
    assert chosen("control/src/vonk/pkg/__init__.py") == {"tests/test_in_package.py"}
    assert "control/tests/test_unrelated.py" not in chosen("control/src/vonk/engine.py")


def test_touched_paths_read_the_patch_text_without_git() -> None:
    assert touched_paths(_patch("a/b.py", "c.py")) == ["a/b.py", "c.py"]
