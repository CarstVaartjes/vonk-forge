#!/usr/bin/env python3
"""Check every mounted HTTP operation against declarations and wire exports."""

from __future__ import annotations

import json
from pathlib import Path

from vonk_control.contract_graph import (
    discover_contracts,
    require_wire_exports,
    schema_application,
)


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    wire = json.loads(
        (root / "rust/crates/vonk-agent-protocol/schema/wire.json").read_text()
    )
    variants = {}
    for browser_auth in (False, True):
        operations, models = discover_contracts(
            schema_application(browser_auth=browser_auth)
        )
        require_wire_exports(models, wire)
        variants["browser" if browser_auth else "bearer"] = operations
    print(
        json.dumps(
            {
                "scope": "mounted HTTP declarations and agent export roots",
                "variants": variants,
            },
            sort_keys=True,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
