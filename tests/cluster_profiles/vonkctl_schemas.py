"""Validators of the published vonkctl schemas, resolving the shared definitions."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from cluster_profiles.cli_json_contract import DocumentKind

SCHEMAS = Path(__file__).resolve().parents[2] / "schemas" / "vonkctl"
_TOOL = SCHEMAS.parents[1] / "tools" / "vonkctl_json_contract.py"


def validator(kind: DocumentKind) -> Any:
    name = "vonk_vonkctl_json_contract"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, _TOOL)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name].validator(kind, SCHEMAS)
