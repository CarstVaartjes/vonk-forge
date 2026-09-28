"""Committed generated contracts equal what their generators produce now.

Each generator renders in memory here; nothing in the worktree is rewritten.
Regenerate with the script named in the failure. The Rust side of the agent
wire (``generated.rs`` from ``wire.json``) is checked by the vonk-wire-codegen
crate's own test, and the language clients by the "Generated control clients"
CI job, which needs Node.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def _script(name: str) -> ModuleType:
    path = ROOT / "scripts" / name
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _stale(path: Path, content: str) -> bool:
    return not path.is_file() or path.read_text(encoding="utf-8") != content


def test_agent_wire_schema_is_current() -> None:
    rendered = _script("export-agent-wire-schema").rendered()
    wire = ROOT / "rust/crates/vonk-agent-protocol/schema/wire.json"
    assert not _stale(wire, rendered), (
        "stale wire.json; run scripts/generate-agent-wire"
    )


def test_installer_release_schema_is_current() -> None:
    module = _script("export-installer-release-schema")
    assert not _stale(module.OUTPUT, module.rendered()), (
        "stale installer release schema; run scripts/generate-agent-wire"
    )


def test_qualification_campaign_schemas_are_current() -> None:
    stale = [
        str(path.relative_to(ROOT))
        for path, content in _script("export-qualification-campaign-schemas")
        .rendered()
        .items()
        if _stale(path, content)
    ]
    assert stale == [], "run scripts/export-qualification-campaign-schemas"


def test_control_openapi_documents_are_current() -> None:
    module = _script("generate-control-clients")

    def document(*, include_browser_auth: bool) -> str:
        schema = module._schema(include_browser_auth=include_browser_auth)
        return json.dumps(schema, sort_keys=True, indent=2) + "\n"

    admin = document(include_browser_auth=False)
    expected = {
        module.OPENAPI: document(include_browser_auth=True),
        module.CLI_OPENAPI_OUTPUT: admin,
        module.CLI_OPENAPI_MIRROR: admin,
    }
    stale = [
        str(path.relative_to(ROOT))
        for path, content in expected.items()
        if _stale(path, content)
    ]
    assert stale == [], "run scripts/generate-control-clients"
