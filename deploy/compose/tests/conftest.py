import sys
from pathlib import Path

import pytest

from tools import pytest_budget

CONTROL_SRC = Path(__file__).resolve().parents[3] / "control/src"
if str(CONTROL_SRC) not in sys.path:
    sys.path.insert(0, str(CONTROL_SRC))


def pytest_addoption(
    parser: pytest.Parser, pluginmanager: pytest.PytestPluginManager
) -> None:
    pytest_budget.register(pluginmanager)
