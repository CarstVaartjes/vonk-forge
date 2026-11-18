"""The closed state words of Controller-only stored records.

These records are Controller-only, so their enums live here and not in
``vonk_agent_protocol.state_machines`` (whose machines are published through
``wire.json`` into Rust, OpenAPI and TypeScript).  Each is a ``WireEnum`` so its
CHECK is generated with ``machine_check``.  Recipe build words are the exception:
they are also published through the run-switch contract
(``RunSwitchContainerBuildState``, ``run_switch_contract/vocabulary.py``), a second
spelling of ``RecipeBuildState`` to be built from the enum later.
"""

from __future__ import annotations

from vonk_agent_protocol.wire_model import WireEnum


class RecipeBuildState(WireEnum):
    """The standing of one recipe container build."""

    PLANNED = "planned"
    BUILDING = "building"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
