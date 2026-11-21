#!/usr/bin/env python3
"""Write ``build-identity.json``: the one stamp writer for server and CLI builds.

The CLI wheel hook (tools/hatch_build.py) and the Controller image's
``server-identity`` stage both run this, so one source tree gives the
Controller and vonkctl the same ``control_contract_sha256``. The fingerprint
function is the Controller's (``vonk_control.build_identity``), loaded by path
because this runs with only the standard library.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _contract_fingerprint(document: object) -> str:
    path = ROOT / "control/src/vonk_control/build_identity.py"
    spec = importlib.util.spec_from_file_location("_vonk_build_identity", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolves its module by name
    spec.loader.exec_module(module)
    return module.contract_fingerprint(document)


def _environment(name: str, pattern: str) -> str | None:
    value = os.environ.get(name) or None
    if value is not None and re.fullmatch(pattern, value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def write_build_identity(*, control_openapi: Path, wire: Path, output: Path) -> None:
    """Fingerprint the generated documents and stamp the build's source identity."""
    identity = {
        "schema_version": 2,
        "control_contract_sha256": _contract_fingerprint(
            json.loads(control_openapi.read_text())
        ),
        "worker_contract_sha256": _contract_fingerprint(json.loads(wire.read_text())),
        "source_sha": _environment("VONK_BUILD_SOURCE_SHA", "[0-9a-f]{40}"),
        "source_tree": _environment("VONK_BUILD_SOURCE_TREE", "[0-9a-f]{40}"),
        "release_version": _environment(
            "VONK_BUILD_RELEASE_VERSION", r"[0-9A-Za-z][0-9A-Za-z.+:~-]{0,127}"
        ),
    }
    output.write_text(json.dumps(identity, sort_keys=True) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-openapi", type=Path, required=True)
    parser.add_argument("--wire", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    write_build_identity(
        control_openapi=arguments.control_openapi,
        wire=arguments.wire,
        output=arguments.output,
    )


if __name__ == "__main__":
    main()
