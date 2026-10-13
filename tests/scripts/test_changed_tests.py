"""The changed-test listing names exactly the tests a patch touches."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/changed-tests"


def _module():
    loader = importlib.machinery.SourceFileLoader("changed_tests", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_added_lines_name_their_test_functions_and_nothing_else(tmp_path: Path) -> None:
    """Catches a listing that names every test of a touched file, misses a new
    module, or treats a product source change as a changed test."""
    module = _module()
    tree = tmp_path
    (tree / "control/tests").mkdir(parents=True)
    (tree / "control/src").mkdir(parents=True)
    (tree / "control/tests/test_existing.py").write_text(
        textwrap.dedent(
            """
            import pytest


            def test_untouched():
                assert True


            @pytest.mark.parametrize("value", [1, 2])
            def test_touched(value):
                assert value
                assert value > 0


            class TestGroup:
                def test_method(self):
                    assert True
            """
        ).lstrip()
    )
    (tree / "control/tests/test_new.py").write_text(
        "def test_one():\n    assert True\n\n\ndef test_two():\n    assert True\n"
    )
    (tree / "control/src/module.py").write_text("def test_like_name():\n    return 1\n")
    patch = textwrap.dedent(
        """
        diff --git a/control/tests/test_existing.py b/control/tests/test_existing.py
        --- a/control/tests/test_existing.py
        +++ b/control/tests/test_existing.py
        @@ -10,0 +11 @@
        +    assert value > 0
        @@ -15,0 +16 @@
        +        assert True
        diff --git a/control/tests/test_new.py b/control/tests/test_new.py
        --- /dev/null
        +++ b/control/tests/test_new.py
        @@ -0,0 +1,6 @@
        +def test_one():
        +    assert True
        +
        +
        +def test_two():
        +    assert True
        diff --git a/control/src/module.py b/control/src/module.py
        --- a/control/src/module.py
        +++ b/control/src/module.py
        @@ -1,0 +2 @@
        +    return 1
        """
    ).lstrip()

    assert module.changed_tests(patch, tree) == [
        "control/tests/test_existing.py::test_method",
        "control/tests/test_existing.py::test_touched",
        "control/tests/test_new.py::test_one",
        "control/tests/test_new.py::test_two",
    ]
