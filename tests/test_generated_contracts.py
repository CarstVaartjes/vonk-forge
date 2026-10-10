"""Generated consumer inputs match their canonical Pydantic producers.

Test setup generates the inputs before collection. Determinism is checked by
scripts/check-generated-determinism; producer/consumer contracts stay covered
here and in the Python, browser, and Rust round-trip suites.
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


def test_cli_release_projection_schema_is_current() -> None:
    module = _script("export-installer-release-schema")
    assert not _stale(module.CLI_OUTPUT, module.rendered_cli_projection())


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
    }
    stale = [
        str(path.relative_to(ROOT))
        for path, content in expected.items()
        if _stale(path, content)
    ]
    assert stale == [], "run scripts/generate-control-clients"


def test_typescript_vocabulary_is_current() -> None:
    module = _script("generate-control-clients")
    target = module.TYPESCRIPT_VOCABULARY
    assert not _stale(target, module.typescript_vocabulary()), (
        "stale vocabulary.generated.ts; run scripts/generate-control-clients"
    )


def test_python_vocabulary_modules_are_current() -> None:
    stale = [
        str(path.relative_to(ROOT))
        for path, content in _script("generate-python-vocabulary").rendered().items()
        if _stale(path, content)
    ]
    assert stale == [], "run scripts/generate-python-vocabulary"
